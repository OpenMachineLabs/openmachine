"""contract mcp execution test_schema behavior."""

import sys
import unittest
import uuid
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_grace_schema_rejects_invalid_values_before_admission(self):
        self.launch()
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

    async def test_interactive_sdk_schema_rejects_invalid_effects_before_admission(self):
        self.launch()
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
