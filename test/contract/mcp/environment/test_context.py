"""Installed private-socket context contract and read-only behavior."""

import json
import unittest

from test.support.mcp import MCPFixture


class ContextWireTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_observation_and_input_errors_on_both_protocols(self):
        self.launch()
        base = {"machine": "desktop", "workspace_name": "example"}
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                reply = await self.wire_call(client, "tools/call", {"name": "inspect_context", "arguments": base})
                result = reply.json()["result"]
                self.assert_wire_tool_result(result, version)
                self.assertFalse(result.get("isError", False), result)
                value = result["structuredContent"]
                self.assertEqual(value["schema_version"], 1)
                self.assertFalse(value["atomic"])
                self.assertEqual(value["target"]["cwd"], str(self.work))
                self.assertEqual(value["sections"]["activity"]["status"], "not_selected")
                self.assertLess(len(json.dumps(result).encode()), 1024 * 1024)
                observed = await self.wire_call(client, "tools/call", {"name": "inspect_context", "arguments": base |
                    {"sections": ["environment", "project"], "executables": ["python3"]}})
                target = observed.json()["result"]
                self.assert_wire_tool_result(target, version)
                self.assertFalse(target.get("isError", False), target)
                self.assertEqual(target["structuredContent"]["sections"]["environment"]["scope"], "controller")
                self.assertIn("interpreter", target["structuredContent"]["sections"]["environment"]["data"]["value"])
                for extra in ({"limit": True}, {"limit": 51}, {"sections": []}, {"surplus": 1},
                              {"sections": ["runtime", "runtime"]}, {"sections": ["unsupported"]},
                              {"sections": ["services"], "ports": [True]}, {"ports": [80]},
                              {"sections": ["environment"], "executables": ["../invalid"]}):
                    response = await self.wire_call(client, "tools/call", {"name": "inspect_context", "arguments": base | extra})
                    error = response.json()["result"]
                    self.assert_wire_tool_result(error, version)
                    self.assertTrue(error["isError"], error)
        self.assertFalse((self.state / "tasks.db").exists())
        self.assertFalse((self.state / "executions").exists())


if __name__ == "__main__":
    unittest.main()
