"""Real SDK calls to an installed wheel over a private socket; no cloud account."""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import openmachine


class MCPContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-mcp-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "AGENTS.md").write_text("Only modify this disposable project.\n")
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version = 1\n[machines.desktop]\ntransport = "local"\nexecution_enabled = true\n'
                               '[workspaces.example]\nmachine = "desktop"\nroot = ' + json.dumps(str(self.work)) + '\n')
        self.state = self.root / "state"
        self.socket = self.state / "mcp.sock"
        self.processes = []
        self.addCleanup(self.stop_all)
        self.launch()

    def launch(self):
        process = subprocess.Popen([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                    "--state-dir", str(self.state), "serve"],
                                   cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            if self.socket.exists():
                return process
            time.sleep(0.03)
        self.fail("Private MCP socket was not created")

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stderr.close()

    def stop_all(self):
        for process in self.processes:
            self.stop(process)

    @asynccontextmanager
    async def client(self):
        async with httpx.AsyncClient(trust_env=False, timeout=10,
                                     transport=httpx.AsyncHTTPTransport(uds=str(self.socket))) as client:
            async with streamable_http_client("http://localhost/mcp", http_client=client) as streams:
                yield streams

    async def test_installed_package_discovery_and_socket_permissions(self):
        self.assertTrue(Path(openmachine.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                self.assertEqual(init.serverInfo.version, openmachine.__version__)
                tools = {t.name: t for t in (await session.list_tools()).tools}
                self.assertEqual(set(tools), {"get_capabilities", "inspect_workspace", "inspect_skills", "start_execution", "read_execution"})
                self.assertFalse(tools["start_execution"].annotations.readOnlyHint)
                self.assertTrue(tools["start_execution"].annotations.destructiveHint)
                self.assertTrue(tools["inspect_workspace"].annotations.readOnlyHint)
                caps = (await session.call_tool("get_capabilities", {})).structuredContent
                self.assertEqual(caps["machines"]["desktop"]["probe_status"], "not_probed")
                skill = await session.call_tool("inspect_skills", {"action": "read"})
                self.assertFalse(skill.isError)
                self.assertIn("Operate Open Machine", skill.structuredContent["text"])

    async def test_real_write_survives_service_restart_and_is_not_replayed(self):
        execution_id = uuid.uuid4().hex
        request = {"machine": "desktop", "execution_id": execution_id, "cwd": str(self.work),
                   "argv": [sys.executable, "-c", 'import time; from pathlib import Path; time.sleep(0.7); p=Path("result.txt"); p.write_text(p.read_text()+"done" if p.exists() else "done"); print("complete")']}
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                rules = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "read", "path": "AGENTS.md"})
                self.assertIn("disposable", rules.structuredContent["text"])
                started = await session.call_tool("start_execution", request)
                self.assertFalse(started.isError, started)
        self.stop(self.processes[0])
        self.assertFalse(self.socket.exists(), "Graceful shutdown must remove its own socket")
        self.launch()
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for _ in range(100):
                    result = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})
                    self.assertFalse(result.isError, result)
                    if result.structuredContent["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(result.structuredContent["exit_code"], 0)
                self.assertIn("complete", result.structuredContent["output"])
                again = await session.call_tool("start_execution", request)
                self.assertEqual(again.structuredContent["status"], "succeeded")
                changed = await session.call_tool("start_execution", {**request, "argv": ["false"]})
                self.assertTrue(changed.isError)
                self.assertEqual(changed.structuredContent["error"]["code"], "execution_conflict")
                observed = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                            "action": "read", "path": "result.txt"})
                self.assertEqual(observed.structuredContent["text"], "done")

    async def test_large_file_offsets_remain_usable_through_mcp(self):
        (self.work / "large.txt").write_text("x" * 180000)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "read", "path": "large.txt", "offset": 131072})
                self.assertFalse(result.isError, result)
                self.assertEqual(result.structuredContent["next_offset"], 180000)

    async def test_disabled_write_and_invalid_origin_fail_closed(self):
        self.stop(self.processes[0])
        self.config.write_text(self.config.read_text().replace("execution_enabled = true", "execution_enabled = false"))
        self.launch()
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": uuid.uuid4().hex,
                                                                      "cwd": str(self.work), "argv": ["false"]})
                self.assertTrue(result.isError)
                self.assertEqual(result.structuredContent["error"]["code"], "execution_disabled")
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(self.socket)), trust_env=False) as client:
            result = await client.get("http://localhost/mcp", headers={"host": "untrusted.invalid"})
            self.assertEqual(result.status_code, 421)

    async def test_invalid_tool_arguments_are_rejected_without_echoing_values(self):
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "password=synthetic-secret"})
                self.assertTrue(result.isError)
                self.assertEqual(result.structuredContent["error"]["code"], "invalid_tool_call")
                self.assertNotIn("synthetic-secret", result.model_dump_json())

    async def test_second_server_does_not_replace_existing_socket(self):
        inode = self.socket.stat().st_ino
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                 "--state-dir", str(self.state), "serve"], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.socket.stat().st_ino, inode)
        async with self.client() as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                self.assertFalse((await session.call_tool("get_capabilities", {})).isError)


if __name__ == "__main__":
    unittest.main()
