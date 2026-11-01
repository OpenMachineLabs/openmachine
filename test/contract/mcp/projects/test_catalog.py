"""Installed private-socket project catalog and unchanged workspace contracts."""

import json
import unittest

from mcp import ClientSession
from test.support.mcp import MCPFixture


class ProjectCatalogContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_discovery_selection_revalidation_and_legacy_reads(self):
        (self.work / "project").mkdir()
        (self.work / "project/private.txt").write_text("File contents are not catalog metadata.\n")
        self.config.write_text(self.config.read_text().replace("execution_enabled = true", "execution_enabled = false"))
        self.launch()
        scope = {"machine": "desktop", "workspace_name": "example"}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

                async def call(name, args):
                    result = await session.call_tool(name, args)
                    self.assertFalse(result.is_error, result.structured_content)
                    self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                    return result.structured_content

                capabilities = await call("get_capabilities", {})
                self.assertEqual(capabilities["project_catalog"]["protocol"], 1)
                self.assertEqual(capabilities["project_catalog"]["transports"], ["local"])
                self.assertFalse(capabilities["project_catalog"]["register"])
                roots = await call("inspect_projects", {"machine": "desktop"})
                self.assertEqual(roots["projects"][0]["kind"], "workspace")
                listing = await call("inspect_projects", scope)
                target = listing["projects"][0]
                self.assertEqual(target["kind"], "folder")
                self.assertNotIn("private.txt", json.dumps(listing))
                selected = await call("inspect_projects", scope | {"action": "inspect", "path": "project",
                    "expected_target_id": target["target_id"]})
                self.assertEqual(selected["project"], target)
                guide = await call("inspect_skills", {"action": "read", "tool": "inspect_projects"})
                self.assertIn("expected_target_id", guide["text"])
                legacy = await call("read_workspace_file", scope | {"path": "AGENTS.md"})
                self.assertIn("Only modify", legacy["text"])
                (self.work / "project").rename(self.work / "old")
                (self.work / "project").mkdir()
                changed = await session.call_tool("inspect_projects", scope | {"action": "inspect", "path": "project",
                    "expected_target_id": target["target_id"]})
                self.assertEqual(changed.structured_content["error"]["code"], "project_changed")

    async def test_invalid_wire_arguments_are_rejected_before_discovery(self):
        self.launch()
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                for delta in ({"limit": True}, {"limit": 51}, {"offset": -1}, {"path": 1},
                              {"expected_target_id": "bad"}, {"action": "register"}, {"unexpected": True}):
                    response = await self.wire_call(client, "tools/call", {"name": "inspect_projects",
                        "arguments": {"machine": "desktop", "workspace_name": "example", **delta}})
                    result = response.json()["result"]
                    self.assert_wire_tool_result(result, version)
                    self.assertTrue(result["isError"])
                    self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_tool_call")
