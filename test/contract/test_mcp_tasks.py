"""Behavior checks for MCPContractTests."""

import asyncio
import json
import sys
import time
import unittest
import uuid
from mcp import Client
from openmachine.execution.records import TERMINAL
from openmachine.foundation.state import lease
from support_mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_observer_publishes_original_results_with_all_clients_absent(self):
        for mode in ("legacy", "2026-07-28"):
            for boundary in ("live", "restart-running", "restart-completed", "crash-running", "crash-completed", "activation"):
                identity = "observe-" + mode + "-" + boundary
                execution_id = uuid.uuid4().hex
                counter, release, done = (self.work / (prefix + identity) for prefix in ("count-", "release-", "done-"))
                path = self.state / "tasks" / identity / "task.json"
                try:
                    async with Client(self.client(), mode=mode, cache=None) as client:
                        async def call(name, arguments):
                            result = await client.call_tool(name, arguments)
                            self.assertFalse(result.is_error, result.structured_content)
                            return result.structured_content
                        context = (await call("read_workspace_file", {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}))["context"]
                        request = self.task_request(context, identity)
                        request["contract"]["tools"] = ["control_task", "start_execution", "read_task", "read_execution"]
                        record = (await call("create_task", request))["record"]
                        record = (await call("control_task", dict(task_id=identity, expected_revision=record["revision"],
                            expected_plan_revision=1, expected_epoch=0, update_id=uuid.uuid4().hex,
                            transition={"action": "claim", "handoff_id": record["handoff"]["id"]})))["record"]
                        code = ('from pathlib import Path\nimport time\n'
                            + 'p=Path(' + repr(str(counter)) + '); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
                            + 'while not Path(' + repr(str(release)) + ').exists(): time.sleep(0.02)\n'
                            + 'Path(' + repr(str(done)) + ').touch()\n')
                        started = await call("start_execution", dict(machine="desktop", execution_id=execution_id,
                            cwd=str(self.work), argv=[sys.executable, "-c", code], timeout_seconds=20,
                            task_context=dict(task_id=identity, task_revision=record["revision"], plan_revision=1,
                                continuation_epoch=record["control"]["epoch"], operation_id=execution_id)))
                        async with Client(self.client(), mode=mode, cache=None) as second:
                            self.assertEqual((await self.observer_task_view(second, identity))["task_id"], identity)
                    # No SDK sessions or tool calls remain. Only fixture control
                    # and passive canonical bytes may be used to await publication.
                    if boundary != "live":
                        crashed = boundary.startswith("crash-")
                        if crashed:
                            self.processes[-1].kill()
                            self.processes[-1].wait(timeout=5)
                            self.assertTrue(self.socket.exists())
                        else:
                            self.stop(self.processes[-1])
                        before = path.read_bytes()
                        if boundary.endswith("-completed"):
                            release.touch()
                            deadline = time.monotonic() + 5
                            while not done.exists() and time.monotonic() < deadline:
                                await asyncio.sleep(0.03)
                            self.assertTrue(done.exists())
                            self.assertEqual(path.read_bytes(), before)
                        if boundary == "activation":
                            # The real lifecycle owner holds this lease through
                            # candidate startup/readiness. It must not strand the
                            # new observer or permit publication before release.
                            with lease(self.state / "locks/lifecycle.lock"):
                                self.launch(managed=True)
                                release.touch()
                                deadline = time.monotonic() + 5
                                while not done.exists() and time.monotonic() < deadline:
                                    await asyncio.sleep(0.03)
                                self.assertTrue(done.exists())
                                async with Client(self.client(), mode=mode, cache=None) as probe:
                                    self.assertEqual((await self.observer_task_view(probe, identity))["task_id"], identity)
                                self.assertEqual(path.read_bytes(), before)
                        else:
                            # CI explicitly supplies managed startup; native
                            # launchd auto-restart is separate acceptance.
                            self.launch(managed=crashed)
                    release.touch()
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        record = json.loads(path.read_bytes())
                        if (record["operations"][0]["result"] or {}).get("status") == "succeeded":
                            break
                        await asyncio.sleep(0.03)
                    self.assertEqual(record["operations"][0]["result"]["status"], "succeeded", record)
                    self.assertEqual(record["handoff"]["reason"], "result_ready")
                    self.assertEqual(record["control"]["state"], "waiting_for_ai")
                    self.assertEqual(record["plan"]["stages"][0]["state"], "running")
                    self.assertIsNone(record["terminal"])
                    self.assertEqual([item["id"] for item in record["operations"]], [execution_id])
                    self.assertEqual(counter.read_text(), "1")
                    before = path.read_bytes()
                    async with Client(self.client(), mode=mode, cache=None) as client:
                        view = await self.observer_task_view(client, identity)
                        self.assertTrue(view["support"]["local_result_observation"])
                        self.assertFalse(view["support"]["events_delivery"])
                        self.assertEqual(view["observations"][0]["state"], "unknown")
                        original = (await client.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})).structured_content
                        self.assertEqual(original["created_at"], started["created_at"])
                        self.assertEqual(original["status"], "succeeded")
                        self.assertEqual(path.read_bytes(), before)
                finally:
                    release.touch()
                    deadline = time.monotonic() + 5
                    while not done.exists() and time.monotonic() < deadline:
                        await asyncio.sleep(0.03)

    async def test_multistage_task_pause_restart_and_completion_on_both_protocols(self):
        for mode in ("legacy", "2026-07-28"):
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

    async def test_all_mutation_contexts_reject_unknown_and_forged_fields_before_dispatch(self):
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                response = await self.wire_call(client, "tools/call", {"name": "read_workspace_file", "arguments": {
                    "machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}})
                source = response.json()["result"]["structuredContent"]
                native = dict(machine="desktop", execution_id="a" * 32)
                file = dict(machine="desktop", workspace_name="example", expected_head_sha="", change_id="b" * 32,
                            expected_workspace_identity=source["context"]["workspace_identity"])
                cases = {
                    "start_execution": dict(native, cwd=str(self.work), argv=[sys.executable, "-c", "raise SystemExit(0)"]),
                    "cancel_execution": native,
                    "control_execution": dict(native, expected_control_seq=0, action="write_stdin", text="not sent\n"),
                    "write_workspace_file": dict(file, path="new.txt", expected_sha256="", content="not written"),
                    "edit_workspace_file": dict(file, path="AGENTS.md", expected_sha256=source["sha256"],
                        edits=[{"old_text": "Only", "new_text": "Never"}]),
                    "apply_workspace_patch": dict(file, patch="*** Begin Patch\n*** Add File: new.txt\n+not written\n*** End Patch\n",
                        preconditions=[{"path": "new.txt", "expected_sha256": ""}]),
                    "github_issue": dict(action="create", repository="Example/repo", title="Not published", body="fixture"),
                    "github_pull_request": dict(action="create", repository="Example/repo", title="Not published", body="fixture", head="topic", base="main"),
                    "github_comment": dict(action="create", repository="Example/repo", body="fixture", number=1),
                    "github_review": dict(action="create", repository="Example/repo", body="fixture", number=1, expected_head_sha="c" * 40),
                    "github_merge": dict(repository="Example/repo", number=1, expected_head_sha="c" * 40)}
                forged = dict(task_id="not-created", task_revision=1, plan_revision=1, continuation_epoch=0, operation_id="d" * 32)
                for name, arguments in cases.items():
                    for invalid in (dict(arguments, task_revison=1),
                                    dict(arguments, task_context=dict(forged, principal_ref="forged")),
                                    dict(arguments, task_context=dict(forged, task_revision=True))):
                        response = await self.wire_call(client, "tools/call", {"name": name, "arguments": invalid})
                        result = response.json()["result"]
                        self.assertTrue(result["isError"], (name, result))
                        self.assert_wire_tool_result(result, version)
                        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_tool_call")
                control = dict(task_id="not-created", expected_revision=1, expected_plan_revision=1,
                    expected_epoch=0, update_id="e" * 32, transition={"action": "pause", "reason": "fixture"})
                for invalid in (dict(control, owner="forged"), dict(control, expected_revision=True),
                                dict(control, transition={"action": "pause", "reason": "fixture", "state": "completed"})):
                    response = await self.wire_call(client, "tools/call", {"name": "control_task", "arguments": invalid})
                    result = response.json()["result"]
                    self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_tool_call")
                    self.assert_wire_tool_result(result, version)
                self.assertEqual((self.work / "AGENTS.md").read_text(), "Only modify this disposable project.\n")
                self.assertFalse((self.work / "new.txt").exists())
                self.assertFalse((self.state / "tasks").exists())
                self.assertFalse((self.state / "executions").exists())

    async def test_canonical_tasks_survive_real_server_restart_without_dispatch(self):
        retained = {}
        for mode in ("legacy", "2026-07-28"):
            async with Client(self.client(), mode=mode, cache=None) as client:
                scope = await client.call_tool("read_workspace_file", {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"})
                identity = "canonical-" + mode
                request = self.task_request(scope.structured_content["context"], identity)
                created = await client.call_tool("create_task", request)
                self.assertFalse(created.is_error, created)
                record = created.structured_content["record"]
                retained[identity] = record
                repeated = await client.call_tool("create_task", request)
                self.assertEqual(repeated.structured_content["record"], record)
                self.assertEqual(repeated.structured_content["status"], "unchanged")
                changed = await client.call_tool("create_task", request | {"title": "Conflicting declaration"})
                self.assertEqual(changed.structured_content["error"]["code"], "task_conflict")
                execution_id = uuid.uuid4().hex
                denied = await client.call_tool("start_execution", {"machine": "desktop", "execution_id": execution_id,
                    "cwd": str(self.work), "argv": [sys.executable, "-c", "raise SystemExit(0)"], "task_id": identity})
                self.assertEqual(denied.structured_content["error"]["code"], "supervised_admission_unavailable")
                self.assertFalse((self.state / "executions" / execution_id).exists())
                index = self.state / "tasks/index.json"
                original = index.read_bytes()
                index.write_text("Interrupted derived index fixture")
                read = await client.call_tool("read_task", {"task_id": identity})
                self.assertFalse(read.is_error, read)
                self.assertEqual(read.structured_content["record"], record)
                self.assertEqual(read.structured_content["index_status"], "stale")
                self.assertEqual(index.read_text(), "Interrupted derived index fixture")
                index.write_bytes(original)
                caps = (await client.call_tool("get_capabilities")).structured_content
                self.assertEqual(caps["lifecycle"]["state_contract"], 12)
                self.assertTrue(caps["tasks"]["structured_records"])
                self.assertTrue(caps["tasks"]["supervised_admission"])
                self.assertFalse(caps["tasks"]["events_delivery"])
        snapshots = {identity: (self.state / "tasks" / identity / "task.json").read_bytes() for identity in retained}
        self.stop(self.processes[-1])
        self.launch()
        async with Client(self.client(), mode="2026-07-28", cache=None) as client:
            found = (await client.call_tool("find_tasks", {"anchors": ["fixture=s11"]})).structured_content
            self.assertEqual(found["coverage"], "complete")
            self.assertEqual({item["task_id"] for item in found["tasks"]}, set(retained))
            for identity, record in retained.items():
                read = (await client.call_tool("read_task", {"task_id": identity})).structured_content
                self.assertEqual(read["record"], record)
                self.assertEqual(read["observations"][0]["state"], "unknown")
                self.assertEqual((self.state / "tasks" / identity / "task.json").read_bytes(), snapshots[identity])

    async def test_canonical_contract_rejects_runtime_fields_and_invalid_bindings_on_both_protocols(self):
        import copy
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                response = await self.wire_call(client, "tools/call", {"name": "read_workspace_file", "arguments": {
                    "machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}})
                context = response.json()["result"]["structuredContent"]["context"]
                request = self.task_request(context, "never-admitted")
                changes = [lambda r: r.update(runtime_state="succeeded"),
                    lambda r: r["stages"][0].update(state="accepted"),
                    lambda r: r["contract"].update(expires_at_ms=True),
                    lambda r: r["contract"].update(scope="\u00e9" * 513),
                    lambda r: r["stages"][0].update(source_sha256="f" * 64),
                    lambda r: r["contract"]["targets"][0].update(workspace_identity="f" * 64)]
                for change in changes:
                    invalid = copy.deepcopy(request)
                    change(invalid)
                    response = await self.wire_call(client, "tools/call", {"name": "create_task", "arguments": invalid})
                    result = response.json()["result"]
                    self.assertTrue(result["isError"], result)
                    self.assert_wire_tool_result(result, version)
                    self.assertIn(result["structuredContent"]["error"]["code"],
                                  {"invalid_tool_call", "invalid_task_contract", "context_mismatch"})
                    self.assertFalse((self.state / "tasks").exists())
                    self.assertFalse((self.state / "executions").exists())
