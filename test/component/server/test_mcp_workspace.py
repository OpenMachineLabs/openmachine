"""component server test_mcp_workspace behavior."""

import asyncio
import time
import threading
import unittest
from unittest.mock import patch
import uuid
from openmachine import server
from openmachine.config import Settings
from test.support.mcp import MCPFixture
import os
from pathlib import Path
import tempfile


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_patch_disconnect_and_partial_receipt_keep_original_effects(self):
        from openmachine.bootstrap import create_application
        from openmachine.workspace import changes
        root = self.root.resolve()
        file = self.work / "source"
        file.write_text("old\n")
        config = Settings.model_validate({"schema_version":1,"machines":{"local":{"transport":"local","workspace_write_enabled":True}},
                                         "workspaces":{"project":{"machine":"local","root":str(self.work.resolve())}}})
        state = root / "patch-state"
        app = create_application(config,state)
        observed = app.read_workspace_file("local","project","source")
        with patch.object(server,"create_application",return_value=app):
            mcp = server.create_server(config,state)
        identity = uuid.uuid4().hex
        request = {"machine":"local","workspace_name":"project","change_id":identity,"expected_head_sha":"",
            "expected_workspace_identity":observed["context"]["workspace_identity"],
            "patch":"*** Begin Patch\n*** Add File: first\n+yes\n*** Add File: second\n+no\n*** End Patch\n",
            "preconditions":[{"path":"first","expected_sha256":""},{"path":"second","expected_sha256":""}]}
        entered,release = threading.Event(),threading.Event()
        original = changes.publish_file
        def controlled(*values,**kwargs):
            if values[3]["path"] == "second":
                return {"stage":"failed_no_effect","result_sha256":None,"synced":False,"readback_verified":False,
                        "error":"fixture_io","cleanup":"not_created"}
            entered.set()
            if not release.wait(5):
                raise AssertionError("Fixture publisher not released")
            return original(*values,**kwargs)
        with patch.object(changes,"publish_file",side_effect=controlled) as publisher:
            mutation = asyncio.create_task(mcp.call_tool("apply_workspace_patch",request))
            try:
                deadline = time.monotonic()+3
                while not entered.is_set() and not mutation.done() and time.monotonic()<deadline:
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set(),mutation.result() if mutation.done() else "publisher not entered")
                mutation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await mutation
            finally:
                release.set()
            deadline = time.monotonic()+3
            while time.monotonic()<deadline:
                read = await mcp.call_tool("read_workspace_change",{"machine":"local","change_id":identity})
                if read.structured_content["status"] == "partial":
                    break
                await asyncio.sleep(.01)
            self.assertEqual(read.structured_content["status"],"partial")
            self.assertFalse(read.is_error,"Inspecting a failed change is a successful read, not a successful patch")
            again = await mcp.call_tool("apply_workspace_patch",request)
            self.assertTrue(again.is_error)
            self.assertEqual(again.structured_content["effects"],read.structured_content["effects"])
            self.assertEqual(publisher.call_count,2)
            self.assertEqual((self.work / "first").read_text(),"yes\n")
            self.assertFalse((self.work / "second").exists())

    async def test_disconnected_edit_observer_does_not_stop_or_repeat_original_publication(self):
        from openmachine.bootstrap import create_application
        from openmachine.workspace import changes
        with tempfile.TemporaryDirectory(prefix="om-edit-disconnect-", dir="/tmp") as directory:
            root = Path(directory).resolve()
            file = root / "source.py"
            file.write_text("answer = 41")
            config = Settings.model_validate({"schema_version": 1,
                "machines": {"local": {"transport": "local", "workspace_write_enabled": True}},
                "workspaces": {"p": {"machine": "local", "root": str(root)}}})
            app = create_application(config, root / "state")
            read = app.read_workspace_file("local", "p", "source.py")
            previous_umask = os.umask(0o022)
            try:
                with patch.object(server, "create_application", return_value=app):
                    mcp = server.create_server(config, root / "state")
            finally:
                os.umask(previous_umask)
            self.assertEqual((root / "state").stat().st_mode & 0o777, 0o700)
            identity = uuid.uuid4().hex
            args = {"machine": "local", "workspace_name": "p", "path": "source.py",
                    "edits": [{"old_text": "41", "new_text": "42"}], "expected_head_sha": "",
                    "expected_workspace_identity": read["context"]["workspace_identity"],
                    "expected_sha256": read["sha256"], "change_id": identity}
            entered, release = threading.Event(), threading.Event()
            actual = changes.publish_file
            def paused(*values):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("The fixture failed to release its publisher")
                return actual(*values)
            with patch.object(changes, "publish_file", side_effect=paused) as publisher:
                mutation = asyncio.create_task(mcp.call_tool("edit_workspace_file", args))
                try:
                    deadline = time.monotonic() + 3
                    while not entered.is_set() and not mutation.done() and time.monotonic() < deadline:
                        await asyncio.sleep(0.01)
                    if mutation.done():
                        self.fail(f"Edit returned before fixture publication: {mutation.result()!r}")
                    self.assertTrue(entered.is_set())
                    observed = await asyncio.wait_for(mcp.call_tool("read_workspace_change", {
                        "machine": "local", "change_id": identity}), 1)
                    self.assertEqual(observed.structured_content["status"], "applying")
                    self.assertTrue(observed.structured_content["writer_alive"])
                    mutation.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await mutation
                finally:
                    release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    observed = await mcp.call_tool("read_workspace_change", {"machine": "local", "change_id": identity})
                    if observed.structured_content["status"] == "applied":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(observed.structured_content["status"], "applied")
                repeated = await mcp.call_tool("edit_workspace_file", args)
                self.assertEqual(repeated.structured_content["revision"], observed.structured_content["revision"])
                self.assertEqual(publisher.call_count, 1)
                self.assertEqual(file.read_text(), "answer = 42")
