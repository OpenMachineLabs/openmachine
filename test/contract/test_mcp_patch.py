"""Behavior checks for MCPContractTests."""

import asyncio
import hashlib
import json
import sys
import time
import threading
import unittest
from unittest.mock import patch
import uuid
from mcp import ClientSession
from openmachine import server
from openmachine.config import Settings
from support_mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_patch_preview_apply_and_original_receipt_survive_server_restart(self):
        source = self.work / "source.py"
        source.write_bytes(b"answer = 41\r\n")
        obsolete = self.work / "obsolete"
        obsolete.write_bytes(b"old\n")
        scope = {"machine": "desktop", "workspace_name": "example"}
        identity = uuid.uuid4().hex
        text = ("*** Begin Patch\n*** Update File: source.py\n*** Move to: new/source.py\n"
                "@@\n-answer = 41\n+answer = 42\n*** Add File: new/expected\n+42\n"
                "*** Delete File: obsolete\n*** End Patch\n")
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                read = (await session.call_tool("read_workspace_file", scope | {"path":"source.py"})).structured_content
                request = scope | {"patch":text,"expected_workspace_identity":read["context"]["workspace_identity"],
                    "expected_head_sha":"","create_parents":True,
                    "preconditions":[{"path":"source.py","expected_sha256":read["sha256"]},
                        {"path":"new/source.py","expected_sha256":""},{"path":"new/expected","expected_sha256":""},
                        {"path":"obsolete","expected_sha256":hashlib.sha256(obsolete.read_bytes()).hexdigest()}]}
                preview = await session.call_tool("apply_workspace_patch", request | {"dry_run":True})
                self.assertFalse(preview.is_error,preview)
                self.assertFalse((self.work / "new").exists())
                self.assertFalse((self.state / "workspace-changes").exists())
                request.update(change_id=identity,expected_plan_sha256=preview.structured_content["plan_sha256"])
                result = await session.call_tool("apply_workspace_patch",request)
                self.assertFalse(result.is_error,result)
                first = result.structured_content
                self.assertEqual(first["status"],"applied")
                self.assertEqual([item["kind"] for item in first["effects"]], ["mkdir","publish","remove","publish","remove"])
                self.assertEqual((self.work / "new/source.py").read_bytes(), b"answer = 42\r\n")
                self.assertFalse(source.exists())
                self.assertFalse(obsolete.exists())
                caps = (await session.call_tool("get_capabilities",{})).structured_content
                self.assertEqual(caps["workspace"]["patch_language"],"om_patch_v1")
                self.assertEqual(caps["workspace"]["change_read_formats"],[1,2])
                self.assertFalse((self.state / "executions").exists(), "Patch must not run hidden commands")
                validation_id, task_id = uuid.uuid4().hex, "patch-validation"
                post_hashes = {item["path"]: item["after_sha256"] for item in first["plan"]["files"]}
                command = [sys.executable, "-I", "-c",
                    "from pathlib import Path; import runpy; "
                    "p=Path('validation-count'); assert not p.exists(); p.write_text('1'); "
                    "assert runpy.run_path('new/source.py')['answer']==int(Path('new/expected').read_text()); "
                    "print('PATCH_VALIDATION_OK')"]
                evidence = {"change_id": identity, "execution_id": validation_id, "head_sha": "",
                            "post_hashes": post_hashes, "argv": command, "cwd": str(self.work), "status": "planned"}
                saved = await session.call_tool("save_task", {"task_id": task_id, "expected_revision": 0,
                    "title": "Validate original patch", "body": json.dumps(evidence),
                    "anchors": [f"change=desktop/{identity}", f"execution=desktop/{validation_id}"]})
                self.assertFalse(saved.is_error, saved)
                for path, sha in post_hashes.items():
                    current = (await session.call_tool("read_workspace_file", scope | {"path": path})).structured_content
                    self.assertEqual(current["sha256"], sha)
                started = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": validation_id,
                    "cwd": str(self.work), "argv": command, "expected_head_sha": "", "task_id": task_id})
                self.assertFalse(started.is_error, started)
                deadline = time.monotonic() + 10
                while True:
                    observed = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": validation_id,
                        "wait_seconds": 0.2})
                    self.assertFalse(observed.is_error, observed)
                    validation = observed.structured_content
                    if validation["status"] in {"succeeded", "failed", "cancelled", "timed_out", "outcome_unknown"}:
                        break
                    self.assertLess(time.monotonic(), deadline)
                self.assertEqual((validation["status"], validation["exit_code"], validation["cleanup"]["status"]),
                                 ("succeeded", 0, "verified"))
                self.assertIn("PATCH_VALIDATION_OK", validation["output"])
                for path, sha in post_hashes.items():
                    current = (await session.call_tool("read_workspace_file", scope | {"path": path})).structured_content
                    self.assertEqual(current["sha256"], sha)
                evidence.update(status="succeeded", execution_created_at=validation["created_at"])
                saved = await session.call_tool("save_task", {"task_id": task_id, "expected_revision": 1,
                    "title": "Validate original patch", "body": json.dumps(evidence)})
                self.assertFalse(saved.is_error, saved)
                self.assertEqual(saved.structured_content["revision"], 2)
        self.stop(self.processes[-1])
        record = self.state / "workspace-changes" / identity / "record.json"
        retained = record.read_bytes()
        (self.work / "new/source.py").write_text("later work\n")
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                read = await session.call_tool("read_workspace_change",{"machine":"desktop","change_id":identity,"observe_current":True})
                self.assertFalse(read.is_error,read)
                self.assertEqual((read.structured_content["created_at"],read.structured_content["revision"]),(first["created_at"],first["revision"]))
                task = (await session.call_tool("read_task", {"task_id": task_id})).structured_content
                self.assertEqual(task["revision"], 2)
                self.assertEqual(json.loads(task["body"]), evidence)
                original_execution = (await session.call_tool("read_execution", {
                    "machine": "desktop", "execution_id": validation_id})).structured_content
                self.assertEqual((original_execution["created_at"], original_execution["status"], original_execution["exit_code"]),
                                 (evidence["execution_created_at"], "succeeded", 0))
                current_hashes = {item["path"]: item["sha256"] for item in read.structured_content["current"]["files"]}
                self.assertNotEqual(current_hashes["new/source.py"], evidence["post_hashes"]["new/source.py"],
                                    "Historical test success must not validate subsequent source drift")
                again = await session.call_tool("apply_workspace_patch",request)
                self.assertFalse(again.is_error,again)
                self.assertEqual(again.structured_content["revision"],first["revision"])
                self.assertEqual((self.work / "new/source.py").read_text(),"later work\n")
                conflict = await session.call_tool("apply_workspace_patch",request | {"create_parents":False})
                self.assertTrue(conflict.is_error)
                self.assertEqual(conflict.structured_content["error"]["code"],"change_id_conflict")
        self.assertEqual(record.read_bytes(),retained)
        self.assertEqual((self.work / "validation-count").read_text(), "1")
        self.assertEqual([p.name for p in (self.state / "executions").iterdir()], [validation_id])

    async def test_patch_strict_schema_independent_grant_and_escaped_response(self):
        self.stop(self.processes[-1])
        self.config.write_text(self.config.read_text().replace("execution_enabled = true","execution_enabled = false\nworkspace_write_enabled = true"))
        self.launch()
        scope = {"machine":"desktop","workspace_name":"example"}
        text = "*** Begin Patch\n*** Add File: escaped\n+" + "\x01" * 32760 + "\n*** End Patch\n"
        async with self.client() as (reader, writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                read = (await session.call_tool("read_workspace_file",scope | {"path":"AGENTS.md"})).structured_content
                request = scope | {"patch":text,"expected_workspace_identity":read["context"]["workspace_identity"],
                                  "expected_head_sha":"","preconditions":[{"path":"escaped","expected_sha256":""}]}
                for delta in ({"create_parents":1},{"dry_run":"false"},{"preconditions":[]},
                              {"preconditions":[{"path":"escaped","expected_sha256":"","extra":True}]}):
                    result = await session.call_tool("apply_workspace_patch",request | {"change_id":uuid.uuid4().hex} | delta)
                    self.assertTrue(result.is_error)
                    self.assertFalse((self.work / "escaped").exists())
                identity = uuid.uuid4().hex
                result = await session.call_tool("apply_workspace_patch",request | {"change_id":identity})
                self.assertFalse(result.is_error,result)
                self.assertEqual(result.structured_content["status"],"applied")
                self.assertEqual(result.structured_content["change_id"],identity)
                self.assertEqual(len(result.structured_content["effects"]),1)
                self.assertEqual(json.loads(result.content[0].text),result.structured_content)
                self.assertLessEqual(len(json.dumps(result.model_dump(mode="json",by_alias=True,exclude_none=True)).encode()),server.MAX_WORKSPACE_RESULT-1024)
                denied = await session.call_tool("start_execution",{"machine":"desktop","execution_id":uuid.uuid4().hex,
                    "cwd":str(self.work),"argv":[sys.executable,"-c","pass"]})
                self.assertEqual(denied.structured_content["error"]["code"],"execution_disabled")

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
