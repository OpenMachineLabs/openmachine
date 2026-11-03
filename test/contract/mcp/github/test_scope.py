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
                    ("github_read", {"action": "workflow_runs", "repository": "Other/repo"}),
                    ("github_workflow", {"action": "dispatch", "repository": "Other/repo", "workflow": "ci.yml", "ref": "main", "expected_head_sha": "a" * 40}),
                    ("github_issue", {"action": "create", "repository": "Other/repo", "title": "not submitted"}),
                    ("github_merge", {"repository": "Other/repo", "number": 1, "expected_head_sha": "a" * 40}),
                    ("github_release", {"action": "create", "repository": "Other/repo", "tag_name": "v1", "body": "Notes"}),
                    ("github_release", {"action": "publish", "repository": "Other/repo", "tag_name": "v1",
                                        "release_id": 1, "expected_body_sha256": "a" * 64}),
                    ("github_review_thread", {"action": "resolve", "repository": "Other/repo", "number": 1, "thread_id": "PRRT_fixture", "expected_head_sha": "a" * 40}),
                ):
                    result = await session.call_tool(name, arguments)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], "github_not_allowed")

    async def test_release_unknown_fields_fail_before_publication(self):
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("github_release", {
                    "action": "create", "repository": "Example/repo",
                    "tag_name": "v1.2.6", "body": "Notes",
                    "draft": False, "prereleasee": True})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "invalid_tool_call")

    async def test_workflow_unknown_fields_and_non_scalar_inputs_fail_before_dispatch(self):
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                args = {"action": "dispatch", "repository": "Example/repo", "workflow": "ci.yml", "ref": "main", "expected_head_sha": "a" * 40}
                for extra in ({"endpoint": "arbitrary"}, {"inputs": {"nested": {}}}, {"expected_attempt": True}):
                    result = await session.call_tool("github_workflow", args | extra)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], "invalid_tool_call")
                result = await session.call_tool("github_read", {"action": "workflow_runs", "repository": "Example/repo", "filters": {"url": "arbitrary"}})
                self.assertEqual(result.structured_content["error"]["code"], "invalid_tool_call")
