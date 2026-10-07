"""resilience execution test_mcp_waiting behavior."""

import sys
import time
import unittest
import uuid
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_wait_schema_and_quiet_expiry_keep_original_execution(self):
        self.launch()
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
