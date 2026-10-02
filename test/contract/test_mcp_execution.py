"""Behavior checks for MCPContractTests."""

import asyncio
import hashlib
import os
import sys
import time
import unittest
import uuid
from mcp import Client, ClientSession
from openmachine import execution
from support_mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_grace_schema_rejects_invalid_values_before_admission(self):
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for value in (True, False, "5", -1, 31):
                    identity = uuid.uuid4().hex
                    result = await session.call_tool("start_execution", {
                        "machine": "desktop", "execution_id": identity, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                        "termination_grace_seconds": value})
                    self.assertTrue(result.is_error, value)
                    self.assertFalse((self.state / "executions" / identity).exists())
                caps = (await session.call_tool("get_capabilities", {})).structured_content
                self.assertEqual(caps["execution"]["receipt_format"], 5)
                self.assertEqual(caps["execution"]["io_modes"], ["pipe", "pty"])
                self.assertEqual(caps["execution"]["controls"], ["interrupt", "resize", "write_stdin"])

    async def test_reconnected_client_finds_and_cancels_original_execution(self):
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

    async def test_large_explicit_payload_and_changed_instruction_cancellation(self):
        scope = {"machine": "desktop", "workspace_name": "example"}
        identity = uuid.uuid4().hex
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                for name in ("save_task", "write_workspace_file"):
                    self.assertFalse(tools[name].annotations.read_only_hint)
                    self.assertTrue(tools[name].annotations.destructive_hint)
                    self.assertTrue(tools[name].annotations.idempotent_hint)
                    self.assertFalse(tools[name].annotations.open_world_hint)
                content = "\x01" * 131072
                write = await session.call_tool("write_workspace_file", {**scope, "path": "large.txt", "content": content,
                    "expected_head_sha": "", "expected_sha256": ""})
                self.assertFalse(write.is_error, write)
                self.assertEqual(write.structured_content["status"], "written")
                digest = hashlib.sha256(content.encode()).hexdigest()
                offset, pieces = 0, []
                while True:
                    page = (await session.call_tool("read_workspace_file", {**scope, "path": "large.txt",
                        "offset": offset, "expected_sha256": digest})).structured_content
                    self.assertLessEqual(len(page["text"].encode()), 32768)
                    pieces.append(page["text"])
                    offset = page["next_offset"]
                    if not page["has_more"]:
                        break
                self.assertEqual("".join(pieces), content)
                checkpoint = {"task_id": "changed-goal", "title": "Short cancellation fixture", "anchors": []}
                initial = await session.call_tool("save_task", {**checkpoint, "expected_revision": 0,
                    "body": "Goal: observe one short owned command.\nPlanned execution: " + identity + "\n"})
                self.assertFalse(initial.is_error, initial)
                start = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": identity,
                    "cwd": str(self.work), "argv": [sys.executable, "-c", "import time; time.sleep(20)"], "task_id": "changed-goal"})
                self.assertFalse(start.is_error, start)
                revised = await session.call_tool("save_task", {**checkpoint, "expected_revision": 1,
                    "body": "Changed fixture instruction: stop the original command.\nOriginal execution: " + identity + "\nNext: cancel and read termination.\n"})
                self.assertFalse(revised.is_error, revised)
                cancel = await session.call_tool("cancel_execution", {"machine": "desktop", "execution_id": identity})
                self.assertFalse(cancel.is_error, cancel)
                for _ in range(100):
                    receipt = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": identity})
                    if receipt.structured_content["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.03)
                self.assertEqual(receipt.structured_content["status"], "cancelled")
                conflict = await session.call_tool("save_task", {**checkpoint, "expected_revision": 1, "body": "Stale progress."})
                self.assertTrue(conflict.is_error)
                self.assertEqual(conflict.structured_content["error"]["code"], "task_conflict")
                final = await session.call_tool("save_task", {**checkpoint, "expected_revision": 2,
                    "body": "Changed instruction retained. Original execution " + identity + " is cancelled. No replacement was started.\n"})
                self.assertEqual(final.structured_content["revision"], 3)
        self.assertEqual(len(list((self.state / "executions").iterdir())), 1)

    async def test_wait_schema_and_quiet_expiry_keep_original_execution(self):
        identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for value in (True, False, "1", -0.1, 5.1):
                    result = await session.call_tool("read_execution", identity | {"wait_seconds": value})
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error"]["code"], "invalid_tool_call")
                caps = (await session.call_tool("get_capabilities", {})).structured_content
                self.assertEqual(caps["execution"]["wait_seconds"], {"default": 0, "min": 0, "max": 5})
                self.assertEqual(caps["execution"]["max_waiting_readers"], 16)
                started = await session.call_tool("start_execution", identity | {
                    "cwd": str(self.work), "argv": [sys.executable, "-c", 'import time; print("ready",flush=True); time.sleep(10)'],
                    "timeout_seconds": 15})
                self.assertFalse(started.is_error)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        ready = (await session.call_tool("read_execution", identity | {"wait_seconds": 1})).structured_content
                        if "ready" in ready.get("output", ""):
                            break
                    self.assertIn("ready", ready["output"])
                    path = self.state / "executions" / identity["execution_id"] / "record.json"
                    original = path.read_bytes()
                    observed = await session.call_tool("read_execution", identity | {"offset": ready["next_offset"], "wait_seconds": 0.1})
                    self.assertFalse(observed.is_error)
                    self.assertTrue(observed.structured_content["wait_expired"])
                    self.assertEqual(observed.structured_content["status"], "running")
                    self.assertEqual(path.read_bytes(), original)
                finally:
                    await session.call_tool("cancel_execution", identity)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        result = (await session.call_tool("read_execution", identity | {"wait_seconds": 1})).structured_content
                        if result.get("status") == "cancelled":
                            break
                    self.assertEqual(result["status"], "cancelled")

    async def test_disconnected_waiter_preserves_command_and_other_operations(self):
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

    async def test_interactive_worker_survives_controller_restart_without_replay(self):
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

    async def test_interactive_sdk_schema_rejects_invalid_effects_before_admission(self):
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                for delta in ({"tty": 1}, {"tty": "true"}, {"tty": True, "terminal_size": {"rows": True, "cols": 80}},
                              {"tty": True, "terminal_size": {"rows": 24, "cols": 80, "extra": 1}},
                              {"terminal_size": {"rows": 24, "cols": 80}}):
                    identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
                    result = await session.call_tool("start_execution", identity | {
                        "cwd": str(self.work), "argv": [sys.executable, "-c", "pass"]} | delta)
                    self.assertTrue(result.is_error)
                    self.assertFalse((self.state / "executions" / identity["execution_id"]).exists())
                identity = {"machine": "desktop", "execution_id": uuid.uuid4().hex}
                for delta in ({"expected_control_seq": True}, {"expected_control_seq": 2**53 - 1},
                              {"action": "close_stdin"}, {"action": "write_stdin", "text": "\0"},
                              {"action": "write_stdin", "text": "é" * 8193},
                              {"action": "resize", "terminal_size": {"rows": 1, "cols": 1001}}):
                    result = await session.call_tool("control_execution", identity | {
                        "expected_control_seq": 0, "action": "interrupt"} | delta)
                    self.assertTrue(result.is_error)
                    self.assertNotEqual(result.structured_content["error"]["code"], "execution_not_found")
                self.assertFalse((self.state / "executions" / identity["execution_id"]).exists())

    async def test_modern_overlapping_clients_and_abandoned_wait_preserve_original_execution(self):
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
