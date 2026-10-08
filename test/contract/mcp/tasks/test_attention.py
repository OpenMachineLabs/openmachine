"""MCP foreground task wait contract."""

import sys
import unittest
import uuid

from mcp import Client
from openmachine import tasks
from test.support.mcp import MCPFixture


class MCPAttentionContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_expired_claim_returns_recoverable_wire_error_and_same_turn_renewal(self):
        self.launch()
        async with Client(self.client(), mode="2026-07-28", cache=None) as client:
            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                self.assertFalse(result.is_error, result.structured_content)
                return result.structured_content

            scope = {"machine": "desktop", "workspace_name": "example"}
            context = (await call("read_workspace_file", scope | {"path": "AGENTS.md"}))["context"]
            identity = "expired-claim-contract"
            request = self.task_request(context, identity)
            request["contract"]["tools"] = ["control_task", "read_task", "wait_task", "write_workspace_file"]
            created = (await call("create_task", request))["record"]
            claimed = (await call("control_task", {
                "task_id": identity, "expected_revision": created["revision"],
                "expected_plan_revision": created["plan"]["revision"],
                "expected_epoch": created["control"]["epoch"], "update_id": uuid.uuid4().hex,
                "transition": {"action": "claim", "handoff_id": created["handoff"]["id"]}}))["record"]
            tasks.update(self.state, identity, lambda record, now:
                         record["control"]["owner"].update(expires_at_ms=now - 1))
            current = (await call("read_task", {"task_id": identity}))["record"]
            waited = await call("wait_task", {"task_id": identity, "expected_revision": current["revision"],
                                              "wait_seconds": 0.0})
            self.assertEqual(waited["wait"]["status"], "renewal_required")
            self.assertFalse(waited["foreground"]["stop_foreground"])
            change_id = uuid.uuid4().hex
            arguments = scope | {"path": "renewed.txt", "content": "continued\n", "expected_head_sha": "",
                "expected_sha256": "", "expected_workspace_identity": context["workspace_identity"],
                "change_id": change_id, "task_context": {"task_id": identity, "task_revision": current["revision"],
                    "plan_revision": current["plan"]["revision"], "continuation_epoch": current["control"]["epoch"],
                    "operation_id": change_id}}
            blocked = await client.call_tool("write_workspace_file", arguments)
            self.assertTrue(blocked.is_error)
            self.assertEqual(blocked.structured_content["error"]["code"], "task_renewal_required")
            self.assertFalse(blocked.structured_content["foreground"]["stop_foreground"])
            self.assertFalse((self.work / "renewed.txt").exists())
            self.assertEqual((await call("read_task", {"task_id": identity}))["record"], current)
            renewed = (await call("control_task", {
                "task_id": identity, "expected_revision": current["revision"],
                "expected_plan_revision": current["plan"]["revision"],
                "expected_epoch": current["control"]["epoch"], "update_id": uuid.uuid4().hex,
                "transition": {"action": "renew", "claim_id": claimed["control"]["owner"]["claim_id"]}}))["record"]
            self.assertEqual(renewed["control"]["epoch"], current["control"]["epoch"])
            arguments["task_context"]["task_revision"] = renewed["revision"]
            self.assertEqual((await call("write_workspace_file", arguments))["status"], "applied")
            self.assertEqual((self.work / "renewed.txt").read_text(), "continued\n")

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
