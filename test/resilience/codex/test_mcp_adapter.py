"""resilience codex test_mcp_adapter behavior."""

import os
import unittest
import uuid
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_codex_browser_tools_keep_original_execution_and_block_missing_bridge(self):
        environment = dict(os.environ, CODEX_HOME=str(self.root / "missing-codex-home"))
        self.launch(environment)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                args = {"machine": "desktop", "execution_id": uuid.uuid4().hex,
                    "cwd": str(self.work), "goal": "Inspect only a disposable public page",
                    "start_url": "https://example.com/", "allowed_origins": ["https://example.com"]}
                for delta in ({"mode": "computer"}, {"unknown": True}, {"timeout_seconds": True}):
                    invalid = await session.call_tool("start_codex_task", args | delta)
                    self.assertTrue(invalid.is_error)
                    self.assertFalse((self.state / "executions" / args["execution_id"]).exists())
                started = await session.call_tool("start_codex_task", args)
                self.assertFalse(started.is_error, started)
                identity = {"machine": "desktop", "execution_id": args["execution_id"]}
                original = await self.execution_until(session, identity, lambda v: v["status"] == "succeeded")
                semantic = (await session.call_tool("read_codex_task", identity)).structured_content
                self.assertEqual(semantic["status"], "blocked")
                self.assertEqual(semantic["native"]["calls"], 0)
                self.assertTrue(semantic["controller_review_required"])
                again = await session.call_tool("start_codex_task", args)
                self.assertFalse(again.is_error)
                self.assertEqual(again.structured_content["created_at"], original["created_at"])
                conflict = await session.call_tool("start_codex_task", args | {"goal": "Different goal"})
                self.assertTrue(conflict.is_error)
                self.assertEqual(conflict.structured_content["error"]["code"], "execution_conflict")
