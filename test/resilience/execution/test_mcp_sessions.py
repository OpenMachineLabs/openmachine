"""resilience execution test_mcp_sessions behavior."""

import asyncio
import sys
import time
import unittest
import uuid
from mcp import Client, ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_reconnected_client_finds_and_cancels_original_execution(self):
        self.launch()
        execution_id = uuid.uuid4().hex
        identity = {"machine": "desktop", "execution_id": execution_id}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("start_execution", {**identity, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", 'import time; print("started", flush=True); time.sleep(20)'],
                        "timeout_seconds": 86400})
                self.assertFalse(result.is_error, result)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listing = await session.call_tool("list_executions", {"machine": "desktop"})
                self.assertEqual(listing.structured_content["executions"][0]["execution_id"], execution_id)
                requested = await session.call_tool("cancel_execution", identity)
                self.assertFalse(requested.is_error, requested)
                self.assertTrue(requested.structured_content["cancel_requested"])
                for _ in range(100):
                    result = await session.call_tool("read_execution", identity)
                    self.assertFalse(result.is_error, result)
                    if result.structured_content["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(result.structured_content["status"], "cancelled")
                repeated = await session.call_tool("cancel_execution", identity)
                self.assertEqual(repeated.structured_content["status"], "cancelled")

    async def test_disconnected_waiter_preserves_command_and_other_operations(self):
        self.launch()
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        gate = self.work / "emit"
        code = ('import time; from pathlib import Path; p=Path("counter"); p.write_text("once"); print("ready",flush=True)\n'
                'while not Path("emit").exists(): time.sleep(.01)\n'
                'print("continued",flush=True)')
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as control:
                await control.initialize()
                started = await control.call_tool("start_execution", identity | {
                    "cwd": str(self.work), "argv": [sys.executable, "-c", code], "timeout_seconds": 15})
                self.assertFalse(started.is_error)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        ready = (await control.call_tool("read_execution", identity | {"wait_seconds": 1})).structured_content
                        if "ready" in ready.get("output", ""):
                            break
                    self.assertIn("ready", ready["output"])
                    async with self.client() as (other_reader, other_writer):
                        async with ClientSession(other_reader, other_writer) as other:
                            await other.initialize()
                            waiting = asyncio.create_task(other.call_tool("read_execution", identity | {
                                "offset": ready["next_offset"], "wait_seconds": 5}))
                            await asyncio.sleep(0.1)
                            self.assertFalse(waiting.done())
                            caps = await asyncio.wait_for(control.call_tool("get_capabilities", {}), 2)
                            self.assertFalse(caps.is_error)
                            now = await asyncio.wait_for(control.call_tool("read_execution", identity), 2)
                            self.assertEqual(now.structured_content["status"], "running")
                            waiting.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await waiting
                    # A new connection observes the original worker rather than a replacement.
                    async with self.client() as (fresh_reader, fresh_writer):
                        async with ClientSession(fresh_reader, fresh_writer) as fresh:
                            await fresh.initialize()
                            observed = await fresh.call_tool("read_execution", identity)
                            self.assertEqual(observed.structured_content["created_at"], started.structured_content["created_at"])
                            self.assertFalse(observed.structured_content["cancel_requested"])
                            self.assertEqual((self.work / "counter").read_text(), "once")
                            gate.touch()
                            deadline = time.monotonic() + 5
                            while time.monotonic() < deadline:
                                result = (await fresh.call_tool("read_execution", identity | {"wait_seconds": 1})).structured_content
                                if result.get("status") == "succeeded":
                                    break
                            self.assertEqual((result["status"], result["exit_code"]), ("succeeded", 0))
                            self.assertIn("continued", result["output"])
                finally:
                    gate.touch()
                    await control.call_tool("cancel_execution", identity)

    async def test_modern_overlapping_clients_and_abandoned_wait_preserve_original_execution(self):
        self.launch()
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        gate = self.work / "modern-emit"
        code = ('import time; from pathlib import Path; p=Path("modern-counter"); p.write_text("once"); '
                'print("ready",flush=True)\n'
                'while not Path("modern-emit").exists(): time.sleep(.01)\n'
                'print("continued",flush=True)')
        async with Client(self.client(), mode="2026-07-28", cache=None) as control:
            self.assertEqual(control.protocol_version, "2026-07-28")
            started = await control.call_tool("start_execution", identity | {
                "cwd": str(self.work), "argv": [sys.executable, "-c", code], "timeout_seconds": 15})
            self.assertFalse(started.is_error, started)
            try:
                ready = await self.execution_until(control, identity, lambda r: "ready" in r.get("output", ""))
                async with Client(self.client(), mode="2026-07-28", cache=None) as other:
                    waiting = asyncio.create_task(other.call_tool("read_execution", identity | {
                        "offset": ready["next_offset"], "wait_seconds": 5}))
                    await asyncio.sleep(0.1)
                    self.assertFalse(waiting.done())
                    caps = await asyncio.wait_for(control.call_tool("get_capabilities", {}), 2)
                    self.assertFalse(caps.is_error)
                    now = await asyncio.wait_for(control.call_tool("read_execution", identity), 2)
                    self.assertEqual(now.structured_content["status"], "running")
                    waiting.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await waiting
                async with Client(self.client(), mode="2026-07-28", cache=None) as fresh:
                    self.assertEqual(fresh.protocol_version, "2026-07-28")
                    observed = await fresh.call_tool("read_execution", identity)
                    self.assertEqual(observed.structured_content["created_at"], started.structured_content["created_at"])
                    self.assertFalse(observed.structured_content["cancel_requested"])
                    self.assertEqual((self.work / "modern-counter").read_text(), "once")
                    gate.touch()
                    result = await self.execution_until(fresh, identity, lambda r: r.get("status") == "succeeded")
                    self.assertEqual((result["status"], result["exit_code"]), ("succeeded", 0))
                    self.assertIn("continued", result["output"])
            finally:
                gate.touch()
                await control.call_tool("cancel_execution", identity)
