"""contract mcp github test_scope behavior."""

import unittest
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_github_scope_rejects_before_connection(self):
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for name, arguments in (
                    ("github_read", {"action": "repository", "repository": "Other/repo"}),
                    ("github_issue", {"action": "create", "repository": "Other/repo", "title": "not submitted"}),
                    ("github_merge", {"repository": "Other/repo", "number": 1, "expected_head_sha": "a" * 40}),
                    ("github_release", {"action": "create", "repository": "Other/repo", "tag_name": "v1", "body": "Notes"}),
                    ("github_review_thread", {"action": "resolve", "repository": "Other/repo", "number": 1, "thread_id": "PRRT_fixture", "expected_head_sha": "a" * 40}),
                ):
                    result = await session.call_tool(name, arguments)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], "github_not_allowed")
