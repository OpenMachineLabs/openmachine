"""contract mcp tasks test_admission behavior."""

import sys
import unittest
import uuid
from mcp import Client
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_execution_feedback_preserves_independent_calls_and_exact_task_on_both_protocols(self):
        self.launch()
        for mode in ("legacy", "2026-07-28"):
            with self.subTest(mode=mode):
                async with Client(self.client(), mode=mode, cache=None) as client:
                    async def call(name, arguments):
                        if name == "read_task":
                            return await self.observer_task_view(client, arguments["task_id"])
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(result.is_error, result.structured_content)
                        return result.structured_content

                    scope = {"machine": "desktop", "workspace_name": "example"}
                    context = (await call("read_workspace_file", scope | {"path": "AGENTS.md"}))["context"]
                    task_id = "feedback-" + mode
                    request = self.task_request(context, task_id)
                    request["contract"]["tools"] = ["control_task", "read_task", "start_execution"]
                    created = (await call("create_task", request))["record"]
                    claimed = (await call("control_task", {
                        "task_id": task_id, "expected_revision": created["revision"], "expected_plan_revision": 1,
                        "expected_epoch": 0, "update_id": uuid.uuid4().hex,
                        "transition": {"action": "claim", "handoff_id": created["handoff"]["id"]}}))["record"]
                    execution_id = uuid.uuid4().hex
                    arguments = {"machine": "desktop", "execution_id": execution_id, "cwd": str(self.work),
                        "argv": [sys.executable, "-c", "print('original output')"],
                        "task_context": {"task_id": task_id, "task_revision": claimed["revision"],
                            "plan_revision": 1, "continuation_epoch": claimed["control"]["epoch"],
                            "operation_id": execution_id}}
                    rejected = await client.call_tool("start_execution", dict(arguments, cwd=str(self.root)))
                    self.assertTrue(rejected.is_error)
                    self.assertEqual(rejected.structured_content["error"]["code"], "task_scope_denied")
                    self.assertEqual(rejected.structured_content["task_id"], task_id)
                    self.assertIn("Keep task_context", rejected.structured_content["resume_prompt"])
                    execution_id = uuid.uuid4().hex
                    arguments["execution_id"] = arguments["task_context"]["operation_id"] = execution_id
                    started = await call("start_execution", arguments)
                    self.assertEqual(started["task_feedback"]["status"], "supervised")
                    original = await self.execution_until(client, {"machine": "desktop", "execution_id": execution_id},
                                                          lambda value: value["status"] == "succeeded")
                    self.assertEqual(original["output"], "original output\n")
                    current = (await call("read_task", {"task_id": task_id}))["record"]
                    await call("control_task", {"task_id": task_id, "expected_revision": current["revision"],
                        "expected_plan_revision": 1, "expected_epoch": current["control"]["epoch"],
                        "update_id": uuid.uuid4().hex, "transition": {"action": "pause", "reason": "Pause only this task."}})
                    paused = await call("read_execution", {"machine": "desktop", "execution_id": execution_id})
                    for key in ("status", "exit_code", "output", "created_at"):
                        self.assertEqual(paused[key], original[key])
                    self.assertEqual(paused["task_feedback"]["foreground"]["reason"], "task_stopped")

                    independent = {key: value for key, value in arguments.items() if key != "task_context"}
                    independent["execution_id"] = uuid.uuid4().hex
                    self.assertEqual((await call("start_execution", independent))["task_feedback"]["status"], "unbound")
                    completed = await self.execution_until(client,
                        {"machine": "desktop", "execution_id": independent["execution_id"]},
                        lambda value: value["status"] == "succeeded")
                    self.assertEqual(completed["task_feedback"]["status"], "unbound")

    async def test_supervised_write_keeps_original_context_on_both_protocols(self):
        self.launch()
        for mode in ("legacy", "2026-07-28"):
            with self.subTest(mode=mode):
                async with Client(self.client(), mode=mode, cache=None) as client:
                    self.assertEqual(client.protocol_version,
                                     "2025-11-25" if mode == "legacy" else "2026-07-28")

                    async def call(name, arguments):
                        if name == "read_task":
                            return await self.observer_task_view(client, arguments["task_id"])
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(result.is_error, result.structured_content)
                        return result.structured_content

                    scope = {"machine": "desktop", "workspace_name": "example"}
                    context = (await call("read_workspace_file", scope | {"path": "AGENTS.md"}))["context"]
                    task_id = "accepted-write-" + mode
                    request = self.task_request(context, task_id)
                    request["contract"]["tools"] = ["control_task", "write_workspace_file",
                                                     "read_workspace_change", "read_task"]
                    created = (await call("create_task", request))["record"]
                    claimed = (await call("control_task", {
                        "task_id": task_id, "expected_revision": created["revision"],
                        "expected_plan_revision": created["plan"]["revision"],
                        "expected_epoch": created["control"]["epoch"], "update_id": uuid.uuid4().hex,
                        "transition": {"action": "claim", "handoff_id": created["handoff"]["id"]}}))["record"]
                    change_id = uuid.uuid4().hex
                    path = "accepted-" + mode + ".txt"
                    content = "One accepted effect on " + mode + "\n"
                    arguments = scope | {
                        "path": path, "content": content, "expected_head_sha": "", "expected_sha256": "",
                        "expected_workspace_identity": context["workspace_identity"], "change_id": change_id,
                        "task_context": {"task_id": task_id, "task_revision": claimed["revision"],
                            "plan_revision": claimed["plan"]["revision"],
                            "continuation_epoch": claimed["control"]["epoch"], "operation_id": change_id}}
                    first = await call("write_workspace_file", arguments)
                    self.assertEqual((first["status"], first["change_id"]), ("applied", change_id))
                    self.assertEqual((self.work / path).read_text(), content)
                    original = await call("read_workspace_change", {"machine": "desktop", "change_id": change_id})
                    # A later independent edit makes an accidental replay visible.
                    later = content + "Subsequent disposable work.\n"
                    (self.work / path).write_text(later)
                    repeated = await call("write_workspace_file", arguments)
                    for key in ("status", "change_id", "created_at", "revision"):
                        self.assertEqual(repeated[key], first[key])
                        self.assertEqual(original[key], first[key])
                    self.assertFalse(repeated["task_operation_replayed"])
                    retained = (await call("read_task", {"task_id": task_id}))["record"]
                    self.assertEqual([operation["id"] for operation in retained["operations"]], [change_id])
                    operation = retained["operations"][0]
                    self.assertEqual(operation["epoch"], claimed["control"]["epoch"])
                    self.assertEqual(operation["plan_revision"], claimed["plan"]["revision"])
                    self.assertEqual((self.work / path).read_text(), later)
        self.assertFalse((self.state / "executions").exists())
