"""Disposable MCP state and explicitly started installed listeners."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import httpx2 as httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ListToolsResult
import openmachine


class MCPFixture(object):
    maxDiff = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-mcp-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "AGENTS.md").write_text("Only modify this disposable project.\n")
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version = 1\ncheck_for_updates = false\n[machines.desktop]\ntransport = "local"\nexecution_enabled = true\n'
                               '[workspaces.example]\nmachine = "desktop"\nroot = ' + json.dumps(str(self.work)) + '\n'
                               '[github]\nrepositories = ["Example/repo"]\nattribution = false\n')
        self.state = self.root / "state"
        self.socket = self.state / "mcp.sock"
        self.processes = []
        self.addCleanup(self.stop_all)
        # Tests launch only the configured listener they actually exercise.

    def launch(self, environment=None, *, managed=False, bootstrap=""):
        entry = ["-c", bootstrap + "\nfrom openmachine.cli import main; main()"] if bootstrap else ["-m", "openmachine.cli"]
        process = subprocess.Popen([sys.executable, "-I", *entry, "--config", str(self.config),
                                    "--state-dir", str(self.state), "serve", *(["--managed-child"] if managed else [])],
                                   cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=environment)
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            if self.socket.exists():
                try:
                    # A listening socket precedes ASGI startup. Restart fixtures
                    # must wait for HTTP readiness before sending SIGTERM.
                    with httpx.Client(trust_env=False, timeout=0.2,
                                      transport=httpx.HTTPTransport(uds=str(self.socket))) as client:
                        if client.options("http://localhost/mcp").status_code == 405:
                            return process
                except httpx.TransportError:
                    pass
            time.sleep(0.03)
        self.fail("Private MCP HTTP listener was not ready")

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

    async def observer_task_view(self, client, identity):
        # The canonical reader is deliberately nonblocking. A concurrent result
        # publication may return resource_busy; retry only that passive read,
        # bounded by one deadline, and expose every other structured failure.
        deadline = time.monotonic() + 2
        while True:
            result = await client.call_tool("read_task", {"task_id": identity})
            payload = result.structured_content or {}
            if not result.is_error:
                return payload
            self.assertEqual(payload.get("error", {}).get("code"), "resource_busy", payload)
            self.assertLess(time.monotonic(), deadline, payload)
            await asyncio.sleep(0.02)

    def assert_tool_contract(self, result: ListToolsResult, *, github: bool, ssh: bool):
        expected = json.loads((Path(__file__).resolve().parents[1] / "contract/mcp/mcp_contract.json").read_text())["tools"]
        if not github:
            expected = {name: tool for name, tool in expected.items() if not name.startswith("github_")}
        # Only these ten annotations vary with configured SSH; never normalize actual results.
        for name in ("inspect_context", "inspect_workspace", "read_workspace_file", "write_workspace_file",
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
