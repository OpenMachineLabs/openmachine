"""contract mcp server test_catalog behavior."""

import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest
from mcp import Client, ClientSession
import openmachine
from openmachine import skills
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    def guide_selector(self, tool):
        calls = [ast.parse("inspect_skills(" + arguments + ")", mode="eval").body
                 for arguments in re.findall(r"inspect_skills\(([^()]*)\)", tool.description or "")]
        selectors = [{keyword.arg: ast.literal_eval(keyword.value) for keyword in call.keywords}
                     for call in calls if not call.args]
        matching = [selector for selector in selectors if selector.get("tool") == tool.name]
        self.assertEqual(matching, [{"action": "read", "tool": tool.name}], tool.name)
        return matching[0]

    async def test_tool_contract_without_github(self):
        local = self.config.read_text().split("[github]")[0]
        for ssh in (False, True):
            with self.subTest(ssh=ssh):
                config = local
                if ssh:
                    config += ('\n[machines.build]\ntransport="ssh"\nssh_alias="uncontacted-host"\n'
                               'ssh_worker="/remote/' + 'a' * 64 + '.pyz"\n'
                               '[workspaces.remote]\nmachine="build"\nroot="/remote/not-on-controller"\n')
                self.config.write_text(config)
                process = self.launch()
                try:
                    async with self.client() as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            discovered = await session.list_tools()
                            self.assert_tool_contract(discovered, github=False, ssh=ssh)
                            for tool in discovered.tools:
                                self.guide_selector(tool)
                            caps = (await session.call_tool("get_capabilities", {})).structured_content
                            self.assertEqual(caps["integrations"], ["ssh"] if ssh else [])
                finally:
                    self.stop(process)

    async def test_installed_package_discovery_and_socket_permissions(self):
        self.launch()
        self.assertTrue(Path(openmachine.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                self.assertEqual(init.server_info.version, openmachine.__version__)
                discovered = await session.list_tools()
                self.assert_tool_contract(discovered, github=True, ssh=False)
                tools = {t.name: t for t in discovered.tools}
                self.assertEqual(set(tools), {"get_capabilities", "inspect_workspace", "inspect_skills", "start_execution", "create_worktree", "read_execution", "list_executions", "cancel_execution", "reconcile_execution", "control_execution", "github_issue", "github_pull_request", "github_comment", "github_review", "github_review_thread", "github_read", "github_merge", "github_project", "read_workspace_file", "write_workspace_file", "edit_workspace_file", "read_workspace_change", "apply_workspace_patch", "find_tasks", "read_task", "wait_task", "save_task", "create_task", "control_task", "list_integrations", "connect_integration", "read_integration_login", "cancel_integration_login", "disconnect_integration", "start_codex_task", "read_codex_task"})
                self.assertFalse(tools["start_execution"].annotations.read_only_hint)
                self.assertTrue(tools["start_execution"].annotations.destructive_hint)
                self.assertTrue(tools["inspect_workspace"].annotations.read_only_hint)
                self.assertTrue(tools["wait_task"].annotations.read_only_hint)
                self.assertFalse(tools["cancel_execution"].annotations.read_only_hint)
                self.assertFalse(tools["control_execution"].annotations.read_only_hint)
                self.assertFalse(tools["control_execution"].annotations.idempotent_hint)
                self.assertFalse(tools["github_issue"].annotations.read_only_hint)
                self.assertTrue(tools["github_read"].annotations.read_only_hint)
                self.assertTrue(tools["github_read"].annotations.open_world_hint)
                for name in ("github_issue", "github_pull_request", "github_comment", "github_review", "github_review_thread", "github_merge", "github_project"):
                    self.assertFalse(tools[name].annotations.idempotent_hint)
                self.assertNotIn("endpoint", tools["github_read"].input_schema["properties"])
                timeout = tools["start_execution"].input_schema["properties"]["timeout_seconds"]
                self.assertEqual((timeout["default"], timeout["maximum"]), (600, 86400))
                task_wait = tools["wait_task"].input_schema["properties"]["wait_seconds"]
                self.assertEqual((task_wait["default"], task_wait["maximum"]), (30.0, 30.0))
                caps = (await session.call_tool("get_capabilities", {})).structured_content
                self.assertEqual(caps["machines"]["desktop"]["probe_status"], "not_probed")
                self.assertEqual(caps["integrations"], ["github"])
                self.assertEqual(caps["skill_navigation"], {
                    "entry": skills.for_tool("get_capabilities"), "tool": "inspect_skills", "selector": "tool"})
                for tool in discovered.tools:
                    with self.subTest(guide=tool.name):
                        selector = self.guide_selector(tool)
                        guide = await session.call_tool("inspect_skills", selector)
                        self.assertFalse(guide.is_error, guide)
                        binding = skills.for_tool(tool.name)
                        self.assertEqual({key: guide.structured_content[key] for key in ("skill", "path")}, binding)
                        self.assertTrue(guide.structured_content["text"])
                        self.assertFalse(guide.structured_content["has_more"])
                        self.assertEqual(guide.structured_content["snapshot"],
                                         hashlib.sha256(guide.structured_content["text"].encode()).hexdigest())
                for arguments, code in (({"tool": "missing_tool"}, "skill_unavailable"),
                                        ({"tool": "get_capabilities", "skill": "om.workspace"}, "invalid_skill_selector"),
                                        ({"tool": "get_capabilities", "path": "other.md"}, "invalid_skill_selector")):
                    rejected = await session.call_tool("inspect_skills", {"action": "read", **arguments})
                    self.assertTrue(rejected.is_error)
                    self.assertEqual(rejected.structured_content["error"]["code"], code)
                skill = await session.call_tool("inspect_skills", {"action": "read"})
                self.assertFalse(skill.is_error)
                self.assertIn("Operate Open Machine", skill.structured_content["text"])
                reference = await session.call_tool("inspect_skills", {"action": "read", "path": "worktrees.md"})
                self.assertFalse(reference.is_error)
                self.assertIn("Choose and reuse a worktree", reference.structured_content["text"])

    async def test_migration_protocols_preserve_all_four_catalogs(self):
        from importlib.metadata import version
        self.assertEqual(version("mcp"), "2.2.0")
        original = self.config.read_text()
        for github in (False, True):
            for ssh in (False, True):
                config = original if github else original.split("[github]")[0]
                if ssh:
                    config += '\n[machines.build]\ntransport="ssh"\nssh_alias="uncontacted-host"\nssh_worker="/remote/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pyz"\n'
                self.config.write_text(config)
                process = self.launch()
                try:
                    for mode in ("legacy", "2026-07-28", "auto"):
                        with self.subTest(github=github, ssh=ssh, mode=mode):
                            async with Client(self.client(), mode=mode, cache=None) as client:
                                expected = "2025-11-25" if mode == "legacy" else "2026-07-28"
                                self.assertEqual(client.protocol_version, expected)
                                self.assert_tool_contract(await client.list_tools(), github=github, ssh=ssh)
                                result = await client.call_tool("read_workspace_file", {
                                    "machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"})
                                self.assertFalse(result.is_error, result)
                                self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                                self.assertIn("disposable", result.structured_content["text"])
                finally:
                    self.stop(process)
