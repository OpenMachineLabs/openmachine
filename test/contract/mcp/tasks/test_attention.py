"""MCP foreground task wait contract."""

import sys
import unittest
import uuid

from mcp import Client
from test.support.mcp import MCPFixture


class MCPAttentionContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_wait_task_returns_one_bounded_foreground_stop(self):
        self.launch()
        async with Client(self.client(), mode="2026-07-28", cache=None) as client:
            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                self.assertFalse(result.is_error, result.structured_content)
                return result.structured_content

            scope = {"machine": "desktop", "workspace_name": "example"}
            context = (await call("read_workspace_file", scope | {"path": "AGENTS.md"}))["context"]
            identity = "foreground-wait-contract"
            request = self.task_request(context, identity)
            request["contract"]["tools"] = ["control_task", "start_execution", "read_execution",
                                                    "cancel_execution", "read_task", "wait_task"]
            created = (await call("create_task", request))["record"]
            claimed = (await call("control_task", {
                "task_id": identity, "expected_revision": created["revision"],
                "expected_plan_revision": created["plan"]["revision"],
                "expected_epoch": created["control"]["epoch"], "update_id": uuid.uuid4().hex,
                "transition": {"action": "claim", "handoff_id": created["handoff"]["id"]}}))["record"]
            execution_id = uuid.uuid4().hex
            started = await call("start_execution", {"machine": "desktop", "execution_id": execution_id,
                "cwd": str(self.work.resolve()), "argv": [sys.executable, "-c", "import time; time.sleep(10)"],
                "timeout_seconds": 20, "expected_head_sha": "", "workspace_access": "exclusive",
                "task_context": {"task_id": identity, "task_revision": claimed["revision"],
                    "plan_revision": claimed["plan"]["revision"],
                    "continuation_epoch": claimed["control"]["epoch"], "operation_id": execution_id}})
            self.assertIn(started["status"], {"accepted", "running"})
            current = await self.observer_task_view(client, identity)
            waited = await call("wait_task", {"task_id": identity,
                "expected_revision": current["record"]["revision"], "wait_seconds": 0.0})
            self.assertEqual((waited["wait"]["status"], waited["foreground"]["status"],
                              waited["foreground"]["stop_foreground"]),
                             ("expired", "wait_expired", True))
            self.assertIn(identity, waited["foreground"]["resume_prompt"])
            await call("cancel_execution", {"machine": "desktop", "execution_id": execution_id})
            await self.execution_until(client, {"machine": "desktop", "execution_id": execution_id},
                                       lambda value: value["status"] in {"cancelled", "succeeded", "failed"})

    async def test_wait_task_schema_is_strict_and_bounded(self):
        self.launch()
        async with Client(self.client(), mode="2026-07-28", cache=None) as client:
            for arguments in ({"task_id": "missing", "expected_revision": 1, "wait_seconds": 30.1},
                              {"task_id": "missing", "expected_revision": True, "wait_seconds": 0.0},
                              {"task_id": "missing", "expected_revision": 1, "wait_seconds": 0.0,
                               "retry": True}):
                result = await client.call_tool("wait_task", arguments)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], "invalid_tool_call")


if __name__ == "__main__":
    unittest.main()
