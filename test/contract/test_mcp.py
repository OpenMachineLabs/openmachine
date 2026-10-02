"""Behavior checks for MCPContractTests."""

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
import httpx2 as httpx
from mcp import Client, ClientSession
import openmachine
from openmachine.foundation.state import lease
from support_mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
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
                self.assertEqual(set(tools), {"get_capabilities", "inspect_workspace", "inspect_skills", "start_execution", "read_execution", "list_executions", "cancel_execution", "control_execution", "github_issue", "github_pull_request", "github_comment", "github_review", "github_read", "github_merge", "read_workspace_file", "write_workspace_file", "edit_workspace_file", "read_workspace_change", "apply_workspace_patch", "find_tasks", "read_task", "save_task", "create_task", "control_task"})
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
        # Content changes and safe redaction do not make a readable probe fail.
        (self.work / "AGENTS.md").write_text("token = synthetic-test-value\n")
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                redacted = await session.call_tool("read_workspace_file", {"machine": "desktop",
                    "workspace_name": "example", "path": "AGENTS.md"})
                self.assertFalse(redacted.is_error)
                self.assertTrue(redacted.structured_content["content_redacted"])
        ready = await asyncio.to_thread(service.probe_service, self.state, self.config, probe,
                                        openmachine.__version__, self.processes[-1].pid)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["file_sha256"], hashlib.sha256((self.work / "AGENTS.md").read_bytes()).hexdigest())
        self.assertNotIn("synthetic-test-value", json.dumps(ready))
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
                                    "--state-dir", str(self.state), "serve", "--managed-child"],
                                   capture_output=True, timeout=10)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(self.socket.stat().st_ino, inode)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                caps = await session.call_tool("get_capabilities", {})
                self.assertEqual(caps.structured_content["lifecycle"]["pid"], self.processes[-1].pid)

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
                    if version == "2026-07-28" and method == "events/list":
                        self.assertEqual(response.json()["result"]["events"][0]["name"], "task.handoff_ready")
                    else:
                        self.assertEqual(response.json()["error"]["code"], -32602 if version == "2026-07-28" else -32601)
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
