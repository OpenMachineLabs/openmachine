"""Real SDK calls to an installed wheel over a private socket; no cloud account."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import shutil
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ListToolsResult

import openmachine
from openmachine import execution, server
from openmachine.config import Settings
from openmachine.foundation.state import lease


class MCPContractTests(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

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

    def launch(self, environment=None, *, managed=False):
        process = subprocess.Popen([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                    "--state-dir", str(self.state), "serve", *(["--managed"] if managed else [])],
                                   cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=environment)
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            if self.socket.exists():
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                        connection.settimeout(0.2)
                        connection.connect(str(self.socket))
                    return process
                except OSError:
                    pass
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

    async def test_grace_schema_rejects_invalid_values_before_admission(self):
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for value in (True, False, "5", -1, 31):
                    identity = uuid.uuid4().hex
                    result = await session.call_tool("start_execution", {
                        "machine": "desktop", "execution_id": identity, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "termination_grace_seconds": value})
                    self.assertTrue(result.isError, value)
                    self.assertFalse((self.state / "executions" / identity).exists())
                caps = (await session.call_tool("get_capabilities", {})).structuredContent
                self.assertEqual(caps["execution"]["receipt_format"], 5)
                self.assertEqual(caps["execution"]["io_modes"], ["pipe", "pty"])
                self.assertEqual(caps["execution"]["controls"], ["interrupt", "resize", "write_stdin"])

    def assert_tool_contract(self, result: ListToolsResult, *, github: bool, ssh: bool):
        expected = json.loads(Path(__file__).with_name("mcp_contract.json").read_text())["tools"]
        if not github:
            expected = {name: tool for name, tool in expected.items() if not name.startswith("github_")}
        # Only these six annotations vary with configured SSH; never normalize actual results.
        for name in ("inspect_workspace", "read_workspace_file", "write_workspace_file",
                     "read_execution", "list_executions", "control_execution"):
            expected[name]["annotations"]["openWorldHint"] = ssh
        self.assertIsNone(result.nextCursor, "The baseline tool catalog is a single complete page")
        actual = {tool.name: tool for tool in result.tools}
        self.assertEqual(len(result.tools), len(actual), "Duplicate tool names in discovery")
        self.assertEqual(set(actual), set(expected), "Configured MCP tool names changed")
        for name, contract in expected.items():
            observed = actual[name].model_dump(mode="json", by_alias=True, exclude_none=True,
                                              include={"inputSchema", "outputSchema", "annotations"})
            # JSON comparison preserves scalar types: a false default must not equal zero.
            self.assertEqual(json.dumps(observed, indent=2, sort_keys=True),
                             json.dumps(contract, indent=2, sort_keys=True),
                             f"{name}: MCP contract changed; inspect before updating the baseline")

    async def test_tool_contract_without_github(self):
        self.stop(self.processes[0])
        local = self.config.read_text().split("[github]")[0]
        for ssh in (False, True):
            with self.subTest(ssh=ssh):
                config = local
                if ssh:
                    config += ('\n[machines.build]\ntransport="ssh"\nssh_alias="uncontacted-host"\n'
                               'ssh_worker="/remote/' + 'a' * 64 + '.pyz"\n'
                               '[workspaces.remote]\nmachine="build"\nroot="/remote/not-on-controller"\n')
                self.config.write_text(config)
                process = self.launch()
                try:
                    async with self.client() as (reader, writer, _):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            self.assert_tool_contract(await session.list_tools(), github=False, ssh=ssh)
                            caps = (await session.call_tool("get_capabilities", {})).structuredContent
                            self.assertEqual(caps["integrations"], ["ssh"] if ssh else [])
                finally:
                    self.stop(process)

    async def test_ssh_metadata_and_local_independence(self):
        self.stop(self.processes[0])
        with self.config.open("a") as output:
            output.write('\n[machines.build]\ntransport="ssh"\nssh_alias="uncontacted-host"\n'
                         'ssh_worker="/remote/' + 'a' * 64 + '.pyz"\n'
                         '[workspaces.remote]\nmachine="build"\nroot="/remote/not-on-controller"\n')
        self.launch()
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                discovered = await session.list_tools()
                self.assert_tool_contract(discovered, github=True, ssh=True)
                tools = {item.name: item for item in discovered.tools}
                self.assertTrue(tools["read_workspace_file"].annotations.openWorldHint)
                self.assertTrue(tools["write_workspace_file"].annotations.openWorldHint)
                self.assertFalse(tools["save_task"].annotations.openWorldHint)
                caps = (await session.call_tool("get_capabilities", {})).structuredContent
                self.assertIn("ssh", caps["integrations"])
                denied = await session.call_tool("read_workspace_file", {"machine": "build", "workspace_name": "remote", "path": "x"})
                self.assertEqual(denied.structuredContent["error"]["code"], "identity_required")
                local = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example", "action": "read", "path": "AGENTS.md"})
                self.assertFalse(local.isError)

    async def test_installed_package_discovery_and_socket_permissions(self):
        self.assertTrue(Path(openmachine.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                self.assertEqual(init.serverInfo.version, openmachine.__version__)
                discovered = await session.list_tools()
                self.assert_tool_contract(discovered, github=True, ssh=False)
                tools = {t.name: t for t in discovered.tools}
                self.assertEqual(set(tools), {"get_capabilities", "inspect_workspace", "inspect_skills", "start_execution", "read_execution", "list_executions", "cancel_execution", "control_execution", "github_issue", "github_pull_request", "github_comment", "github_review", "github_read", "github_merge", "read_workspace_file", "write_workspace_file", "find_tasks", "read_task", "save_task"})
                self.assertFalse(tools["start_execution"].annotations.readOnlyHint)
                self.assertTrue(tools["start_execution"].annotations.destructiveHint)
                self.assertTrue(tools["inspect_workspace"].annotations.readOnlyHint)
                self.assertFalse(tools["cancel_execution"].annotations.readOnlyHint)
                self.assertFalse(tools["control_execution"].annotations.readOnlyHint)
                self.assertFalse(tools["control_execution"].annotations.idempotentHint)
                self.assertFalse(tools["github_issue"].annotations.readOnlyHint)
                self.assertTrue(tools["github_read"].annotations.readOnlyHint)
                self.assertTrue(tools["github_read"].annotations.openWorldHint)
                for name in ("github_issue", "github_pull_request", "github_comment", "github_review", "github_merge"):
                    self.assertFalse(tools[name].annotations.idempotentHint)
                self.assertNotIn("endpoint", tools["github_read"].inputSchema["properties"])
                timeout = tools["start_execution"].inputSchema["properties"]["timeout_seconds"]
                self.assertEqual((timeout["default"], timeout["maximum"]), (600, 86400))
                caps = (await session.call_tool("get_capabilities", {})).structuredContent
                self.assertEqual(caps["machines"]["desktop"]["probe_status"], "not_probed")
                self.assertEqual(caps["integrations"], ["github"])
                skill = await session.call_tool("inspect_skills", {"action": "read"})
                self.assertFalse(skill.isError)
                self.assertIn("Operate Open Machine", skill.structuredContent["text"])

    async def test_github_scope_rejects_before_connection(self):
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for name, arguments in (
                    ("github_read", {"action": "repository", "repository": "Other/repo"}),
                    ("github_issue", {"action": "create", "repository": "Other/repo", "title": "not submitted"}),
                    ("github_merge", {"repository": "Other/repo", "number": 1, "expected_head_sha": "a" * 40}),
                ):
                    result = await session.call_tool(name, arguments)
                    self.assertTrue(result.isError)
                    self.assertEqual(result.structuredContent["error"]["code"], "github_not_allowed")

    async def test_missing_gh_and_auth_do_not_break_local_execution(self):
        self.stop(self.processes[-1])
        binaries = self.root / "without-gh"
        binaries.mkdir()
        for name in ("git", "ps"):
            source = shutil.which(name)
            if source:
                (binaries / name).symlink_to(source)
        environment = dict(os.environ, PATH=str(binaries))
        for code in ("github_unavailable", "github_authentication_required"):
            if code == "github_authentication_required":
                command = binaries / "gh"
                command.write_text("#!/bin/sh\nprintf 'gh: Bad credentials (HTTP 401)\\n' >&2\nexit 1\n")
                command.chmod(0o700)
            process = self.launch(environment)
            async with self.client() as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    unavailable = await session.call_tool("github_read", {"action": "status"})
                    self.assertTrue(unavailable.isError)
                    self.assertEqual(unavailable.structuredContent["error"]["code"], code)
                    identity = uuid.uuid4().hex
                    started = await session.call_tool("start_execution", {
                        "machine": "desktop", "execution_id": identity, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", "print('local-still-usable')"],
                    })
                    self.assertFalse(started.isError, started)
                    for _ in range(100):
                        observed = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                        self.assertFalse(observed.isError, observed)
                        if observed.structuredContent["status"] == "succeeded":
                            break
                        await asyncio.sleep(0.03)
                    self.assertEqual(observed.structuredContent["exit_code"], 0)
                    self.assertIn("local-still-usable", observed.structuredContent["output"])
            self.stop(process)

    async def test_github_typed_boundaries_before_dispatch(self):
        self.stop(self.processes[-1])
        binaries = self.root / "synthetic-gh"
        binaries.mkdir()
        calls = self.root / "github-calls.jsonl"
        responses = {"user": {"id": 1, "login": "Operator"},
                     "repos/Example/repo": {"id": 1, "full_name": "Example/repo"},
                     "repos/Example/repo/issues/2": {"id": 2, "number": 2, "pull_request": {}}}
        command = binaries / "gh"
        command.write_text(f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
                           "method = sys.argv[sys.argv.index('--method') + 1]\n"
                           f"with Path({str(calls)!r}).open('a') as log:\n"
                           "    log.write(json.dumps([method, sys.argv[-1]]) + '\\n')\n"
                           "assert method == 'GET', 'Unexpected mutation'\n"
                           f"print(json.dumps({responses!r}[sys.argv[-1]]))\n")
        command.chmod(0o700)
        self.launch(dict(os.environ, PATH=str(binaries)))
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("github_issue", {
                    "action": "update", "repository": "Example/repo", "number": 2, "state": "closed"})
                self.assertTrue(result.isError)
                self.assertEqual(result.structuredContent["error"]["code"], "github_wrong_object_type")
                for path in (".ENV", "Secrets.toml", "ID_RSA", "config/.EnV.local"):
                    result = await session.call_tool("github_read", {
                        "action": "file", "repository": "Example/repo", "ref": "a" * 40, "path": path})
                    self.assertTrue(result.isError)
                    self.assertEqual(result.structuredContent["error"]["code"], "invalid_github_request")
        observed = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertTrue(observed)
        self.assertTrue(all(method == "GET" and endpoint in responses for method, endpoint in observed))

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


    async def test_lifecycle_readiness_and_mutation_gate(self):
        from openmachine import execution, service
        from openmachine.foundation.sanitize import Fault
        probe = {"workspace": "example", "path": "AGENTS.md"}
        ready = await asyncio.to_thread(service.probe_service, self.state, self.config, probe,
                                        openmachine.__version__, self.processes[-1].pid)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["tunnel"], "not_probed")
        self.assertEqual(ready["chatgpt"], "not_probed")
        self.assertNotIn("text", ready)
        self.assertEqual(ready["file_sha256"], hashlib.sha256((self.work / "AGENTS.md").read_bytes()).hexdigest())
        with self.assertRaises(Fault) as failed:
            await asyncio.to_thread(service.probe_service, self.state, self.config, probe,
                                    openmachine.__version__, self.processes[-1].pid + 1)
        self.assertEqual(failed.exception.code, "service_identity_mismatch")
        with self.assertRaises(Fault) as failed:
            await asyncio.to_thread(service.probe_service, self.state, self.config,
                                    {**probe, "path": "absent.txt"}, openmachine.__version__)
        self.assertEqual(failed.exception.code, "service_read_unavailable")
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                request = {"task_id": "transition-guard", "title": "Transition guard",
                           "expected_revision": 0, "body": "One authorized fixture checkpoint.", "anchors": []}
                with lease(self.state / "locks/lifecycle.lock"):
                    denied = await session.call_tool("save_task", request)
                    self.assertTrue(denied.isError)
                    self.assertEqual(denied.structuredContent["error"]["code"], "resource_busy")
                    self.assertFalse((await session.call_tool("get_capabilities", {})).isError)
                    self.assertFalse((self.state / "tasks").exists())
                saved = await session.call_tool("save_task", request)
                self.assertFalse(saved.isError, saved)
                self.assertEqual(saved.structuredContent["revision"], 1)

    async def test_managed_stale_socket_recovery_preserves_live_owner(self):
        # Leave this owned listener's socket behind without depending on graceful
        # shutdown timing or trying to bind over a runner's existing stale path.
        self.processes[0].kill()
        self.processes[0].wait(timeout=5)
        self.assertTrue(self.socket.exists())
        self.launch(managed=True)
        inode = self.socket.stat().st_ino
        duplicate = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                    "--state-dir", str(self.state), "serve", "--managed"],
                                   capture_output=True, timeout=10)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(self.socket.stat().st_ino, inode)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                caps = await session.call_tool("get_capabilities", {})
                self.assertEqual(caps.structuredContent["lifecycle"]["pid"], self.processes[-1].pid)



    async def test_wait_schema_and_quiet_expiry_keep_original_execution(self):
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for value in (True, False, "1", -0.1, 5.1):
                    result = await session.call_tool("read_execution", identity | {"wait_seconds": value})
                    self.assertTrue(result.isError)
                    self.assertEqual(result.structuredContent["error"]["code"], "invalid_tool_call")
                caps = (await session.call_tool("get_capabilities", {})).structuredContent
                self.assertEqual(caps["execution"]["wait_seconds"], {"default": 0, "min": 0, "max": 5})
                self.assertEqual(caps["execution"]["max_waiting_readers"], 16)
                started = await session.call_tool("start_execution", identity | {
                    "cwd": str(self.work), "argv": [sys.executable, "-c", 'import time; print("ready",flush=True); time.sleep(10)'],
                    "timeout_seconds": 15})
                self.assertFalse(started.isError)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        ready = (await session.call_tool("read_execution", identity | {"wait_seconds": 1})).structuredContent
                        if "ready" in ready.get("output", ""):
                            break
                    self.assertIn("ready", ready["output"])
                    path = self.state / "executions" / identity["execution_id"] / "record.json"
                    original = path.read_bytes()
                    observed = await session.call_tool("read_execution", identity | {"offset": ready["next_offset"], "wait_seconds": 0.1})
                    self.assertFalse(observed.isError)
                    self.assertTrue(observed.structuredContent["wait_expired"])
                    self.assertEqual(observed.structuredContent["status"], "running")
                    self.assertEqual(path.read_bytes(), original)
                finally:
                    await session.call_tool("cancel_execution", identity)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        result = (await session.call_tool("read_execution", identity | {"wait_seconds": 1})).structuredContent
                        if result.get("status") == "cancelled":
                            break
                    self.assertEqual(result["status"], "cancelled")

    async def test_disconnected_waiter_preserves_command_and_other_operations(self):
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        gate = self.work / "emit"
        code = ('import time; from pathlib import Path; p=Path("counter"); p.write_text("once"); print("ready",flush=True)\n'
                'while not Path("emit").exists(): time.sleep(.01)\n'
                'print("continued",flush=True)')
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as control:
                await control.initialize()
                started = await control.call_tool("start_execution", identity | {
                    "cwd": str(self.work), "argv": [sys.executable, "-c", code], "timeout_seconds": 15})
                self.assertFalse(started.isError)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        ready = (await control.call_tool("read_execution", identity | {"wait_seconds": 1})).structuredContent
                        if "ready" in ready.get("output", ""):
                            break
                    self.assertIn("ready", ready["output"])
                    async with self.client() as (other_reader, other_writer, _):
                        async with ClientSession(other_reader, other_writer) as other:
                            await other.initialize()
                            waiting = asyncio.create_task(other.call_tool("read_execution", identity | {
                                "offset": ready["next_offset"], "wait_seconds": 5}))
                            await asyncio.sleep(0.1)
                            self.assertFalse(waiting.done())
                            caps = await asyncio.wait_for(control.call_tool("get_capabilities", {}), 2)
                            self.assertFalse(caps.isError)
                            now = await asyncio.wait_for(control.call_tool("read_execution", identity), 2)
                            self.assertEqual(now.structuredContent["status"], "running")
                            waiting.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await waiting
                    # A new connection observes the original worker rather than a replacement.
                    async with self.client() as (fresh_reader, fresh_writer, _):
                        async with ClientSession(fresh_reader, fresh_writer) as fresh:
                            await fresh.initialize()
                            observed = await fresh.call_tool("read_execution", identity)
                            self.assertEqual(observed.structuredContent["created_at"], started.structuredContent["created_at"])
                            self.assertFalse(observed.structuredContent["cancel_requested"])
                            self.assertEqual((self.work / "counter").read_text(), "once")
                            gate.touch()
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline:
                                result = (await fresh.call_tool("read_execution", identity | {"wait_seconds": 1})).structuredContent
                                if result.get("status") == "succeeded":
                                    break
                            self.assertEqual((result["status"], result["exit_code"]), ("succeeded", 0))
                            self.assertIn("continued", result["output"])
                finally:
                    gate.touch()
                    await control.call_tool("cancel_execution", identity)


    async def execution_until(self, session, identity, predicate):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            response = await session.call_tool("read_execution", identity | {"wait_seconds": 0.25})
            self.assertFalse(response.isError)
            observed = response.structuredContent
            if predicate(observed):
                return observed
            await asyncio.sleep(0.02)
        self.fail("The original SDK execution did not reach the required observation")

    async def test_interactive_worker_continues_across_real_sdk_connections_without_replay(self):
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        code = ('import time; from pathlib import Path; p=Path("start-count"); '
                'p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); '
                'print("ready",flush=True); x=input(); print("first-read",flush=True); '
                'y=input(); print("same-process",x,y,flush=True); time.sleep(8)')
        try:
            async with self.client() as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    started = await session.call_tool("start_execution", identity | {
                        "cwd": str(self.work), "argv": [sys.executable, "-c", code], "timeout_seconds": 15, "tty": True})
                    self.assertFalse(started.isError)
                    ready = await self.execution_until(session, identity, lambda r: r["control_ready"] and "ready" in r["output"])
                    sent = await session.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "write_stdin", "text": "41\n"})
                    self.assertFalse(sent.isError)
                    receipt = sent.structuredContent["control_result"]
                    self.assertEqual(receipt["status"], "applied")
                    self.assertNotIn("output", sent.structuredContent)
                    await self.execution_until(session, identity, lambda r: "first-read" in r["output"])
            async with self.client() as (reader, writer, _):
                async with ClientSession(reader, writer) as fresh:
                    await fresh.initialize()
                    observed = await self.execution_until(fresh, identity, lambda r: r["control"]["seq"] == 1)
                    self.assertEqual(observed["created_at"], ready["created_at"])
                    duplicate = await fresh.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "write_stdin", "text": "41\n"})
                    self.assertFalse(duplicate.isError)
                    self.assertEqual(duplicate.structuredContent["control_result"], receipt)
                    second = await fresh.call_tool("control_execution", identity | {
                        "expected_control_seq": 1, "action": "write_stdin", "text": "1\n"})
                    self.assertFalse(second.isError)
                    await self.execution_until(fresh, identity, lambda r: "same-process 41 1" in r["output"])
                    self.assertEqual((self.work / "start-count").read_text(), "1")
                    self.assertFalse((await fresh.call_tool("cancel_execution", identity)).isError)
                    final = await self.execution_until(fresh, identity, lambda r: r["status"] == "cancelled")
                    self.assertEqual(final["cleanup"]["status"], "verified")
                    self.assertEqual(final["control"]["seq"], 2)
        finally:
            async with self.client() as (reader, writer, _):
                async with ClientSession(reader, writer) as cleanup:
                    await cleanup.initialize()
                    await cleanup.call_tool("cancel_execution", identity)

    async def test_interactive_sdk_schema_rejects_invalid_effects_before_admission(self):
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for delta in ({"tty": 1}, {"tty": "true"}, {"tty": True, "terminal_size": {"rows": True, "cols": 80}},
                              {"tty": True, "terminal_size": {"rows": 24, "cols": 80, "extra": 1}},
                              {"terminal_size": {"rows": 24, "cols": 80}}):
                    identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
                    result = await session.call_tool("start_execution", identity | {
                        "cwd": str(self.work), "argv": [sys.executable, "-c", "pass"]} | delta)
                    self.assertTrue(result.isError)
                    self.assertFalse((self.state / "executions" / identity["execution_id"]).exists())
                identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
                for delta in ({"expected_control_seq": True}, {"expected_control_seq": 2**53 - 1},
                              {"action": "close_stdin"}, {"action": "write_stdin", "text": "\0"},
                              {"action": "write_stdin", "text": "é" * 8193},
                              {"action": "resize", "terminal_size": {"rows": 1, "cols": 1001}}):
                    result = await session.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "interrupt"} | delta)
                    self.assertTrue(result.isError)
                    self.assertNotEqual(result.structuredContent["error"]["code"], "execution_not_found")
                self.assertFalse((self.state / "executions" / identity["execution_id"]).exists())



class ObservationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_cap_and_cancelled_await_retain_the_actual_thread_slot(self):
        with tempfile.TemporaryDirectory(prefix="om-read-limit-", dir="/tmp") as directory:
            root = Path(directory)
            config = Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}},
                                              "workspaces": {"work": {"machine": "local", "root": str(root)}}})
            application = Mock()
            application.get_capabilities.return_value = {"alive": True}
            application.cancel_execution.return_value = {"cancel_requested": True}
            application.control_execution.return_value = {"control_result": {"status": "applied"}}
            admitted = threading.Event()
            release = threading.Event()
            counter_lock = threading.Lock()
            calls = []

            def read(machine, identity, offset, size, wait_seconds, *, cancelled):
                if wait_seconds:
                    with counter_lock:
                        calls.append(cancelled)
                        if len(calls) == 16:
                            admitted.set()
                    if not release.wait(5):
                        raise AssertionError("The test did not release its admitted observers")
                return {"status": "running", "wait_expired": bool(wait_seconds)}

            application.read_execution.side_effect = read
            with patch.object(server, "create_application", return_value=application):
                mcp = server.create_server(config, root / "state")
            args = {"machine": "local", "execution_id": "a" * 32, "wait_seconds": 5}
            with patch.object(server, "ThreadPoolExecutor", side_effect=OSError("fixture unavailable")):
                with self.assertRaises(ToolError):
                    await mcp.call_tool("read_execution", args)
            readers = [asyncio.create_task(mcp.call_tool("read_execution", args)) for _ in range(16)]
            try:
                self.assertTrue(await asyncio.to_thread(admitted.wait, 3))
                overflow = await mcp.call_tool("read_execution", args)
                self.assertEqual(overflow.structuredContent["error"]["code"], "reader_busy")
                self.assertFalse((await mcp.call_tool("get_capabilities", {})).isError)
                self.assertFalse((await mcp.call_tool("read_execution", args | {"wait_seconds": 0})).isError)
                self.assertFalse((await mcp.call_tool("cancel_execution", {"machine": "local", "execution_id": "a" * 32})).isError)
                readers[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await readers[0]
                self.assertTrue(any(event.is_set() for event in calls))
                # The fake observer intentionally has not exited yet. Its slot
                # cannot be reused just because the asyncio await was cancelled.
                overflow = await mcp.call_tool("read_execution", args)
                self.assertEqual(overflow.structuredContent["error"]["code"], "reader_busy")
            finally:
                release.set()
                await asyncio.gather(*readers, return_exceptions=True)
            result = await mcp.call_tool("read_execution", args)
            self.assertFalse(result.isError)

    async def test_cancellation_has_capacity_when_immediate_reads_fill_default_executor(self):
        with tempfile.TemporaryDirectory(prefix="om-cancel-capacity-", dir="/tmp") as directory:
            root = Path(directory)
            config = Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}},
                                              "workspaces": {"work": {"machine": "local", "root": str(root)}}})
            application = Mock()
            release = threading.Event()
            filled = threading.Event()
            lock = threading.Lock()
            count = 0

            def blocked_read(*args, **kwargs):
                nonlocal count
                with lock:
                    count += 1
                    if count == 2:
                        filled.set()
                if not release.wait(5):
                    raise AssertionError("The test did not release its immediate readers")
                return {"status": "running"}

            application.read_execution.side_effect = blocked_read
            application.cancel_execution.return_value = {"cancel_requested": True}
            application.control_execution.return_value = {"control_result": {"status": "applied"}}
            with patch.object(server, "create_application", return_value=application):
                mcp = server.create_server(config, root / "state")
            asyncio.get_running_loop().set_default_executor(server.ThreadPoolExecutor(max_workers=2))
            args = {"machine": "local", "execution_id": "b" * 32}
            readers = [asyncio.create_task(mcp.call_tool("read_execution", args)) for _ in range(2)]
            try:
                deadline = time.monotonic() + 3
                while not filled.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(filled.is_set())
                result = await asyncio.wait_for(mcp.call_tool("cancel_execution", args), 1)
                self.assertFalse(result.isError)
                self.assertTrue(result.structuredContent["cancel_requested"])
                application.cancel_execution.assert_called_once_with("local", "b" * 32)
                controlled = await asyncio.wait_for(mcp.call_tool("control_execution", args | {"expected_control_seq": 0, "action": "interrupt"}), 1)
                self.assertEqual(controlled.structuredContent["control_result"]["status"], "applied")
                application.control_execution.assert_called_once_with("local", "b" * 32, 0, "interrupt", None, None)
                self.assertFalse(any(task.done() for task in readers))
            finally:
                release.set()
                await asyncio.gather(*readers, return_exceptions=True)

    async def test_execution_reply_counts_both_json_representations(self):
        payload = {"output": "\ufffd" * 200000, "offset": 0, "next_offset": 200000,
                   "output_presentation_truncated": False}
        result = server._reply(lambda: payload, execution_output=True)
        encoded = json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()
        self.assertFalse(result.isError)
        self.assertLessEqual(len(encoded), server.MAX_RESULT_BYTES - 1024)
        self.assertTrue(result.structuredContent["output_presentation_truncated"])
        self.assertEqual(result.structuredContent["next_offset"], 200000)
        self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
        self.assertFalse(payload["output_presentation_truncated"])


if __name__ == "__main__":
    unittest.main()
