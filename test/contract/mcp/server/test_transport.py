"""contract mcp server test_transport behavior."""

import asyncio
import json
import unittest
import uuid
import httpx2 as httpx
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_disabled_write_and_invalid_origin_fail_closed(self):
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
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "password=synthetic-secret"})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "invalid_tool_call")
                self.assertNotIn("synthetic-secret", result.model_dump_json())

    async def test_migration_wire_envelopes_and_invalid_arguments_are_safe(self):
        self.launch()
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
                if version == "2026-07-28":
                    discovery = (await self.wire_call(client, "server/discover", {})).json()["result"]
                    self.assertNotIn("events", discovery["capabilities"])
        self.assertFalse((self.state / "events.json").exists())
        self.assertFalse((self.state / "locks/events.lock").exists())
        self.assertFalse((self.state / "executions").exists())
        self.assertFalse((self.state / "tasks").exists())

    async def test_migration_ingress_host_origin_and_version_reject_before_effects(self):
        self.launch()
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

    async def test_invalid_calls_identify_core_validation_without_echoing_inputs(self):
        self.launch()
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                caps = (await self.wire_call(client, "tools/call", {"name": "get_capabilities"})).json()["result"]
                digest = caps["structuredContent"]["mcp_catalog"]["sha256"]
                cases = (({"name": "private-sentinel"}, "unknown_tool"),
                    ({"name": "read_task", "arguments": {"task_id": "not-created", "limit": "private-sentinel"}}, "invalid_arguments"),
                    ({"name": "read_workspace_file", "arguments": {"machine": "desktop", "workspace_name": "example",
                        "path": "AGENTS.md", "line_count": 1.0}}, "sdk_validation"),
                    ({"name": "control_task", "arguments": {"task_id": "not-created", "expected_revision": 1.0,
                        "expected_plan_revision": 1, "expected_epoch": 0, "update_id": "a" * 32,
                        "transition": {"action": "claim", "handoff_id": "b" * 32}}}, "sdk_validation"),
                    ({"name": "read_task", "arguments": {"task_id": "not-created", "preparation_id": "a" * 32}}, "unexpected_arguments"))
                for params, reason in cases:
                    response = await self.wire_call(client, "tools/call", params)
                    result = response.json()["result"]
                    self.assert_wire_tool_result(result, version)
                    self.assertTrue(result["isError"])
                    diagnostic = result["structuredContent"]["diagnostic"]
                    self.assertEqual(diagnostic["source"], "core.mcp")
                    self.assertEqual(diagnostic["reason"], reason)
                    self.assertEqual(diagnostic["phase"], "validation")
                    self.assertEqual(diagnostic["current_call_dispatch"], "not_attempted")
                    self.assertEqual(diagnostic["catalog_sha256"], digest)
                    self.assertNotIn("private-sentinel", response.text)
        self.assertFalse((self.state / "tasks.db").exists())
        self.assertFalse((self.state / "executions").exists())

    async def test_unstructured_handler_failure_never_claims_no_dispatch(self):
        marker = self.root / "handler-count"
        self.launch(bootstrap=("from pathlib import Path\nfrom openmachine.app import Application\n"
            "def fail_after_effect(self):\n"
            f"    marker = Path({str(marker)!r})\n"
            "    marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else '1')\n"
            "    raise ValueError('private-handler-sentinel')\n"
            "Application.get_capabilities = fail_after_effect\n"))
        for count, version in enumerate(("2025-11-25", "2026-07-28"), 1):
            async with self.wire_client(version) as client:
                response = await self.wire_call(client, "tools/call", {"name": "get_capabilities"})
                result = response.json()["result"]
                self.assert_wire_tool_result(result, version)
                self.assertTrue(result["isError"])
                diagnostic = result["structuredContent"]["diagnostic"]
                self.assertEqual(diagnostic["phase"], "handler")
                self.assertEqual(diagnostic["current_call_dispatch"], "unknown")
                self.assertEqual(marker.read_text(), str(count))
                self.assertNotIn("private-handler-sentinel", response.text)

    async def test_handler_validation_errors_remain_unknown_for_sync_and_async_tools(self):
        marker = self.root / "validation-count"
        self.launch(bootstrap=("from pathlib import Path\nfrom pydantic import TypeAdapter\n"
            "from openmachine.app import Application\n"
            "def fail_after_effect(self, *args, **kwargs):\n"
            f"    marker = Path({str(marker)!r})\n"
            "    marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else '1')\n"
            "    TypeAdapter(int).validate_python('private-validation-sentinel')\n"
            "Application.get_capabilities = Application.read_task = fail_after_effect\n"))
        count = 0
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                for name, arguments in (("get_capabilities", {}), ("read_task", {"task_id": "fixture"})):
                    response = await self.wire_call(client, "tools/call", {"name": name, "arguments": arguments})
                    result = response.json()["result"]
                    self.assert_wire_tool_result(result, version)
                    self.assertTrue(result["isError"])
                    diagnostic = result["structuredContent"]["diagnostic"]
                    self.assertEqual(diagnostic["phase"], "handler")
                    self.assertEqual(diagnostic["current_call_dispatch"], "unknown")
                    count += 1
                    self.assertEqual(marker.read_text(), str(count))
                    self.assertNotIn("private-validation-sentinel", response.text)

    async def test_handler_entry_is_isolated_from_concurrent_rejected_calls(self):
        ready, release = self.root / "handler-ready", self.root / "handler-release"
        self.launch(bootstrap=("from pathlib import Path\nimport time\nfrom openmachine.app import Application\n"
            "def blocked_handler(self, *args, **kwargs):\n"
            f"    Path({str(ready)!r}).touch()\n"
            "    deadline = time.monotonic() + 5\n"
            f"    while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
            "        time.sleep(0.01)\n"
            "    raise ValueError('private-concurrent-sentinel')\n"
            "Application.read_task = blocked_handler\n"))
        async with self.wire_client("2026-07-28") as active, self.wire_client("2025-11-25") as rejected:
            pending = asyncio.create_task(self.wire_call(active, "tools/call", {
                "name": "read_task", "arguments": {"task_id": "fixture"}}))
            try:
                async def await_entry():
                    while not ready.exists():
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(await_entry(), 3)
                response = await self.wire_call(rejected, "tools/call", {
                    "name": "read_task", "arguments": {"task_id": "fixture", "limit": 1.0}})
                diagnostic = response.json()["result"]["structuredContent"]["diagnostic"]
                self.assertEqual(diagnostic["reason"], "sdk_validation")
                self.assertEqual(diagnostic["phase"], "validation")
                self.assertEqual(diagnostic["current_call_dispatch"], "not_attempted")
            finally:
                release.touch()
                completed = await asyncio.wait_for(pending, 3)
            diagnostic = completed.json()["result"]["structuredContent"]["diagnostic"]
            self.assertEqual(diagnostic["phase"], "handler")
            self.assertEqual(diagnostic["current_call_dispatch"], "unknown")
            self.assertNotIn("private-concurrent-sentinel", completed.text)
