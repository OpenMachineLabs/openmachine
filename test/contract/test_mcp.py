"""Real SDK calls to an installed wheel over a private socket; no cloud account."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import openmachine


class MCPContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-mcp-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "AGENTS.md").write_text("Only modify this disposable project.\n")
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version = 1\n[machines.desktop]\ntransport = "local"\nexecution_enabled = true\n'
                               '[workspaces.example]\nmachine = "desktop"\nroot = ' + json.dumps(str(self.work)) + '\n'
                               '[github]\nrepositories = ["Example/repo"]\nattribution = false\n')
        self.state = self.root / "state"
        self.socket = self.state / "mcp.sock"
        self.processes = []
        self.addCleanup(self.stop_all)
        self.launch()

    def launch(self):
        process = subprocess.Popen([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                    "--state-dir", str(self.state), "serve"],
                                   cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            if self.socket.exists():
                return process
            time.sleep(0.03)
        self.fail("Private MCP socket was not created")

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stderr.close()

    def stop_all(self):
        for process in self.processes:
            self.stop(process)

    @asynccontextmanager
    async def client(self):
        async with httpx.AsyncClient(trust_env=False, timeout=10,
                                     transport=httpx.AsyncHTTPTransport(uds=str(self.socket))) as client:
            async with streamable_http_client("http://localhost/mcp", http_client=client) as streams:
                yield streams

    async def test_installed_package_discovery_and_socket_permissions(self):
        self.assertTrue(Path(openmachine.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                self.assertEqual(init.serverInfo.version, openmachine.__version__)
                tools = {t.name: t for t in (await session.list_tools()).tools}
                self.assertEqual(set(tools), {"get_capabilities", "inspect_workspace", "inspect_skills", "start_execution", "read_execution", "list_executions", "cancel_execution", "github_issue", "github_pull_request", "github_comment", "github_review", "read_workspace_file", "write_workspace_file", "find_tasks", "read_task", "save_task"})
                self.assertFalse(tools["start_execution"].annotations.readOnlyHint)
                self.assertTrue(tools["start_execution"].annotations.destructiveHint)
                self.assertTrue(tools["inspect_workspace"].annotations.readOnlyHint)
                self.assertFalse(tools["cancel_execution"].annotations.readOnlyHint)
                self.assertFalse(tools["github_issue"].annotations.readOnlyHint)
                timeout = tools["start_execution"].inputSchema["properties"]["timeout_seconds"]
                self.assertEqual((timeout["default"], timeout["maximum"]), (600, 86400))
                caps = (await session.call_tool("get_capabilities", {})).structuredContent
                self.assertEqual(caps["machines"]["desktop"]["probe_status"], "not_probed")
                self.assertEqual(caps["integrations"], ["github"])
                skill = await session.call_tool("inspect_skills", {"action": "read"})
                self.assertFalse(skill.isError)
                self.assertIn("Operate Open Machine", skill.structuredContent["text"])

    async def test_real_write_survives_service_restart_and_is_not_replayed(self):
        execution_id = uuid.uuid4().hex
        request = {"machine": "desktop", "execution_id": execution_id, "cwd": str(self.work),
                   "argv": [sys.executable, "-c", 'import time; from pathlib import Path; time.sleep(0.7); p=Path("result.txt"); p.write_text(p.read_text()+"done" if p.exists() else "done"); print("complete")']}
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                rules = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "read", "path": "AGENTS.md"})
                self.assertIn("disposable", rules.structuredContent["text"])
                started = await session.call_tool("start_execution", request)
                self.assertFalse(started.isError, started)
        self.stop(self.processes[0])
        self.assertFalse(self.socket.exists(), "Graceful shutdown must remove its own socket")
        self.launch()
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for _ in range(100):
                    result = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})
                    self.assertFalse(result.isError, result)
                    if result.structuredContent["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(result.structuredContent["exit_code"], 0)
                self.assertIn("complete", result.structuredContent["output"])
                again = await session.call_tool("start_execution", request)
                self.assertEqual(again.structuredContent["status"], "succeeded")
                changed = await session.call_tool("start_execution", {**request, "argv": ["false"]})
                self.assertTrue(changed.isError)
                self.assertEqual(changed.structuredContent["error"]["code"], "execution_conflict")
                observed = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                            "action": "read", "path": "result.txt"})
                self.assertEqual(observed.structuredContent["text"], "done")

    async def test_large_file_offsets_remain_usable_through_mcp(self):
        (self.work / "large.txt").write_text("x" * 180000)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "read", "path": "large.txt", "offset": 131072})
                self.assertFalse(result.isError, result)
                self.assertEqual(result.structuredContent["next_offset"], 180000)

    async def test_disabled_write_and_invalid_origin_fail_closed(self):
        self.stop(self.processes[0])
        self.config.write_text(self.config.read_text().replace("execution_enabled = true", "execution_enabled = false"))
        self.launch()
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": uuid.uuid4().hex,
                                                                      "cwd": str(self.work), "argv": ["false"]})
                self.assertTrue(result.isError)
                self.assertEqual(result.structuredContent["error"]["code"], "execution_disabled")
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(self.socket)), trust_env=False) as client:
            result = await client.get("http://localhost/mcp", headers={"host": "untrusted.invalid"})
            self.assertEqual(result.status_code, 421)

    async def test_invalid_tool_arguments_are_rejected_without_echoing_values(self):
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "password=synthetic-secret"})
                self.assertTrue(result.isError)
                self.assertEqual(result.structuredContent["error"]["code"], "invalid_tool_call")
                self.assertNotIn("synthetic-secret", result.model_dump_json())

    async def test_second_server_does_not_replace_existing_socket(self):
        inode = self.socket.stat().st_ino
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                 "--state-dir", str(self.state), "serve"], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.socket.stat().st_ino, inode)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                self.assertFalse((await session.call_tool("get_capabilities", {})).isError)


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
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                scope = {"machine": "desktop", "workspace_name": "example"}
                rules = await session.call_tool("inspect_workspace", {**scope, "action": "read", "path": "AGENTS.md"})
                self.assertIn("disposable", rules.structuredContent["text"])
                before = await session.call_tool("inspect_workspace", {**scope, "action": "status"})
                self.assertEqual(before.structuredContent["context"]["head_sha"], head)
                found = await session.call_tool("inspect_workspace", {**scope, "action": "search", "query": "return a - b"})
                self.assertEqual(found.structuredContent["matches"][0]["path"], "calculator.py")
                commands = [
                    [sys.executable, "-c", 'from pathlib import Path; p=Path("calculator.py"); s=p.read_text(); assert s.count("return a - b")==1; p.write_text(s.replace("return a - b", "return a + b"))'],
                    [sys.executable, "-m", "unittest", "discover", "-p", "test_calculator.py"],
                ]
                for argv in commands:
                    execution_id = uuid.uuid4().hex
                    request = {"machine": "desktop", "execution_id": execution_id, "cwd": str(self.work),
                               "argv": argv, "expected_head_sha": head}
                    started = await session.call_tool("start_execution", request)
                    self.assertFalse(started.isError, started)
                    for _ in range(100):
                        result = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})
                        self.assertFalse(result.isError, result)
                        if result.structuredContent["status"] in ("succeeded", "failed", "outcome_unknown"):
                            break
                        await asyncio.sleep(0.03)
                    self.assertEqual((result.structuredContent["status"], result.structuredContent["exit_code"]), ("succeeded", 0))
                self.assertIn("OK", result.structuredContent["output"])
                diff = await session.call_tool("inspect_workspace", {**scope, "action": "diff", "path": "calculator.py"})
                self.assertFalse(diff.isError, diff)
                self.assertIn("+    return a + b", diff.structuredContent["text"])
                self.assertFalse(diff.structuredContent["truncated"])
                readback = await session.call_tool("inspect_workspace", {**scope, "action": "read", "path": "calculator.py"})
                self.assertEqual(readback.structuredContent["text"], "def add(a, b):\n    return a + b\n")
                self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve this existing work\n")
                self.assertEqual(git("rev-parse", "HEAD"), head)

    async def test_reconnected_client_finds_and_cancels_original_execution(self):
        execution_id = uuid.uuid4().hex
        identity = {"machine": "desktop", "execution_id": execution_id}
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("start_execution", {**identity, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", 'import time; print("started", flush=True); time.sleep(20)'],
                        "timeout_seconds": 86400})
                self.assertFalse(result.isError, result)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = await session.call_tool("list_executions", {"machine": "desktop"})
                self.assertEqual(listing.structuredContent["executions"][0]["execution_id"], execution_id)
                requested = await session.call_tool("cancel_execution", identity)
                self.assertFalse(requested.isError, requested)
                self.assertTrue(requested.structuredContent["cancel_requested"])
                for _ in range(100):
                    result = await session.call_tool("read_execution", identity)
                    self.assertFalse(result.isError, result)
                    if result.structuredContent["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(result.structuredContent["status"], "cancelled")
                repeated = await session.call_tool("cancel_execution", identity)
                self.assertEqual(repeated.structuredContent["status"], "cancelled")


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
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = (await session.call_tool("inspect_skills", {})).structuredContent
                selected = next(item["skill"] for item in listing["skills"] if item["name"] == "repair")
                self.assertIn("Inspect the diff", (await session.call_tool("inspect_skills", {"action": "read", "skill": selected})).structuredContent["text"])
                original = (await session.call_tool("read_workspace_file", {**scope, "path": "calculator.py"})).structuredContent
                initial = await session.call_tool("save_task", {**checkpoint, "expected_revision": 0,
                    "body": "Goal: repair calculator in the disposable project.\nNext: make the authorized change.\n"})
                self.assertFalse(initial.isError, initial)
                changed = await session.call_tool("write_workspace_file", {**scope, "path": "calculator.py",
                    "content": "def add(a, b):\n    return a + b\n", "expected_head_sha": head,
                    "expected_sha256": original["sha256"]})
                self.assertEqual(changed.structuredContent["status"], "written")
                intent = await session.call_tool("save_task", {**checkpoint, "expected_revision": 1,
                    "body": "Completed: code repair.\nExecution: " + identity + "\nNext: run the project test once.\n"})
                self.assertFalse(intent.isError, intent)
                start = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": identity,
                    "cwd": str(self.work), "argv": [sys.executable, "-m", "unittest", "discover", "-p", "test_calculator.py"],
                    "expected_head_sha": head, "task_id": "resume-fixture"})
                self.assertFalse(start.isError, start)
                for _ in range(100):
                    receipt = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                    if receipt.structuredContent["status"] in ("succeeded", "failed", "outcome_unknown"):
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual((receipt.structuredContent["status"], receipt.structuredContent["exit_code"]), ("succeeded", 0))
                self.assertEqual(receipt.structuredContent["task_id"], "resume-fixture")
                result = await session.call_tool("save_task", {**checkpoint, "expected_revision": 2,
                    "body": "Completed: repair and passing tests.\nOriginal execution: " + identity + "\nNext: inspect diff only.\n"})
                self.assertFalse(result.isError, result)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                found = await session.call_tool("find_tasks", {"anchors": [anchor]})
                self.assertEqual(found.structuredContent["status"], "matched")
                checkpoint_read = await session.call_tool("read_task", {"task_id": "resume-fixture"})
                self.assertIn(identity, checkpoint_read.structuredContent["body"])
                observed = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                self.assertEqual(observed.structuredContent["status"], "succeeded")
                status = await session.call_tool("inspect_workspace", {**scope, "action": "status"})
                self.assertEqual(status.structuredContent["context"]["head_sha"], head)
                diff = await session.call_tool("inspect_workspace", {**scope, "action": "diff", "path": "calculator.py"})
                self.assertIn("+    return a + b", diff.structuredContent["text"])
                latest = await session.call_tool("read_workspace_file", {**scope, "path": "calculator.py"})
                self.assertEqual(latest.structuredContent["sha256"], changed.structuredContent["sha256"])
                final = await session.call_tool("save_task", {**checkpoint, "expected_revision": 3,
                    "body": "Completed: repair, original passing tests, unchanged source, and final diff review.\nNo further action.\n"})
                self.assertEqual(final.structuredContent["revision"], 4)
        self.assertEqual((self.work / "test_runs.txt").read_text(), "1")
        self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve existing work\n")
        self.assertEqual(len(list((self.state / "executions").iterdir())), 1)

    async def test_large_explicit_payload_and_changed_instruction_cancellation(self):
        scope = {"machine": "desktop", "workspace_name": "example"}
        identity = uuid.uuid4().hex
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                for name in ("save_task", "write_workspace_file"):
                    self.assertFalse(tools[name].annotations.readOnlyHint)
                    self.assertTrue(tools[name].annotations.destructiveHint)
                    self.assertTrue(tools[name].annotations.idempotentHint)
                    self.assertFalse(tools[name].annotations.openWorldHint)
                content = "\x01" * 131072
                write = await session.call_tool("write_workspace_file", {**scope, "path": "large.txt", "content": content,
                    "expected_head_sha": "", "expected_sha256": ""})
                self.assertFalse(write.isError, write)
                self.assertEqual(write.structuredContent["status"], "written")
                digest = hashlib.sha256(content.encode()).hexdigest()
                offset, pieces = 0, []
                while True:
                    page = (await session.call_tool("read_workspace_file", {**scope, "path": "large.txt",
                        "offset": offset, "expected_sha256": digest})).structuredContent
                    self.assertLessEqual(len(page["text"].encode()), 32768)
                    pieces.append(page["text"])
                    offset = page["next_offset"]
                    if not page["has_more"]:
                        break
                self.assertEqual("".join(pieces), content)
                checkpoint = {"task_id": "changed-goal", "title": "Short cancellation fixture", "anchors": []}
                initial = await session.call_tool("save_task", {**checkpoint, "expected_revision": 0,
                    "body": "Goal: observe one short owned command.\nPlanned execution: " + identity + "\n"})
                self.assertFalse(initial.isError, initial)
                start = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": identity,
                    "cwd": str(self.work), "argv": [sys.executable, "-c", "import time; time.sleep(20)"], "task_id": "changed-goal"})
                self.assertFalse(start.isError, start)
                revised = await session.call_tool("save_task", {**checkpoint, "expected_revision": 1,
                    "body": "Changed fixture instruction: stop the original command.\nOriginal execution: " + identity + "\nNext: cancel and read termination.\n"})
                self.assertFalse(revised.isError, revised)
                cancel = await session.call_tool("cancel_execution", {"machine": "desktop", "execution_id": identity})
                self.assertFalse(cancel.isError, cancel)
                for _ in range(100):
                    receipt = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                    if receipt.structuredContent["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(receipt.structuredContent["status"], "cancelled")
                conflict = await session.call_tool("save_task", {**checkpoint, "expected_revision": 1, "body": "Stale progress."})
                self.assertTrue(conflict.isError)
                self.assertEqual(conflict.structuredContent["error"]["code"], "task_conflict")
                final = await session.call_tool("save_task", {**checkpoint, "expected_revision": 2,
                    "body": "Changed instruction retained. Original execution " + identity + " is cancelled. No replacement was started.\n"})
                self.assertEqual(final.structuredContent["revision"], 3)
        self.assertEqual(len(list((self.state / "executions").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
