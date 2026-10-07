"""resilience app test_mcp_admission behavior."""

import asyncio
import sys
import time
import unittest
import uuid
from mcp import Client
from openmachine.execution.records import TERMINAL
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_multistage_task_pause_restart_and_completion(self):
        self.launch()
        for mode in ("2026-07-28",):
            identity, execution_id, change_id = "admission-" + mode, uuid.uuid4().hex, uuid.uuid4().hex
            counter = self.work / ("count-" + mode)
            result_path = "result-" + mode + ".txt"
            async with Client(self.client(), mode=mode, cache=None) as client:
                async def call(name, arguments):
                    if name == "read_task":
                        return await self.observer_task_view(client, arguments["task_id"])
                    result = await client.call_tool(name, arguments)
                    self.assertFalse(result.is_error, result.structured_content)
                    return result.structured_content
                context = (await call("read_workspace_file", {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}))["context"]
                request = self.task_request(context, identity)
                request["origin"]["objective"] = "Write the fixture file, verify it once, and retain both results."
                request["contract"].update(objective="Write the fixture file, verify it once, and retain both results.",
                    acceptance="File contains ready, original verification succeeds once, and both stages retain evidence.",
                    tools=["read_task", "control_task", "write_workspace_file", "start_execution", "read_execution"])
                request["stages"] = [dict(request["stages"][0], id="edit", result="Fixture file contains ready.",
                    verification="Read the applied change and current file."),
                    dict(request["stages"][0], depends_on=["edit"], result="Original verification succeeds once.",
                         verification="Read the original terminal receipt and single counter increment.")]
                record = (await call("create_task", request))["record"]
                def control(record, action, **values):
                    return dict(task_id=identity, expected_revision=record["revision"], expected_plan_revision=record["plan"]["revision"],
                        expected_epoch=record["control"]["epoch"], update_id=uuid.uuid4().hex, transition=dict(action=action, **values))
                def task_context(record, operation_id):
                    return dict(task_id=identity, task_revision=record["revision"], plan_revision=record["plan"]["revision"],
                                continuation_epoch=record["control"]["epoch"], operation_id=operation_id)
                claim = control(record, "claim", handoff_id=record["handoff"]["id"])
                claimed = (await call("control_task", claim))["record"]
                repeated = await call("control_task", claim)
                self.assertEqual(repeated["record"], claimed)
                change = dict(machine="desktop", workspace_name="example", path=result_path, content="ready\n",
                    expected_workspace_identity=context["workspace_identity"], expected_head_sha="", expected_sha256="",
                    change_id=change_id, task_context=task_context(claimed, change_id))
                self.assertEqual((await call("write_workspace_file", change))["status"], "applied")
                self.assertEqual((self.work / result_path).read_text(), "ready\n")
                record = (await call("read_task", {"task_id": identity}))["record"]
                decision = control(record, "record_decision", stage_id="edit", outcome="accepted", evidence=[change_id],
                                   reason="Verified original change and current file contents.")
                accepted_edit = (await call("control_task", decision))["record"]
                self.assertEqual((await call("control_task", decision))["record"], accepted_edit)
                handover = (await call("control_task", control(accepted_edit, "release", reason="Hand over before verification.")))["record"]
            self.stop(self.processes[-1])
            self.launch()
            async with Client(self.client(), mode=mode, cache=None) as client:
                record = (await call("read_task", {"task_id": identity}))["record"]
                self.assertEqual(record, handover)
                self.assertEqual(record["control"]["current_stage_id"], "verify")
                self.assertEqual([stage["state"] for stage in record["plan"]["stages"]], ["accepted", "pending"])
                original_change = await call("write_workspace_file", change)
                self.assertEqual(original_change["status"], "applied")
                self.assertFalse(original_change["task_operation_replayed"])
                self.assertEqual((await call("read_task", {"task_id": identity}))["record"], handover)
                claimed = (await call("control_task", control(record, "claim", handoff_id=record["handoff"]["id"])))["record"]
                execution_context = task_context(claimed, execution_id)
                code = ('import time; from pathlib import Path; '
                        f'assert Path({result_path!r}).read_text() == "ready\\n"; p=Path({str(counter)!r}); '
                        'p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); time.sleep(1.5)')
                arguments = dict(machine="desktop", execution_id=execution_id, cwd=str(self.work), argv=[sys.executable, "-c", code],
                                 task_context=execution_context)
                started = await call("start_execution", arguments)
                self.assertNotIn(started["status"], TERMINAL)
                record = (await call("read_task", {"task_id": identity}))["record"]
                paused = (await call("control_task", control(record, "pause", reason="Keep original work while blocking new admission.")))["record"]
                self.assertEqual(paused["control"]["stop_intent"], "pause")
                self.assertIsNone(paused["handoff"])
            self.stop(self.processes[-1])
            self.launch()
            async with Client(self.client(), mode=mode, cache=None) as client:
                record = (await call("read_task", {"task_id": identity}))["record"]
                self.assertEqual(record["control"], paused["control"])
                self.assertEqual(record["plan"], paused["plan"])
                self.assertGreaterEqual(record["revision"], paused["revision"])
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    original = await call("read_execution", {"machine": "desktop", "execution_id": execution_id})
                    if original["status"] in TERMINAL:
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(original["status"], "succeeded")
                current = (await call("read_task", {"task_id": identity}))["record"]
                self.assertEqual(current["control"], paused["control"])
                self.assertIsNone(current["handoff"])
                recovered = await call("start_execution", arguments)
                self.assertEqual(recovered["status"], "succeeded")
                self.assertFalse(recovered["task_operation_replayed"])
                self.assertEqual(counter.read_text(), "1")
                new_id = uuid.uuid4().hex
                stale = await client.call_tool("start_execution", dict(arguments, execution_id=new_id,
                    task_context=dict(execution_context, operation_id=new_id)))
                self.assertEqual(stale.structured_content["error"]["code"], "task_conflict")
                self.assertFalse((self.state / "executions" / new_id).exists())
                record = (await call("read_task", {"task_id": identity}))["record"]
                resumed = (await call("control_task", control(record, "resume", reason="Explicit fixture continuation.")))["record"]
                record = (await call("control_task", control(resumed, "claim", handoff_id=resumed["handoff"]["id"])))["record"]
                accepted = (await call("control_task", control(record, "record_decision", stage_id="verify", outcome="accepted",
                    evidence=[execution_id], reason="Verified the original zero exit and single counter increment.")))["record"]
                self.assertEqual(accepted["plan"]["stages"][0], accepted_edit["plan"]["stages"][0])
                self.assertEqual(accepted["operations"][0], accepted_edit["operations"][0])
                self.assertTrue(all(stage["state"] == "accepted" for stage in accepted["plan"]["stages"]))
                self.assertIsNone(accepted["terminal"])
                final_handover = (await call("control_task", control(accepted, "release", reason="Hand over final acceptance.")))["record"]
            self.stop(self.processes[-1])
            self.launch()
            async with Client(self.client(), mode=mode, cache=None) as client:
                record = (await call("read_task", {"task_id": identity}))["record"]
                self.assertEqual(record, final_handover)
                claimed = (await call("control_task", control(record, "claim", handoff_id=record["handoff"]["id"])))["record"]
                final = (await call("control_task", control(claimed, "finish", outcome="completed",
                    acceptance=[{"stage_id": "edit", "operation_id": change_id}, {"stage_id": "verify", "operation_id": execution_id}],
                    limitations="Scripted SDK contract, not AI-authored planning or automatic AI recovery.")))["record"]
                self.assertEqual(final["terminal"]["outcome"], "completed")
                self.assertIsNone(final["handoff"])
                self.assertIsNone(final["control"]["owner"])
                self.assertEqual([item["id"] for item in final["operations"]], [change_id, execution_id])
                self.assertEqual(counter.read_text(), "1")
                path = self.state / "tasks" / identity / "task.json"
                retained = path.read_bytes()
                found = await call("find_tasks", {"task_id": identity})
                self.assertEqual(found["status"], "matched")
                view = await call("read_task", {"task_id": identity})
                self.assertEqual(view["record"], final)
                runtime = next(item for item in view["observations"] if item["kind"] == "runtime")
                self.assertEqual(runtime["state"], "retired")
                self.assertIsNone(runtime["next_attempt_at_ms"])
                self.assertEqual(path.read_bytes(), retained)
