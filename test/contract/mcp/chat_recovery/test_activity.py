"""contract mcp chat_recovery test_activity behavior."""

import asyncio
import json
import unittest
import uuid
from mcp import Client
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_recovery_activity_is_attributable_and_does_not_change_task_revisions(self):
        self.launch()
        for mode in ("legacy", "2026-07-28"):
            identity = "chat-activity-" + mode
            async with Client(self.client(), mode=mode, cache=None) as client:
                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    self.assertFalse(result.is_error, result.structured_content)
                    return result.structured_content
                context = (await call("read_workspace_file", {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}))["context"]
                request = self.task_request(context, identity)
                await call("create_task", request)
                await call("configure_chat_recovery", {"task_id": identity, "update_id": uuid.uuid4().hex,
                    "expected_revision": 0, "change": {"action": "disable"}})
                path = self.state / "chat_recovery.json"
                before = path.read_bytes()
                task_before = (self.state / "tasks" / identity / "task.json").read_bytes()
                caps = await call("get_capabilities", {})
                self.assertTrue(caps["chat_recovery"]["automatic_detection"])
                invalid = await client.call_tool("read_task", {"task_id": None})
                self.assertTrue(invalid.is_error)
                self.assertEqual(path.read_bytes(), before)
                await asyncio.sleep(0.002)
                await call("read_task", {"task_id": identity})
                current = json.loads(path.read_bytes())["bindings"][identity]
                self.assertGreater(current["activity_at_ms"], json.loads(before)["bindings"][identity]["activity_at_ms"])
                self.assertEqual((self.state / "tasks" / identity / "task.json").read_bytes(), task_before)
