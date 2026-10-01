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

import httpx2 as httpx
from mcp import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError
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

    def task_request(self, context, identity):
        return {"task_id": identity, "update_id": "1" * 32, "title": "Retain SDK fixture", "anchors": ["fixture=s11"],
            "origin": {"request_ref": "fixture:request", "request_sha256": "a" * 64, "objective": "Retain declared work."},
            "contract": {"objective": "Retain declared work.", "scope": "Disposable SDK fixture.", "exclusions": "No production effects.",
                "acceptance": "Read the same task after restart.", "stop_condition": "Stop after readback.",
                "authority_refs": ["fixture:consent"], "targets": [{"machine": "desktop", "workspace": "example",
                    "workspace_identity": context["workspace_identity"], "head_sha": context.get("head_sha") or None}],
                "repositories": [], "tools": ["read_task"], "sources": [],
                "expires_at_ms": time.time_ns() // 1000000 + 3600000},
            "stages": [{"id": "verify", "depends_on": [], "result": "Retained task", "verification": "Exact readback",
                        "source_refs": [], "source_sha256": hashlib.sha256(b"[]\n").hexdigest()}]}

    async def test_canonical_tasks_survive_real_server_restart_without_dispatch(self):
        retained = {}
        for mode in ("legacy", "2026-07-28"):
            async with Client(self.client(), mode=mode, cache=None) as client:
                scope = await client.call_tool("read_workspace_file", {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"})
                identity = "canonical-" + mode
                request = self.task_request(scope.structured_content["context"], identity)
                created = await client.call_tool("create_task", request)
                self.assertFalse(created.is_error, created)
                record = created.structured_content["record"]
                retained[identity] = record
                repeated = await client.call_tool("create_task", request)
                self.assertEqual(repeated.structured_content["record"], record)
                self.assertEqual(repeated.structured_content["status"], "unchanged")
                changed = await client.call_tool("create_task", request | {"title": "Conflicting declaration"})
                self.assertEqual(changed.structured_content["error"]["code"], "task_conflict")
                execution_id = uuid.uuid4().hex
                denied = await client.call_tool("start_execution", {"machine": "desktop", "execution_id": execution_id,
                    "cwd": str(self.work), "argv": [sys.executable, "-c", "raise SystemExit(0)"], "task_id": identity})
                self.assertEqual(denied.structured_content["error"]["code"], "supervised_admission_unavailable")
                self.assertFalse((self.state / "executions" / execution_id).exists())
                index = self.state / "tasks/index.json"
                original = index.read_bytes()
                index.write_text("Interrupted derived index fixture")
                read = await client.call_tool("read_task", {"task_id": identity})
                self.assertFalse(read.is_error, read)
                self.assertEqual(read.structured_content["record"], record)
                self.assertEqual(read.structured_content["index_status"], "stale")
                self.assertEqual(index.read_text(), "Interrupted derived index fixture")
                index.write_bytes(original)
                caps = (await client.call_tool("get_capabilities")).structured_content
                self.assertEqual(caps["lifecycle"]["state_contract"], 6)
                self.assertTrue(caps["tasks"]["structured_records"])
                self.assertFalse(caps["tasks"]["supervised_admission"])
                self.assertFalse(caps["tasks"]["events_delivery"])
        snapshots = {identity: (self.state / "tasks" / identity / "task.json").read_bytes() for identity in retained}
        self.stop(self.processes[-1])
        self.launch()
        async with Client(self.client(), mode="2026-07-28", cache=None) as client:
            found = (await client.call_tool("find_tasks", {"anchors": ["fixture=s11"]})).structured_content
            self.assertEqual(found["coverage"], "complete")
            self.assertEqual({item["task_id"] for item in found["tasks"]}, set(retained))
            for identity, record in retained.items():
                read = (await client.call_tool("read_task", {"task_id": identity})).structured_content
                self.assertEqual(read["record"], record)
                self.assertEqual(read["observations"][0]["state"], "unknown")
                self.assertEqual((self.state / "tasks" / identity / "task.json").read_bytes(), snapshots[identity])

    async def test_canonical_contract_rejects_runtime_fields_and_invalid_bindings_on_both_protocols(self):
        import copy
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                response = await self.wire_call(client, "tools/call", {"name": "read_workspace_file", "arguments": {
                    "machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}})
                context = response.json()["result"]["structuredContent"]["context"]
                request = self.task_request(context, "never-admitted")
                changes = [lambda r: r.update(runtime_state="succeeded"),
                    lambda r: r["stages"][0].update(state="accepted"),
                    lambda r: r["contract"].update(expires_at_ms=True),
                    lambda r: r["contract"].update(scope="\u00e9" * 513),
                    lambda r: r["stages"][0].update(source_sha256="f" * 64),
                    lambda r: r["contract"]["targets"][0].update(workspace_identity="f" * 64)]
                for change in changes:
                    invalid = copy.deepcopy(request)
                    change(invalid)
                    response = await self.wire_call(client, "tools/call", {"name": "create_task", "arguments": invalid})
                    result = response.json()["result"]
                    self.assertTrue(result["isError"], result)
                    self.assert_wire_tool_result(result, version)
                    self.assertIn(result["structuredContent"]["error"]["code"],
                                  {"invalid_tool_call", "invalid_task_contract", "context_mismatch"})
                    self.assertFalse((self.state / "tasks").exists())
                    self.assertFalse((self.state / "executions").exists())

    async def test_grace_schema_rejects_invalid_values_before_admission(self):
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for value in (True, False, "5", -1, 31):
                    identity = uuid.uuid4().hex
                    result = await session.call_tool("start_execution", {
                        "machine": "desktop", "execution_id": identity, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "termination_grace_seconds": value})
                    self.assertTrue(result.is_error, value)
                    self.assertFalse((self.state / "executions" / identity).exists())
                caps = (await session.call_tool("get_capabilities", {})).structured_content
                self.assertEqual(caps["execution"]["receipt_format"], 5)
                self.assertEqual(caps["execution"]["io_modes"], ["pipe", "pty"])
                self.assertEqual(caps["execution"]["controls"], ["interrupt", "resize", "write_stdin"])

    def assert_tool_contract(self, result: ListToolsResult, *, github: bool, ssh: bool):
        expected = json.loads(Path(__file__).with_name("mcp_contract.json").read_text())["tools"]
        if not github:
            expected = {name: tool for name, tool in expected.items() if not name.startswith("github_")}
        # Only these nine annotations vary with configured SSH; never normalize actual results.
        for name in ("inspect_workspace", "read_workspace_file", "write_workspace_file",
                     "read_execution", "list_executions", "control_execution", "edit_workspace_file", "read_workspace_change", "apply_workspace_patch"):
            expected[name]["annotations"]["openWorldHint"] = ssh
        self.assertIsNone(result.next_cursor, "The baseline tool catalog is a single complete page")
        actual = {tool.name: tool for tool in result.tools}
        self.assertEqual(len(result.tools), len(actual), "Duplicate tool names in discovery")
        self.assertEqual(set(actual), set(expected), "Configured MCP tool names changed")
        for name, contract in expected.items():
            observed = actual[name].model_dump(mode="json", by_alias=True, exclude_none=True,
                                              include={"input_schema", "output_schema", "annotations"})
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
                    async with self.client() as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            self.assert_tool_contract(await session.list_tools(), github=False, ssh=ssh)
                            caps = (await session.call_tool("get_capabilities", {})).structured_content
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
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                discovered = await session.list_tools()
                self.assert_tool_contract(discovered, github=True, ssh=True)
                tools = {item.name: item for item in discovered.tools}
                self.assertTrue(tools["read_workspace_file"].annotations.open_world_hint)
                self.assertTrue(tools["write_workspace_file"].annotations.open_world_hint)
                self.assertFalse(tools["save_task"].annotations.open_world_hint)
                caps = (await session.call_tool("get_capabilities", {})).structured_content
                self.assertIn("ssh", caps["integrations"])
                denied = await session.call_tool("read_workspace_file", {"machine": "build", "workspace_name": "remote", "path": "x"})
                self.assertEqual(denied.structured_content["error"]["code"], "identity_required")
                local = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example", "action": "read", "path": "AGENTS.md"})
                self.assertFalse(local.is_error)

    async def test_installed_package_discovery_and_socket_permissions(self):
        self.assertTrue(Path(openmachine.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                self.assertEqual(init.server_info.version, openmachine.__version__)
                discovered = await session.list_tools()
                self.assert_tool_contract(discovered, github=True, ssh=False)
                tools = {t.name: t for t in discovered.tools}
                self.assertEqual(set(tools), {"get_capabilities", "inspect_workspace", "inspect_skills", "start_execution", "read_execution", "list_executions", "cancel_execution", "control_execution", "github_issue", "github_pull_request", "github_comment", "github_review", "github_read", "github_merge", "read_workspace_file", "write_workspace_file", "edit_workspace_file", "read_workspace_change", "apply_workspace_patch", "find_tasks", "read_task", "save_task", "create_task"})
                self.assertFalse(tools["start_execution"].annotations.read_only_hint)
                self.assertTrue(tools["start_execution"].annotations.destructive_hint)
                self.assertTrue(tools["inspect_workspace"].annotations.read_only_hint)
                self.assertFalse(tools["cancel_execution"].annotations.read_only_hint)
                self.assertFalse(tools["control_execution"].annotations.read_only_hint)
                self.assertFalse(tools["control_execution"].annotations.idempotent_hint)
                self.assertFalse(tools["github_issue"].annotations.read_only_hint)
                self.assertTrue(tools["github_read"].annotations.read_only_hint)
                self.assertTrue(tools["github_read"].annotations.open_world_hint)
                for name in ("github_issue", "github_pull_request", "github_comment", "github_review", "github_merge"):
                    self.assertFalse(tools[name].annotations.idempotent_hint)
                self.assertNotIn("endpoint", tools["github_read"].input_schema["properties"])
                timeout = tools["start_execution"].input_schema["properties"]["timeout_seconds"]
                self.assertEqual((timeout["default"], timeout["maximum"]), (600, 86400))
                caps = (await session.call_tool("get_capabilities", {})).structured_content
                self.assertEqual(caps["machines"]["desktop"]["probe_status"], "not_probed")
                self.assertEqual(caps["integrations"], ["github"])
                skill = await session.call_tool("inspect_skills", {"action": "read"})
                self.assertFalse(skill.is_error)
                self.assertIn("Operate Open Machine", skill.structured_content["text"])

    async def test_github_scope_rejects_before_connection(self):
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for name, arguments in (
                    ("github_read", {"action": "repository", "repository": "Other/repo"}),
                    ("github_issue", {"action": "create", "repository": "Other/repo", "title": "not submitted"}),
                    ("github_merge", {"repository": "Other/repo", "number": 1, "expected_head_sha": "a" * 40}),
                ):
                    result = await session.call_tool(name, arguments)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], "github_not_allowed")

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
            async with self.client() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    unavailable = await session.call_tool("github_read", {"action": "status"})
                    self.assertTrue(unavailable.is_error)
                    self.assertEqual(unavailable.structured_content["error"]["code"], code)
                    identity = uuid.uuid4().hex
                    started = await session.call_tool("start_execution", {
                        "machine": "desktop", "execution_id": identity, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", "print('local-still-usable')"],
                    })
                    self.assertFalse(started.is_error, started)
                    for _ in range(100):
                        observed = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                        self.assertFalse(observed.is_error, observed)
                        if observed.structured_content["status"] == "succeeded":
                            break
                        await asyncio.sleep(0.03)
                    self.assertEqual(observed.structured_content["exit_code"], 0)
                    self.assertIn("local-still-usable", observed.structured_content["output"])
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
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("github_issue", {
                    "action": "update", "repository": "Example/repo", "number": 2, "state": "closed"})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "github_wrong_object_type")
                for path in (".ENV", "Secrets.toml", "ID_RSA", "config/.EnV.local"):
                    result = await session.call_tool("github_read", {
                        "action": "file", "repository": "Example/repo", "ref": "a" * 40, "path": path})
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], "invalid_github_request")
        observed = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertTrue(observed)
        self.assertTrue(all(method == "GET" and endpoint in responses for method, endpoint in observed))

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

    async def test_disabled_write_and_invalid_origin_fail_closed(self):
        self.stop(self.processes[0])
        self.config.write_text(self.config.read_text().replace("execution_enabled = true", "execution_enabled = false"))
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": uuid.uuid4().hex,
                                                                      "cwd": str(self.work), "argv": ["false"]})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "execution_disabled")
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(self.socket)), trust_env=False) as client:
            result = await client.get("http://localhost/mcp", headers={"host": "untrusted.invalid"})
            self.assertEqual(result.status_code, 421)

    async def test_invalid_tool_arguments_are_rejected_without_echoing_values(self):
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "password=synthetic-secret"})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "invalid_tool_call")
                self.assertNotIn("synthetic-secret", result.model_dump_json())

    async def test_second_server_does_not_replace_existing_socket(self):
        inode = self.socket.stat().st_ino
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                 "--state-dir", str(self.state), "serve"], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.socket.stat().st_ino, inode)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                self.assertFalse((await session.call_tool("get_capabilities", {})).is_error)


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

    async def test_reconnected_client_finds_and_cancels_original_execution(self):
        execution_id = uuid.uuid4().hex
        identity = {"machine": "desktop", "execution_id": execution_id}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("start_execution", {**identity, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", 'import time; print("started", flush=True); time.sleep(20)'],
                        "timeout_seconds": 86400})
                self.assertFalse(result.is_error, result)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = await session.call_tool("list_executions", {"machine": "desktop"})
                self.assertEqual(listing.structured_content["executions"][0]["execution_id"], execution_id)
                requested = await session.call_tool("cancel_execution", identity)
                self.assertFalse(requested.is_error, requested)
                self.assertTrue(requested.structured_content["cancel_requested"])
                for _ in range(100):
                    result = await session.call_tool("read_execution", identity)
                    self.assertFalse(result.is_error, result)
                    if result.structured_content["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(result.structured_content["status"], "cancelled")
                repeated = await session.call_tool("cancel_execution", identity)
                self.assertEqual(repeated.structured_content["status"], "cancelled")


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

    async def test_large_explicit_payload_and_changed_instruction_cancellation(self):
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
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                request = {"task_id": "transition-guard", "title": "Transition guard",
                           "expected_revision": 0, "body": "One authorized fixture checkpoint.", "anchors": []}
                with lease(self.state / "locks/lifecycle.lock"):
                    denied = await session.call_tool("save_task", request)
                    self.assertTrue(denied.is_error)
                    self.assertEqual(denied.structured_content["error"]["code"], "resource_busy")
                    self.assertFalse((await session.call_tool("get_capabilities", {})).is_error)
                    self.assertFalse((self.state / "tasks").exists())
                saved = await session.call_tool("save_task", request)
                self.assertFalse(saved.is_error, saved)
                self.assertEqual(saved.structured_content["revision"], 1)

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
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                caps = await session.call_tool("get_capabilities", {})
                self.assertEqual(caps.structured_content["lifecycle"]["pid"], self.processes[-1].pid)



    async def test_wait_schema_and_quiet_expiry_keep_original_execution(self):
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for value in (True, False, "1", -0.1, 5.1):
                    result = await session.call_tool("read_execution", identity | {"wait_seconds": value})
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], "invalid_tool_call")
                caps = (await session.call_tool("get_capabilities", {})).structured_content
                self.assertEqual(caps["execution"]["wait_seconds"], {"default": 0, "min": 0, "max": 5})
                self.assertEqual(caps["execution"]["max_waiting_readers"], 16)
                started = await session.call_tool("start_execution", identity | {
                    "cwd": str(self.work), "argv": [sys.executable, "-c", 'import time; print("ready",flush=True); time.sleep(10)'],
                    "timeout_seconds": 15})
                self.assertFalse(started.is_error)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        ready = (await session.call_tool("read_execution", identity | {"wait_seconds": 1})).structured_content
                        if "ready" in ready.get("output", ""):
                            break
                    self.assertIn("ready", ready["output"])
                    path = self.state / "executions" / identity["execution_id"] / "record.json"
                    original = path.read_bytes()
                    observed = await session.call_tool("read_execution", identity | {"offset": ready["next_offset"], "wait_seconds": 0.1})
                    self.assertFalse(observed.is_error)
                    self.assertTrue(observed.structured_content["wait_expired"])
                    self.assertEqual(observed.structured_content["status"], "running")
                    self.assertEqual(path.read_bytes(), original)
                finally:
                    await session.call_tool("cancel_execution", identity)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        result = (await session.call_tool("read_execution", identity | {"wait_seconds": 1})).structured_content
                        if result.get("status") == "cancelled":
                            break
                    self.assertEqual(result["status"], "cancelled")

    async def test_disconnected_waiter_preserves_command_and_other_operations(self):
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        gate = self.work / "emit"
        code = ('import time; from pathlib import Path; p=Path("counter"); p.write_text("once"); print("ready",flush=True)\n'
                'while not Path("emit").exists(): time.sleep(.01)\n'
                'print("continued",flush=True)')
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as control:
                await control.initialize()
                started = await control.call_tool("start_execution", identity | {
                    "cwd": str(self.work), "argv": [sys.executable, "-c", code], "timeout_seconds": 15})
                self.assertFalse(started.is_error)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        ready = (await control.call_tool("read_execution", identity | {"wait_seconds": 1})).structured_content
                        if "ready" in ready.get("output", ""):
                            break
                    self.assertIn("ready", ready["output"])
                    async with self.client() as (other_reader, other_writer):
                        async with ClientSession(other_reader, other_writer) as other:
                            await other.initialize()
                            waiting = asyncio.create_task(other.call_tool("read_execution", identity | {
                                "offset": ready["next_offset"], "wait_seconds": 5}))
                            await asyncio.sleep(0.1)
                            self.assertFalse(waiting.done())
                            caps = await asyncio.wait_for(control.call_tool("get_capabilities", {}), 2)
                            self.assertFalse(caps.is_error)
                            now = await asyncio.wait_for(control.call_tool("read_execution", identity), 2)
                            self.assertEqual(now.structured_content["status"], "running")
                            waiting.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await waiting
                    # A new connection observes the original worker rather than a replacement.
                    async with self.client() as (fresh_reader, fresh_writer):
                        async with ClientSession(fresh_reader, fresh_writer) as fresh:
                            await fresh.initialize()
                            observed = await fresh.call_tool("read_execution", identity)
                            self.assertEqual(observed.structured_content["created_at"], started.structured_content["created_at"])
                            self.assertFalse(observed.structured_content["cancel_requested"])
                            self.assertEqual((self.work / "counter").read_text(), "once")
                            gate.touch()
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline:
                                result = (await fresh.call_tool("read_execution", identity | {"wait_seconds": 1})).structured_content
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
            self.assertFalse(response.is_error)
            observed = response.structured_content
            if predicate(observed):
                return observed
            await asyncio.sleep(0.02)
        self.fail("The original SDK execution did not reach the required observation")

    async def test_interactive_worker_survives_controller_restart_without_replay(self):
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        code = ('import os,time; from pathlib import Path; p=Path("start-count"); '
                'p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); '
                'Path("process-identity").write_text(f"{os.getpid()} {os.getppid()}"); '
                'print("ready",flush=True); x=input(); print("first-read",flush=True); '
                'y=input(); print("same-process",x,y,flush=True); time.sleep(8)')
        try:
            async with self.client() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    started = await session.call_tool("start_execution", identity | {
                        "cwd": str(self.work), "argv": [sys.executable, "-c", code], "timeout_seconds": 15, "tty": True})
                    self.assertFalse(started.is_error)
                    ready = await self.execution_until(session, identity, lambda r: r["control_ready"] and "ready" in r["output"])
                    sent = await session.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "write_stdin", "text": "41\n"})
                    self.assertFalse(sent.is_error)
                    receipt = sent.structured_content["control_result"]
                    self.assertEqual(receipt["status"], "applied")
                    self.assertNotIn("output", sent.structured_content)
                    before = await self.execution_until(session, identity, lambda r: "first-read" in r["output"])
            original = execution.read_record(self.state / "executions" / identity["execution_id"])
            original_processes = (self.work / "process-identity").read_text()
            self.stop(self.processes[0])
            self.assertFalse(self.socket.exists())
            restarted = self.launch()
            self.assertNotEqual(restarted.pid, self.processes[0].pid)
            async with self.client() as (reader, writer):
                async with ClientSession(reader, writer) as fresh:
                    await fresh.initialize()
                    observed = await self.execution_until(fresh, identity, lambda r: r["control"]["seq"] == 1)
                    self.assertEqual(observed["created_at"], ready["created_at"])
                    self.assertEqual(observed["context"], ready["context"])
                    retained = execution.read_record(self.state / "executions" / identity["execution_id"])
                    self.assertEqual((self.work / "process-identity").read_text(), original_processes)
                    for pid in map(int, original_processes.split()):
                        os.kill(pid, 0)
                    self.assertEqual(retained["command"], original["command"])
                    self.assertEqual(retained["request"], original["request"])
                    self.assertEqual(observed["control"]["last"], receipt)
                    cursor = (await fresh.call_tool("read_execution", identity | {
                        "offset": before["next_offset"]})).structured_content
                    self.assertEqual(cursor["next_offset"], before["next_offset"])
                    self.assertFalse(cursor["output_gap"])
                    self.assertEqual(cursor["output"], "")
                    duplicate = await fresh.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "write_stdin", "text": "41\n"})
                    self.assertFalse(duplicate.is_error)
                    self.assertEqual(duplicate.structured_content["control_result"], receipt)
                    second = await fresh.call_tool("control_execution", identity | {
                        "expected_control_seq": 1, "action": "write_stdin", "text": "1\n"})
                    self.assertFalse(second.is_error)
                    await self.execution_until(fresh, identity, lambda r: "same-process 41 1" in r["output"])
                    self.assertEqual((self.work / "start-count").read_text(), "1")
                    self.assertFalse((await fresh.call_tool("cancel_execution", identity)).is_error)
                    final = await self.execution_until(fresh, identity, lambda r: r["status"] == "cancelled")
                    self.assertEqual(final["cleanup"]["status"], "verified")
                    self.assertEqual(final["control"]["seq"], 2)
        finally:
            async with self.client() as (reader, writer):
                async with ClientSession(reader, writer) as cleanup:
                    await cleanup.initialize()
                    await cleanup.call_tool("cancel_execution", identity)

    async def test_interactive_sdk_schema_rejects_invalid_effects_before_admission(self):
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for delta in ({"tty": 1}, {"tty": "true"}, {"tty": True, "terminal_size": {"rows": True, "cols": 80}},
                              {"tty": True, "terminal_size": {"rows": 24, "cols": 80, "extra": 1}},
                              {"terminal_size": {"rows": 24, "cols": 80}}):
                    identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
                    result = await session.call_tool("start_execution", identity | {
                        "cwd": str(self.work), "argv": [sys.executable, "-c", "pass"]} | delta)
                    self.assertTrue(result.is_error)
                    self.assertFalse((self.state / "executions" / identity["execution_id"]).exists())
                identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
                for delta in ({"expected_control_seq": True}, {"expected_control_seq": 2**53 - 1},
                              {"action": "close_stdin"}, {"action": "write_stdin", "text": "\0"},
                              {"action": "write_stdin", "text": "é" * 8193},
                              {"action": "resize", "terminal_size": {"rows": 1, "cols": 1001}}):
                    result = await session.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "interrupt"} | delta)
                    self.assertTrue(result.is_error)
                    self.assertNotEqual(result.structured_content["error"]["code"], "execution_not_found")
                self.assertFalse((self.state / "executions" / identity["execution_id"]).exists())


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

    def wire_client(self, version):
        return httpx.AsyncClient(trust_env=False, timeout=10,
            transport=httpx.AsyncHTTPTransport(uds=str(self.socket)),
            headers={"accept": "application/json, text/event-stream",
                     "content-type": "application/json", "MCP-Protocol-Version": version})

    async def wire_call(self, client, method, params=None):
        version = client.headers["MCP-Protocol-Version"]
        headers = {}
        if version == "2026-07-28":
            params = dict(params or {})
            meta = dict(params.get("_meta") or {})
            meta["io.modelcontextprotocol/protocolVersion"] = version
            meta["io.modelcontextprotocol/clientCapabilities"] = {}
            params["_meta"] = meta
            headers["MCP-Method"] = method
            if method == "tools/call" and isinstance(params.get("name"), str):
                headers["MCP-Name"] = params["name"]
        request = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            request["params"] = params
        return await client.post("http://localhost/mcp", json=request, headers=headers)

    def assert_wire_tool_result(self, result, version):
        modern = version == "2026-07-28"
        self.assertEqual("resultType" in result, modern, result)
        if modern:
            self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result.get("_meta", {}).get("io.modelcontextprotocol/serverInfo"),
                         {"name": "Open Machine", "version": openmachine.__version__} if modern else None)
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])

    async def test_migration_protocols_preserve_all_four_catalogs(self):
        from importlib.metadata import version
        self.assertEqual(version("mcp"), "2.2.0")
        original = self.config.read_text()
        self.stop(self.processes[0])
        for github in (False, True):
            for ssh in (False, True):
                config = original if github else original.split("[github]")[0]
                if ssh:
                    config += '\n[machines.build]\ntransport="ssh"\nssh_alias="uncontacted-host"\nssh_worker="/remote/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pyz"\n'
                self.config.write_text(config)
                process = self.launch()
                try:
                    for mode in ("legacy", "2026-07-28", "auto"):
                        with self.subTest(github=github, ssh=ssh, mode=mode):
                            async with Client(self.client(), mode=mode, cache=None) as client:
                                expected = "2025-11-25" if mode == "legacy" else "2026-07-28"
                                self.assertEqual(client.protocol_version, expected)
                                self.assert_tool_contract(await client.list_tools(), github=github, ssh=ssh)
                                result = await client.call_tool("read_workspace_file", {
                                    "machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"})
                                self.assertFalse(result.is_error, result)
                                self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                                self.assertIn("disposable", result.structured_content["text"])
                finally:
                    self.stop(process)


    async def test_migration_wire_envelopes_and_invalid_arguments_are_safe(self):
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                response = await self.wire_call(client, "tools/call", {"name": "get_capabilities"})
                self.assertEqual(response.status_code, 200)
                result = response.json()["result"]
                self.assert_wire_tool_result(result, version)
                scope = {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}
                for params in ({"name": "untrusted-sentinel"},
                               {"name": "read_workspace_file", "arguments": scope | {"line_count": "untrusted-sentinel"}},
                               {"name": "read_workspace_file", "arguments": scope | {"line_count": 1.0}},
                               {"name": "get_capabilities", "_meta": {"progressToken": ["untrusted-sentinel"]}}):
                    response = await self.wire_call(client, "tools/call", params)
                    self.assertEqual(response.status_code, 200)
                    result = response.json()["result"]
                    self.assertTrue(result["isError"])
                    self.assertIn("structuredContent", result, (version, params, result))
                    self.assert_wire_tool_result(result, version)
                    self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_tool_call")
                    self.assertNotIn("untrusted-sentinel", response.text)
                for method in ("events/list", "events/subscribe", "events/unsubscribe"):
                    response = await self.wire_call(client, method, {})
                    self.assertEqual(response.json()["error"]["code"], -32601)
        self.assertFalse((self.state / "executions").exists())
        self.assertFalse((self.state / "tasks").exists())


    async def test_migration_ingress_host_origin_and_version_reject_before_effects(self):
        bound = 1048576
        harmless = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "get_capabilities"}}).encode()
        mutation = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "save_task", "arguments": {"task_id": "never-admitted", "expected_revision": 0,
            "title": "Rejected request", "body": "No effects"}}}
        oversized = json.dumps(mutation).encode()
        oversized += b" " * (bound + 1 - len(oversized))
        async def chunks():
            for offset in range(0, len(oversized), 32768):
                yield oversized[offset:offset + 32768]
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                exact_body = harmless
                exact_headers = {}
                if version == "2026-07-28":
                    request = json.loads(harmless)
                    request["params"]["_meta"] = {
                        "io.modelcontextprotocol/protocolVersion": version,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                    exact_body = json.dumps(request).encode()
                    exact_headers = {"MCP-Method": "tools/call", "MCP-Name": "get_capabilities"}
                exact = await client.post("http://localhost/mcp",
                                          content=exact_body + b" " * (bound - len(exact_body)),
                                          headers=exact_headers)
                self.assertEqual(exact.status_code, 200)
                self.assertFalse(exact.json()["result"]["isError"])
                for content in (oversized, chunks()):
                    response = await client.post("http://localhost/mcp", content=content)
                    self.assertEqual(response.status_code, 413)
                for headers, status in (({"host": "untrusted.invalid"}, 421),
                                        ({"origin": "https://untrusted.invalid"}, 403),
                                        ({"MCP-Protocol-Version": "2099-01-01"}, 400)):
                    response = await client.post("http://localhost/mcp", json=mutation, headers=headers)
                    self.assertEqual(response.status_code, status, response.text)
        self.assertFalse((self.state / "tasks").exists())

    async def test_modern_overlapping_clients_and_abandoned_wait_preserve_original_execution(self):
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        gate = self.work / "modern-emit"
        code = ('import time; from pathlib import Path; p=Path("modern-counter"); p.write_text("once"); '
                'print("ready",flush=True)\n'
                'while not Path("modern-emit").exists(): time.sleep(.01)\n'
                'print("continued",flush=True)')
        async with Client(self.client(), mode="2026-07-28", cache=None) as control:
            self.assertEqual(control.protocol_version, "2026-07-28")
            started = await control.call_tool("start_execution", identity | {
                "cwd": str(self.work), "argv": [sys.executable, "-c", code], "timeout_seconds": 15})
            self.assertFalse(started.is_error, started)
            try:
                ready = await self.execution_until(control, identity, lambda r: "ready" in r.get("output", ""))
                async with Client(self.client(), mode="2026-07-28", cache=None) as other:
                    waiting = asyncio.create_task(other.call_tool("read_execution", identity | {
                        "offset": ready["next_offset"], "wait_seconds": 5}))
                    await asyncio.sleep(0.1)
                    self.assertFalse(waiting.done())
                    caps = await asyncio.wait_for(control.call_tool("get_capabilities", {}), 2)
                    self.assertFalse(caps.is_error)
                    now = await asyncio.wait_for(control.call_tool("read_execution", identity), 2)
                    self.assertEqual(now.structured_content["status"], "running")
                    waiting.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await waiting
                async with Client(self.client(), mode="2026-07-28", cache=None) as fresh:
                    self.assertEqual(fresh.protocol_version, "2026-07-28")
                    observed = await fresh.call_tool("read_execution", identity)
                    self.assertEqual(observed.structured_content["created_at"], started.structured_content["created_at"])
                    self.assertFalse(observed.structured_content["cancel_requested"])
                    self.assertEqual((self.work / "modern-counter").read_text(), "once")
                    gate.touch()
                    result = await self.execution_until(fresh, identity, lambda r: r.get("status") == "succeeded")
                    self.assertEqual((result["status"], result["exit_code"]), ("succeeded", 0))
                    self.assertIn("continued", result["output"])
            finally:
                gate.touch()
                await control.call_tool("cancel_execution", identity)


    async def test_task_events_readonly_preflight_preserves_original_store(self):
        import probe_task_handoff as probe
        self.assertEqual(probe.private_socket(str(self.socket)), self.socket)
        async with self.wire_client(probe.PROTOCOL) as client:
            report = await probe.observe(client)
        self.assertEqual(report["discovery"], "blocked")
        self.assertTrue(report["protocol_supported"])
        self.assertFalse(report["events_advertised"])
        self.assertEqual(report["method_errors"]["events/list"], -32601)
        self.assertEqual(report["ai_continuation"], "not_run")
        self.assertFalse((self.state / "tasks").exists())
        self.assertFalse((self.state / "executions").exists())
        alias = self.root / "socket-alias"
        alias.symlink_to(self.socket)
        for path in (str(alias), str(self.config), "relative.sock"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                probe.private_socket(path)

    async def test_task_events_public_sdk_seam_is_not_subscription_acceptance(self):
        from importlib.metadata import version
        from mcp.server import MCPServer
        from mcp.server.extension import Extension, MethodBinding
        from mcp.types import RequestParams, Result, ServerCapabilities
        from mcp.shared.exceptions import MCPError
        import probe_task_handoff as probe

        self.assertEqual(version("mcp"), "2.2.0")
        # Unknown constructor fields are discarded; Extension.settings is not capabilities.events.
        self.assertNotIn("events", ServerCapabilities(events={}).model_dump(exclude_none=True))
        calls = []
        class Catalog(Result):
            events: list[dict]
        async def listing(ctx, params):
            calls.append(ctx.method)
            return Catalog(events=[{"name": "task.handoff_ready", "delivery": ["webhook"]}])
        async def unavailable(ctx, params):
            calls.append(ctx.method)
            raise MCPError(-32000, "S0 wire probe has no subscription backend")
        class Events(Extension):
            identifier = "com.example/s0-events"
            def methods(self):
                return [MethodBinding(name, RequestParams, handler, frozenset({probe.PROTOCOL}))
                        for name, handler in (("events/list", listing), ("events/subscribe", unavailable),
                                              ("events/unsubscribe", unavailable))]
        with self.assertRaises(ValueError):
            MethodBinding("server/discover", RequestParams, listing)
        async def advertise(ctx, call_next):
            result = await call_next(ctx)
            if ctx.method == "server/discover" and ctx.protocol_version == probe.PROTOCOL:
                return {**result, "capabilities": {**result["capabilities"], "events": {}}}
            return result

        for declared in (False, True):
            mcp = MCPServer("S0 wire probe", extensions=[Events()], middleware=[advertise] if declared else [])
            @mcp.tool()
            def sentinel() -> str:
                return "unchanged"
            from mcp.server.transport_security import TransportSecuritySettings
            app = mcp.streamable_http_app(json_response=True, stateless_http=True,
                transport_security=TransportSecuritySettings(allowed_hosts=["localhost"]))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False,
                        timeout=5, headers={"MCP-Protocol-Version": probe.PROTOCOL,
                        "accept": "application/json, text/event-stream"}) as client:
                    discovery = (await self.wire_call(client, "server/discover", {})).json()["result"]
                    self.assertIn("com.example/s0-events", discovery["capabilities"]["extensions"])
                    self.assertEqual("events" in discovery["capabilities"], declared)
                    report = await probe.observe(client)
                    self.assertEqual(report["discovery"], "ready" if declared else "blocked")
                    self.assertEqual(report["ai_continuation"], "not_run")
                    before = len(calls)
                    for method in ("events/subscribe", "events/unsubscribe"):
                        response = await self.wire_call(client, method, {})
                        self.assertEqual(response.json()["error"]["code"], -32000)
                    self.assertEqual(calls[before:], ["events/subscribe", "events/unsubscribe"])
                    tool = (await self.wire_call(client, "tools/call", {"name": "sentinel"})).json()["result"]
                    self.assertEqual(tool["content"][0]["text"], "unchanged")
                    self.assertEqual(tool["resultType"], "complete")
                    client.headers["MCP-Protocol-Version"] = "2025-11-25"
                    before = len(calls)
                    for method in ("events/list", "events/subscribe", "events/unsubscribe"):
                        response = await self.wire_call(client, method, {})
                        self.assertEqual(response.json()["error"]["code"], -32601)
                    self.assertEqual(len(calls), before)

    async def test_task_events_probe_rejects_inconclusive_or_unsafe_evidence(self):
        import probe_task_handoff as probe
        requests = []
        def transport(request):
            value = json.loads(request.content)
            requests.append(value["method"])
            if value["method"] == "server/discover":
                result = {"supportedVersions": [probe.PROTOCOL], "capabilities": {"events": {}}}
            else:
                result = {"events": [], "nextCursor": "more"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": value["id"], "result": result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as client:
            report = await probe.observe(client)
            self.assertEqual(report["discovery"], "unknown")
            self.assertFalse(report["catalog_complete"])
            self.assertEqual(requests, list(probe.READ_METHODS))
            with self.assertRaises(ValueError):
                await probe.rpc(client, "events/subscribe")
            self.assertEqual(requests, list(probe.READ_METHODS))
        cases = ((200, b"x" * (probe.MAX_RESPONSE + 1)), (200, b"{}"),
                 (200, b'{"jsonrpc":"2.0","id":true,"result":{}}'),
                 (200, b'{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":-32601}}'),
                 (302, b"not-followed"))
        for status, raw in cases:
            with self.subTest(status=status, size=len(raw)):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(status, content=raw)), trust_env=False) as client:
                    with self.assertRaises(ValueError):
                        await probe.rpc(client, "server/discover")


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
                self.assertEqual(overflow.structured_content["error"]["code"], "reader_busy")
                self.assertFalse((await mcp.call_tool("get_capabilities", {})).is_error)
                self.assertFalse((await mcp.call_tool("read_execution", args | {"wait_seconds": 0})).is_error)
                self.assertFalse((await mcp.call_tool("cancel_execution", {"machine": "local", "execution_id": "a" * 32})).is_error)
                readers[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await readers[0]
                self.assertTrue(any(event.is_set() for event in calls))
                # The fake observer intentionally has not exited yet. Its slot
                # cannot be reused just because the asyncio await was cancelled.
                overflow = await mcp.call_tool("read_execution", args)
                self.assertEqual(overflow.structured_content["error"]["code"], "reader_busy")
            finally:
                release.set()
                await asyncio.gather(*readers, return_exceptions=True)
            result = await mcp.call_tool("read_execution", args)
            self.assertFalse(result.is_error)

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
                self.assertFalse(result.is_error)
                self.assertTrue(result.structured_content["cancel_requested"])
                application.cancel_execution.assert_called_once_with("local", "b" * 32)
                controlled = await asyncio.wait_for(mcp.call_tool("control_execution", args | {"expected_control_seq": 0, "action": "interrupt"}), 1)
                self.assertEqual(controlled.structured_content["control_result"]["status"], "applied")
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
        self.assertFalse(result.is_error)
        self.assertLessEqual(len(encoded), server.MAX_RESULT_BYTES - 1024)
        self.assertTrue(result.structured_content["output_presentation_truncated"])
        self.assertEqual(result.structured_content["next_offset"], 200000)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertFalse(payload["output_presentation_truncated"])


if __name__ == "__main__":
    unittest.main()
