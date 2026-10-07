"""resilience service_runtime test_mcp_listener behavior."""

import subprocess
import sys
import unittest
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_second_server_does_not_replace_existing_socket(self):
        self.launch()
        inode = self.socket.stat().st_ino
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                 "--state-dir", str(self.state), "serve"], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.socket.stat().st_ino, inode)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                self.assertFalse((await session.call_tool("get_capabilities", {})).is_error)

    async def test_managed_stale_socket_recovery_preserves_live_owner(self):
        self.launch()
        # Leave this owned listener's socket behind without depending on graceful
        # shutdown timing or trying to bind over a runner's existing stale path.
        self.processes[0].kill()
        self.processes[0].wait(timeout=5)
        self.assertTrue(self.socket.exists())
        self.launch(managed=True)
        inode = self.socket.stat().st_ino
        duplicate = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                    "--state-dir", str(self.state), "serve", "--managed-child"],
                                   capture_output=True, timeout=10)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(self.socket.stat().st_ino, inode)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                caps = await session.call_tool("get_capabilities", {})
                self.assertEqual(caps.structured_content["lifecycle"]["pid"], self.processes[-1].pid)
