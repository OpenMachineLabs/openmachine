"""resilience tasks test_mcp_legacy_workflow behavior."""

import asyncio
import hashlib
import sys
import unittest
import uuid
from mcp import ClientSession
from test.support.mcp import MCPFixture
import json
import subprocess


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_large_explicit_payload_and_changed_instruction_cancellation(self):
        self.launch()
        scope = {"machine": "desktop", "workspace_name": "example"}
        identity = uuid.uuid4().hex
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                for name in ("save_task", "write_workspace_file"):
                    self.assertFalse(tools[name].annotations.read_only_hint)
                    self.assertTrue(tools[name].annotations.destructive_hint)
                    self.assertTrue(tools[name].annotations.idempotent_hint)
                    self.assertFalse(tools[name].annotations.open_world_hint)
                content = "\x01" * 131072
                write = await session.call_tool("write_workspace_file", {**scope, "path": "large.txt", "content": content,
                    "expected_head_sha": "", "expected_sha256": ""})
                self.assertFalse(write.is_error, write)
                self.assertEqual(write.structured_content["status"], "written")
                digest = hashlib.sha256(content.encode()).hexdigest()
                offset, pieces = 0, []
                while True:
                    page = (await session.call_tool("read_workspace_file", {**scope, "path": "large.txt",
                        "offset": offset, "expected_sha256": digest})).structured_content
                    self.assertLessEqual(len(page["text"].encode()), 32768)
                    pieces.append(page["text"])
                    offset = page["next_offset"]
                    if not page["has_more"]:
                        break
                self.assertEqual("".join(pieces), content)
                checkpoint = {"task_id": "changed-goal", "title": "Short cancellation fixture", "anchors": []}
                initial = await session.call_tool("save_task", {**checkpoint, "expected_revision": 0,
                    "body": "Goal: observe one short owned command.\nPlanned execution: " + identity + "\n"})
                self.assertFalse(initial.is_error, initial)
                start = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": identity,
                    "cwd": str(self.work), "argv": [sys.executable, "-c", "import time; time.sleep(20)"], "task_id": "changed-goal"})
                self.assertFalse(start.is_error, start)
                revised = await session.call_tool("save_task", {**checkpoint, "expected_revision": 1,
                    "body": "Changed fixture instruction: stop the original command.\nOriginal execution: " + identity + "\nNext: cancel and read termination.\n"})
                self.assertFalse(revised.is_error, revised)
                cancel = await session.call_tool("cancel_execution", {"machine": "desktop", "execution_id": identity})
                self.assertFalse(cancel.is_error, cancel)
                for _ in range(100):
                    receipt = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                    if receipt.structured_content["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(receipt.structured_content["status"], "cancelled")
                conflict = await session.call_tool("save_task", {**checkpoint, "expected_revision": 1, "body": "Stale progress."})
                self.assertTrue(conflict.is_error)
                self.assertEqual(conflict.structured_content["error"]["code"], "task_conflict")
                final = await session.call_tool("save_task", {**checkpoint, "expected_revision": 2,
                    "body": "Changed instruction retained. Original execution " + identity + " is cancelled. No replacement was started.\n"})
                self.assertEqual(final.structured_content["revision"], 3)
        self.assertEqual(len(list((self.state / "executions").iterdir())), 1)

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
