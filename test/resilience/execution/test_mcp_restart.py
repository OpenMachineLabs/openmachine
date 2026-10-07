"""resilience execution test_mcp_restart behavior."""

import asyncio
import os
import sys
import unittest
import uuid
from mcp import ClientSession
from openmachine import execution
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_interactive_worker_survives_controller_restart_without_replay(self):
        self.launch()
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        code = ('import os,time; from pathlib import Path; p=Path("start-count"); '
                'p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); '
                'Path("process-identity").write_text(f"{os.getpid()} {os.getppid()}"); '
                'print("ready",flush=True); x=input(); print("first-read",flush=True); '
                'y=input(); print("same-process",x,y,flush=True); time.sleep(8)')
        try:
            async with self.client() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    started = await session.call_tool("start_execution", identity | {
                        "cwd": str(self.work), "argv": [sys.executable, "-c", code], "timeout_seconds": 15, "tty": True})
                    self.assertFalse(started.is_error)
                    ready = await self.execution_until(session, identity, lambda r: r["control_ready"] and "ready" in r["output"])
                    sent = await session.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "write_stdin", "text": "41\n"})
                    self.assertFalse(sent.is_error)
                    receipt = sent.structured_content["control_result"]
                    self.assertEqual(receipt["status"], "applied")
                    self.assertNotIn("output", sent.structured_content)
                    before = await self.execution_until(session, identity, lambda r: "first-read" in r["output"])
            original = execution.read_record(self.state / "executions" / identity["execution_id"])
            original_processes = (self.work / "process-identity").read_text()
            self.stop(self.processes[0])
            self.assertFalse(self.socket.exists())
            restarted = self.launch()
            self.assertNotEqual(restarted.pid, self.processes[0].pid)
            async with self.client() as (reader, writer):
                async with ClientSession(reader, writer) as fresh:
                    await fresh.initialize()
                    observed = await self.execution_until(fresh, identity, lambda r: r["control"]["seq"] == 1)
                    self.assertEqual(observed["created_at"], ready["created_at"])
                    self.assertEqual(observed["context"], ready["context"])
                    retained = execution.read_record(self.state / "executions" / identity["execution_id"])
                    self.assertEqual((self.work / "process-identity").read_text(), original_processes)
                    for pid in map(int, original_processes.split()):
                        os.kill(pid, 0)
                    self.assertEqual(retained["command"], original["command"])
                    self.assertEqual(retained["request"], original["request"])
                    self.assertEqual(observed["control"]["last"], receipt)
                    cursor = (await fresh.call_tool("read_execution", identity | {
                        "offset": before["next_offset"]})).structured_content
                    self.assertEqual(cursor["next_offset"], before["next_offset"])
                    self.assertFalse(cursor["output_gap"])
                    self.assertEqual(cursor["output"], "")
                    duplicate = await fresh.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "write_stdin", "text": "41\n"})
                    self.assertFalse(duplicate.is_error)
                    self.assertEqual(duplicate.structured_content["control_result"], receipt)
                    second = await fresh.call_tool("control_execution", identity | {
                        "expected_control_seq": 1, "action": "write_stdin", "text": "1\n"})
                    self.assertFalse(second.is_error)
                    await self.execution_until(fresh, identity, lambda r: "same-process 41 1" in r["output"])
                    self.assertEqual((self.work / "start-count").read_text(), "1")
                    self.assertFalse((await fresh.call_tool("cancel_execution", identity)).is_error)
                    final = await self.execution_until(fresh, identity, lambda r: r["status"] == "cancelled")
                    self.assertEqual(final["cleanup"]["status"], "verified")
                    self.assertEqual(final["control"]["seq"], 2)
        finally:
            async with self.client() as (reader, writer):
                async with ClientSession(reader, writer) as cleanup:
                    await cleanup.initialize()
                    await cleanup.call_tool("cancel_execution", identity)

    async def test_real_write_survives_service_restart_and_is_not_replayed(self):
        self.launch()
        execution_id = uuid.uuid4().hex
        request = {"machine": "desktop", "execution_id": execution_id, "cwd": str(self.work),
                   "argv": [sys.executable, "-c", 'import time; from pathlib import Path; time.sleep(0.7); p=Path("result.txt"); p.write_text(p.read_text()+"done" if p.exists() else "done"); print("complete")']}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                rules = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "read", "path": "AGENTS.md"})
                self.assertIn("disposable", rules.structured_content["text"])
                started = await session.call_tool("start_execution", request)
                self.assertFalse(started.is_error, started)
        self.stop(self.processes[0])
        self.assertFalse(self.socket.exists(), "Graceful shutdown must remove its own socket")
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for _ in range(100):
                    result = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})
                    self.assertFalse(result.is_error, result)
                    if result.structured_content["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(result.structured_content["exit_code"], 0)
                self.assertIn("complete", result.structured_content["output"])
                again = await session.call_tool("start_execution", request)
                self.assertEqual(again.structured_content["status"], "succeeded")
                changed = await session.call_tool("start_execution", {**request, "argv": ["false"]})
                self.assertTrue(changed.is_error)
                self.assertEqual(changed.structured_content["error"]["code"], "execution_conflict")
                observed = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                            "action": "read", "path": "result.txt"})
                self.assertEqual(observed.structured_content["text"], "done")
