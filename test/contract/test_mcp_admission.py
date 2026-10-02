"""Independent observation and cancellation admission capacity."""

import asyncio
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import Mock, patch
from mcp.server.mcpserver.exceptions import ToolError
from openmachine import server
from openmachine.config import Settings


class ObservationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_cap_and_cancelled_await_retain_the_actual_thread_slot(self):
        with tempfile.TemporaryDirectory(prefix="om-read-limit-", dir="/tmp") as directory:
            root = Path(directory)
            config = Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}},
                                              "workspaces": {"work": {"machine": "local", "root": str(root)}}})
            application = Mock()
            application.get_capabilities.return_value = {"alive": True}
            application.cancel_execution.return_value = {"cancel_requested": True}
            application.control_execution.return_value = {"control_result": {"status": "applied"}}
            admitted = threading.Event()
            release = threading.Event()
            counter_lock = threading.Lock()
            calls = []

            def read(machine, identity, offset, size, wait_seconds, *, cancelled):
                if wait_seconds:
                    with counter_lock:
                        calls.append(cancelled)
                        if len(calls) == 16:
                            admitted.set()
                    if not release.wait(5):
                        raise AssertionError("The test did not release its admitted observers")
                return {"status": "running", "wait_expired": bool(wait_seconds)}

            application.read_execution.side_effect = read
            with patch.object(server, "create_application", return_value=application):
                mcp = server.create_server(config, root / "state")
            args = {"machine": "local", "execution_id": "a" * 32, "wait_seconds": 5}
            with patch.object(server, "ThreadPoolExecutor", side_effect=OSError("fixture unavailable")):
                with self.assertRaises(ToolError):
                    await mcp.call_tool("read_execution", args)
            readers = [asyncio.create_task(mcp.call_tool("read_execution", args)) for _ in range(16)]
            try:
                self.assertTrue(await asyncio.to_thread(admitted.wait, 3))
                overflow = await mcp.call_tool("read_execution", args)
                self.assertEqual(overflow.structured_content["error"]["code"], "reader_busy")
                self.assertFalse((await mcp.call_tool("get_capabilities", {})).is_error)
                self.assertFalse((await mcp.call_tool("read_execution", args | {"wait_seconds": 0})).is_error)
                self.assertFalse((await mcp.call_tool("cancel_execution", {"machine": "local", "execution_id": "a" * 32})).is_error)
                readers[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await readers[0]
                self.assertTrue(any(event.is_set() for event in calls))
                # The fake observer intentionally has not exited yet. Its slot
                # cannot be reused just because the asyncio await was cancelled.
                overflow = await mcp.call_tool("read_execution", args)
                self.assertEqual(overflow.structured_content["error"]["code"], "reader_busy")
            finally:
                release.set()
                await asyncio.gather(*readers, return_exceptions=True)
            result = await mcp.call_tool("read_execution", args)
            self.assertFalse(result.is_error)

    async def test_cancellation_has_capacity_when_immediate_reads_fill_default_executor(self):
        with tempfile.TemporaryDirectory(prefix="om-cancel-capacity-", dir="/tmp") as directory:
            root = Path(directory)
            config = Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}},
                                              "workspaces": {"work": {"machine": "local", "root": str(root)}}})
            application = Mock()
            release = threading.Event()
            filled = threading.Event()
            lock = threading.Lock()
            count = 0

            def blocked_read(*args, **kwargs):
                nonlocal count
                with lock:
                    count += 1
                    if count == 2:
                        filled.set()
                if not release.wait(5):
                    raise AssertionError("The test did not release its immediate readers")
                return {"status": "running"}

            application.read_execution.side_effect = blocked_read
            application.cancel_execution.return_value = {"cancel_requested": True}
            application.control_execution.return_value = {"control_result": {"status": "applied"}}
            with patch.object(server, "create_application", return_value=application):
                mcp = server.create_server(config, root / "state")
            asyncio.get_running_loop().set_default_executor(server.ThreadPoolExecutor(max_workers=2))
            args = {"machine": "local", "execution_id": "b" * 32}
            readers = [asyncio.create_task(mcp.call_tool("read_execution", args)) for _ in range(2)]
            try:
                deadline = time.monotonic() + 3
                while not filled.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(filled.is_set())
                result = await asyncio.wait_for(mcp.call_tool("cancel_execution", args), 1)
                self.assertFalse(result.is_error)
                self.assertTrue(result.structured_content["cancel_requested"])
                application.cancel_execution.assert_called_once_with("local", "b" * 32, None)
                controlled = await asyncio.wait_for(mcp.call_tool("control_execution", args | {"expected_control_seq": 0, "action": "interrupt"}), 1)
                self.assertEqual(controlled.structured_content["control_result"]["status"], "applied")
                application.control_execution.assert_called_once_with("local", "b" * 32, 0, "interrupt", None, None, None)
                self.assertFalse(any(task.done() for task in readers))
            finally:
                release.set()
                await asyncio.gather(*readers, return_exceptions=True)

    async def test_execution_reply_counts_both_json_representations(self):
        payload = {"output": "\ufffd" * 200000, "offset": 0, "next_offset": 200000,
                   "output_presentation_truncated": False}
        result = server._reply(lambda: payload, execution_output=True)
        encoded = json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()
        self.assertFalse(result.is_error)
        self.assertLessEqual(len(encoded), server.MAX_RESULT_BYTES - 1024)
        self.assertTrue(result.structured_content["output_presentation_truncated"])
        self.assertEqual(result.structured_content["next_offset"], 200000)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertFalse(payload["output_presentation_truncated"])
