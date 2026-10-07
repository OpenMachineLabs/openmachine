"""contract mcp ssh test_discovery behavior."""

import unittest
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_ssh_metadata_and_local_independence(self):
        with self.config.open("a") as output:
            output.write('\n[machines.build]\ntransport="ssh"\nssh_alias="uncontacted-host"\n'
                         'ssh_worker="/remote/' + 'a' * 64 + '.pyz"\n'
                         '[workspaces.remote]\nmachine="build"\nroot="/remote/not-on-controller"\n')
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                discovered = await session.list_tools()
                self.assert_tool_contract(discovered, github=True, ssh=True)
                tools = {item.name: item for item in discovered.tools}
                self.assertTrue(tools["read_workspace_file"].annotations.open_world_hint)
                self.assertTrue(tools["write_workspace_file"].annotations.open_world_hint)
                self.assertFalse(tools["save_task"].annotations.open_world_hint)
                caps = (await session.call_tool("get_capabilities", {})).structured_content
                self.assertIn("ssh", caps["integrations"])
                denied = await session.call_tool("read_workspace_file", {"machine": "build", "workspace_name": "remote", "path": "x"})
                self.assertEqual(denied.structured_content["error"]["code"], "identity_required")
                local = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example", "action": "read", "path": "AGENTS.md"})
                self.assertFalse(local.is_error)
