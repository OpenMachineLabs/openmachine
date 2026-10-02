"""Behavior checks for MCPContractTests."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
    async def test_real_write_survives_service_restart_and_is_not_replayed(self):
        execution_id = uuid.uuid4().hex
        request = {"machine": "desktop", "execution_id": execution_id, "cwd": str(self.work),
                   "argv": [sys.executable, "-c", 'import time; from pathlib import Path; time.sleep(0.7); p=Path("result.txt"); p.write_text(p.read_text()+"done" if p.exists() else "done"); print("complete")']}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                rules = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "read", "path": "AGENTS.md"})
                self.assertIn("disposable", rules.structured_content["text"])
                started = await session.call_tool("start_execution", request)
                self.assertFalse(started.is_error, started)
        self.stop(self.processes[0])
        self.assertFalse(self.socket.exists(), "Graceful shutdown must remove its own socket")
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for _ in range(100):
                    result = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})
                    self.assertFalse(result.is_error, result)
                    if result.structured_content["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(result.structured_content["exit_code"], 0)
                self.assertIn("complete", result.structured_content["output"])
                again = await session.call_tool("start_execution", request)
                self.assertEqual(again.structured_content["status"], "succeeded")
                changed = await session.call_tool("start_execution", {**request, "argv": ["false"]})
                self.assertTrue(changed.is_error)
                self.assertEqual(changed.structured_content["error"]["code"], "execution_conflict")
                observed = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                            "action": "read", "path": "result.txt"})
                self.assertEqual(observed.structured_content["text"], "done")

    async def test_large_file_offsets_remain_usable_through_mcp(self):
        (self.work / "large.txt").write_text("x" * 180000)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "read", "path": "large.txt", "offset": 131072})
                self.assertFalse(result.is_error, result)
                page = result.structured_content
                self.assertEqual(page["next_offset"], 163840)
                self.assertTrue(page["has_more"])
                self.assertEqual(len(page["text"].encode()), 32768)
                tail = await session.call_tool("inspect_workspace", {
                    "machine": "desktop", "workspace_name": "example", "action": "read",
                    "path": "large.txt", "offset": page["next_offset"]})
                self.assertFalse(tail.is_error, tail)
                self.assertEqual(tail.structured_content["next_offset"], 180000)
                self.assertFalse(tail.structured_content["has_more"])
                self.assertEqual(page["text"] + tail.structured_content["text"], "x" * (180000 - 131072))

    async def test_complete_coding_workflow_uses_real_mcp_tools(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.work), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        (self.work / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
        (self.work / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add\nclass Addition(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        git("add", "AGENTS.md", "calculator.py", "test_calculator.py")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
        head = git("rev-parse", "HEAD")
        (self.work / "unrelated.txt").write_text("preserve this existing work\n")
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                scope = {"machine": "desktop", "workspace_name": "example"}
                rules = await session.call_tool("inspect_workspace", {**scope, "action": "read", "path": "AGENTS.md"})
                self.assertIn("disposable", rules.structured_content["text"])
                before = await session.call_tool("inspect_workspace", {**scope, "action": "status"})
                self.assertEqual(before.structured_content["context"]["head_sha"], head)
                found = await session.call_tool("inspect_workspace", {**scope, "action": "search", "query": "return a - b"})
                self.assertEqual(found.structured_content["matches"][0]["path"], "calculator.py")
                commands = [
                    [sys.executable, "-c", 'from pathlib import Path; p=Path("calculator.py"); s=p.read_text(); assert s.count("return a - b")==1; p.write_text(s.replace("return a - b", "return a + b"))'],
                    [sys.executable, "-m", "unittest", "discover", "-p", "test_calculator.py"],
                ]
                for argv in commands:
                    execution_id = uuid.uuid4().hex
                    request = {"machine": "desktop", "execution_id": execution_id, "cwd": str(self.work),
                               "argv": argv, "expected_head_sha": head}
                    started = await session.call_tool("start_execution", request)
                    self.assertFalse(started.is_error, started)
                    for _ in range(100):
                        result = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})
                        self.assertFalse(result.is_error, result)
                        if result.structured_content["status"] in ("succeeded", "failed", "outcome_unknown"):
                            break
                        await asyncio.sleep(0.03)
                    self.assertEqual((result.structured_content["status"], result.structured_content["exit_code"]), ("succeeded", 0))
                self.assertIn("OK", result.structured_content["output"])
                diff = await session.call_tool("inspect_workspace", {**scope, "action": "diff", "path": "calculator.py"})
                self.assertFalse(diff.is_error, diff)
                self.assertIn("+    return a + b", diff.structured_content["text"])
                self.assertFalse(diff.structured_content["truncated"])
                readback = await session.call_tool("inspect_workspace", {**scope, "action": "read", "path": "calculator.py"})
                self.assertEqual(readback.structured_content["text"], "def add(a, b):\n    return a + b\n")
                self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve this existing work\n")
                self.assertEqual(git("rev-parse", "HEAD"), head)

    async def test_explicit_edits_and_checkpoints_continue_across_client_sessions(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.work), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        (self.work / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
        (self.work / "test_calculator.py").write_text(
            "import unittest\nfrom pathlib import Path\nfrom calculator import add\n"
            "class Addition(unittest.TestCase):\n    def test_add(self):\n"
            "        p = Path('test_runs.txt')\n"
            "        p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
            "        self.assertEqual(add(2, 3), 5)\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
        head = git("rev-parse", "HEAD")
        (self.work / "unrelated.txt").write_text("preserve existing work\n")
        source = self.root / "user-skill"
        source.mkdir()
        (source / "SKILL.md").write_text("---\nname: repair\n---\nInspect the diff before completion.\n")
        self.stop(self.processes[0])
        self.config.write_text(self.config.read_text() + '\n[skills]\nroots = [' + json.dumps(str(source)) + ']\n')
        self.launch()
        scope = {"machine": "desktop", "workspace_name": "example"}
        identity = uuid.uuid4().hex
        anchor = "issue=example/project#17"
        checkpoint = {"task_id": "resume-fixture", "title": "Repair calculator", "anchors": [anchor]}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = (await session.call_tool("inspect_skills", {})).structured_content
                selected = next(item["skill"] for item in listing["skills"] if item["name"] == "repair")
                self.assertIn("Inspect the diff", (await session.call_tool("inspect_skills", {"action": "read", "skill": selected})).structured_content["text"])
                original = (await session.call_tool("read_workspace_file", {**scope, "path": "calculator.py"})).structured_content
                initial = await session.call_tool("save_task", {**checkpoint, "expected_revision": 0,
                    "body": "Goal: repair calculator in the disposable project.\nNext: make the authorized change.\n"})
                self.assertFalse(initial.is_error, initial)
                changed = await session.call_tool("write_workspace_file", {**scope, "path": "calculator.py",
                    "content": "def add(a, b):\n    return a + b\n", "expected_head_sha": head,
                    "expected_sha256": original["sha256"]})
                self.assertEqual(changed.structured_content["status"], "written")
                intent = await session.call_tool("save_task", {**checkpoint, "expected_revision": 1,
                    "body": "Completed: code repair.\nExecution: " + identity + "\nNext: run the project test once.\n"})
                self.assertFalse(intent.is_error, intent)
                start = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": identity,
                    "cwd": str(self.work), "argv": [sys.executable, "-m", "unittest", "discover", "-p", "test_calculator.py"],
                    "expected_head_sha": head, "task_id": "resume-fixture"})
                self.assertFalse(start.is_error, start)
                for _ in range(100):
                    receipt = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                    if receipt.structured_content["status"] in ("succeeded", "failed", "outcome_unknown"):
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual((receipt.structured_content["status"], receipt.structured_content["exit_code"]), ("succeeded", 0))
                self.assertEqual(receipt.structured_content["task_id"], "resume-fixture")
                result = await session.call_tool("save_task", {**checkpoint, "expected_revision": 2,
                    "body": "Completed: repair and passing tests.\nOriginal execution: " + identity + "\nNext: inspect diff only.\n"})
                self.assertFalse(result.is_error, result)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                found = await session.call_tool("find_tasks", {"anchors": [anchor]})
                self.assertEqual(found.structured_content["status"], "matched")
                checkpoint_read = await session.call_tool("read_task", {"task_id": "resume-fixture"})
                self.assertIn(identity, checkpoint_read.structured_content["body"])
                observed = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                self.assertEqual(observed.structured_content["status"], "succeeded")
                status = await session.call_tool("inspect_workspace", {**scope, "action": "status"})
                self.assertEqual(status.structured_content["context"]["head_sha"], head)
                diff = await session.call_tool("inspect_workspace", {**scope, "action": "diff", "path": "calculator.py"})
                self.assertIn("+    return a + b", diff.structured_content["text"])
                latest = await session.call_tool("read_workspace_file", {**scope, "path": "calculator.py"})
                self.assertEqual(latest.structured_content["sha256"], changed.structured_content["sha256"])
                final = await session.call_tool("save_task", {**checkpoint, "expected_revision": 3,
                    "body": "Completed: repair, original passing tests, unchanged source, and final diff review.\nNo further action.\n"})
                self.assertEqual(final.structured_content["revision"], 4)
        self.assertEqual((self.work / "test_runs.txt").read_text(), "1")
        self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve existing work\n")
        self.assertEqual(len(list((self.state / "executions").iterdir())), 1)

    async def test_exact_edit_preview_and_original_receipt_survive_server_restart(self):
        file = self.work / "exact.py"
        file.write_bytes(b"answer = 41\r\nuntouched = 7")
        scope = {"machine": "desktop", "workspace_name": "example", "path": "exact.py"}
        change_id = uuid.uuid4().hex
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                read = (await session.call_tool("read_workspace_file", scope)).structured_content
                args = {**scope, "edits": [{"old_text": "41", "new_text": "42"}],
                        "expected_workspace_identity": read["context"]["workspace_identity"],
                        "expected_head_sha": "", "expected_sha256": read["sha256"]}
                preview = await session.call_tool("edit_workspace_file", args | {"dry_run": True})
                self.assertFalse(preview.is_error, preview)
                self.assertEqual(preview.structured_content["status"], "preview")
                self.assertFalse((self.state / "workspace-changes").exists())
                self.assertIn(b"41", file.read_bytes())
                args.update(change_id=change_id, expected_plan_sha256=preview.structured_content["plan_sha256"])
                applied = await session.call_tool("edit_workspace_file", args)
                self.assertFalse(applied.is_error, applied)
                first = applied.structured_content
                self.assertEqual(first["status"], "applied")
                self.assertEqual(file.read_bytes(), b"answer = 42\r\nuntouched = 7")
                repeated = await session.call_tool("edit_workspace_file", args)
                self.assertFalse(repeated.is_error, repeated)
                self.assertEqual((repeated.structured_content["created_at"], repeated.structured_content["revision"]),
                                 (first["created_at"], first["revision"]))
                rejected = await session.call_tool("edit_workspace_file", args | {"expected_sha256": "0" * 64})
                self.assertTrue(rejected.is_error)
                self.assertEqual(rejected.structured_content["error"]["code"], "change_id_conflict")
                self.assertEqual(rejected.structured_content["change_id"], change_id)
        self.stop(self.processes[-1])
        file.write_bytes(b"later third-party edit")
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("read_workspace_change", {
                    "machine": "desktop", "change_id": change_id, "observe_current": True})
                self.assertFalse(result.is_error, result)
                readback = result.structured_content
                self.assertEqual((readback["status"], readback["created_at"], readback["revision"]),
                                 ("applied", first["created_at"], first["revision"]))
                self.assertEqual(readback["current"]["sha256"], hashlib.sha256(file.read_bytes()).hexdigest())
                self.assertFalse(readback["historical_diff_available"])
                repeated = await session.call_tool("edit_workspace_file", args)
                self.assertEqual(repeated.structured_content["revision"], first["revision"])
                self.assertEqual(file.read_bytes(), b"later third-party edit")
        self.assertEqual(len(list((self.state / "workspace-changes").iterdir())), 1)
        self.assertFalse((self.state / "executions").exists())

    async def test_exact_edit_grant_schema_and_worst_case_json(self):
        self.stop(self.processes[-1])
        self.config.write_text(self.config.read_text().replace("execution_enabled = true", "execution_enabled = false\nworkspace_write_enabled = true"))
        self.launch()
        file = self.work / "escaped.txt"
        file.write_text("\x01" * 32760 + " 41")
        scope = {"machine": "desktop", "workspace_name": "example", "path": "escaped.txt"}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                read = (await session.call_tool("read_workspace_file", scope)).structured_content
                args = {**scope, "edits": [{"old_text": "41", "new_text": "42"}],
                        "expected_workspace_identity": read["context"]["workspace_identity"],
                        "expected_head_sha": "", "expected_sha256": read["sha256"]}
                for delta in ({"dry_run": "false"}, {"edits": [{"old_text": "41", "new_text": "42", "replace_all": True}]},
                              {"edits": [{"old_text": "", "new_text": "42"}]}, {"edits": []}):
                    bad = await session.call_tool("edit_workspace_file", args | {"change_id": uuid.uuid4().hex} | delta)
                    self.assertTrue(bad.is_error)
                    self.assertTrue(file.read_text().endswith("41"))
                identity = uuid.uuid4().hex
                result = await session.call_tool("edit_workspace_file", args | {"change_id": identity})
                self.assertFalse(result.is_error, result)
                encoded = json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()
                self.assertLessEqual(len(encoded), server.MAX_WORKSPACE_RESULT - 1024)
                self.assertEqual(result.structured_content["change_id"], identity)
                self.assertTrue(result.structured_content["truncated"])
                self.assertEqual(result.structured_content["effects"][0]["stage"], "applied")
                self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                denied = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": uuid.uuid4().hex,
                    "cwd": str(self.work), "argv": [sys.executable, "-c", "pass"]})
                self.assertEqual(denied.structured_content["error"]["code"], "execution_disabled")

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

    async def test_workspace_error_envelope_is_distinct_from_history_inspection(self):
        from openmachine.foundation.sanitize import Fault
        payload = {"change_id": "a" * 32, "status": "outcome_unknown", "revision": 3,
                   "effects": [{"stage": "applied", "result_sha256": "b" * 64}], "error": "record_unverified"}
        applied = server._reply(lambda: payload, workspace_output=True, workspace_mutation=True)
        inspected = server._reply(lambda: payload, workspace_output=True)
        self.assertTrue(applied.is_error)
        self.assertFalse(inspected.is_error)
        self.assertEqual(applied.structured_content["effects"], payload["effects"])
        def failed():
            raise Fault("change_outcome_unknown", "Original evidence only.", payload)
        fault = server._reply(failed, workspace_output=True, workspace_mutation=True)
        self.assertTrue(fault.is_error)
        self.assertEqual(fault.structured_content["change_id"], payload["change_id"])
        self.assertEqual(fault.structured_content["effects"], payload["effects"])
        abandoned = server._reply(lambda: {"status": "applying", "reconciliation": "outcome_unknown"},
                                   workspace_output=True, workspace_mutation=True)
        self.assertTrue(abandoned.is_error)

    async def test_large_line_window_small_edit_and_receipt_through_sdk(self):
        file = self.work / "large.py"
        original = b"padding\n" * 20000 + b"answer = 41\n"
        file.write_bytes(original)
        scope = {"machine": "desktop", "workspace_name": "example", "path": "large.py"}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                read = await session.call_tool("read_workspace_file", scope | {"start_line": 20001, "line_count": 1})
                self.assertFalse(read.is_error, read)
                page = read.structured_content
                self.assertEqual(page["text"], "answer = 41\n")
                self.assertGreater(page["offset"], 131072)
                byte_page = await session.call_tool("read_workspace_file", scope | {"offset": page["offset"], "expected_sha256": page["sha256"]})
                self.assertFalse(byte_page.is_error, byte_page)
                identity = uuid.uuid4().hex
                args = scope | {"edits": [{"old_text": "41", "new_text": "42"}],
                    "expected_workspace_identity": page["context"]["workspace_identity"],
                    "expected_head_sha": "", "expected_sha256": page["sha256"]}
                preview = await session.call_tool("edit_workspace_file", args | {"dry_run": True})
                self.assertFalse(preview.is_error, preview)
                self.assertEqual(file.read_bytes(), original)
                changed = await session.call_tool("edit_workspace_file", args | {"change_id": identity,
                    "expected_plan_sha256": preview.structured_content["plan_sha256"]})
                self.assertFalse(changed.is_error, changed)
                self.assertEqual(file.read_bytes(), original.replace(b"41", b"42"))
                receipt = await session.call_tool("read_workspace_change", {"machine": "desktop", "change_id": identity, "observe_current": True})
                self.assertFalse(receipt.is_error, receipt)
                self.assertEqual(receipt.structured_content["current"]["sha256"], changed.structured_content["plan"]["after_sha256"])
                for invalid in ({"start_line": True}, {"line_count": "2"}, {"line_count": 501}, {"start_line": 1, "offset": 1}):
                    result = await session.call_tool("read_workspace_file", scope | invalid)
                    self.assertTrue(result.is_error, invalid)

    async def test_bounded_search_pages_do_not_skip_captured_matches(self):
        file = self.work / "escaped.txt"
        file.write_text(("needle " + "\x01" * 315 + "\n") * 300)
        scope = {"machine": "desktop", "workspace_name": "example", "action": "search",
                 "query": "needle", "path_glob": "*.txt", "limit": 200}
        offset, lines, snapshot = 0, [], ""
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                while True:
                    result = await session.call_tool("inspect_workspace", scope | {"offset": offset, "expected_snapshot": snapshot})
                    self.assertFalse(result.is_error, result)
                    encoded = json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()
                    self.assertLessEqual(len(encoded), server.MAX_WORKSPACE_RESULT - 1024)
                    page = result.structured_content
                    self.assertEqual(json.loads(result.content[0].text), page)
                    lines.extend(item["line"] for item in page["matches"])
                    snapshot = page["snapshot"]
                    if not page["has_more"]:
                        break
                    self.assertEqual(page["next_offset"], offset + len(page["matches"]))
                    self.assertGreater(page["next_offset"], offset)
                    offset = page["next_offset"]
        self.assertEqual(lines, list(range(1, 301)))

    async def test_escaped_raw_read_pages_remain_bounded_and_lossless(self):
        file = self.work / "escaped.txt"
        original = "\x01" * 100000
        file.write_text(original)
        scope = {"machine": "desktop", "workspace_name": "example", "path": "escaped.txt"}
        offset, parts = 0, []
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                while True:
                    result = await session.call_tool("read_workspace_file", scope | {"offset": offset})
                    self.assertFalse(result.is_error, result)
                    self.assertLessEqual(len(json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()),
                                         server.MAX_WORKSPACE_RESULT - 1024)
                    page = result.structured_content
                    parts.append(page["text"])
                    if not page["has_more"]:
                        break
                    self.assertEqual(page["next_offset"], offset + len(page["text"].encode()))
                    offset = page["next_offset"]
        self.assertEqual("".join(parts), original)
