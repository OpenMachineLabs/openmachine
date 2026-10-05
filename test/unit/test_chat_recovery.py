"""Focused task-owned preparation, receipt and single-route delivery checks."""

import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from integrations import chatgpt, codex
from openmachine.bootstrap import create_application
from openmachine.config import ChatRecoveryConfig
from openmachine.foundation.sanitize import Fault
from openmachine import tasks
from openmachine.foundation.state import lease, save_json
from support_tasks import TaskFixture, task_contract


class ChatRecoveryTests(TaskFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config.chat_recovery = ChatRecoveryConfig(workspace="project", connection="Fixture OM",
            browser_account="Fixture Account", browser_workspace="Personal", model="Fixture Model")
        self.app = create_application(self.config, self.state)
        self.request["contract"].pop("recovery_policy", None)  # Exercise the new default.

    def receiver_fixture(self):
        """Actual registration/reservation with no native browser effects."""
        self.create()
        return self.reserve_receiver("structured-one")

    def reserve_receiver(self, task_id):
        binding = chatgpt.get(self.state, task_id)
        identity = binding.preparation.preparation_id
        chatgpt.reserve_preparation(self.state, task_id, identity, binding.revision, uuid.uuid4().hex)
        return {"task_id": task_id, "preparation_id": identity}

    def preparation_result(self, task_id="structured-one", url=None):
        """Synthetic native-adapter facts; the independent read proof is real."""
        binding = chatgpt.get(self.state, task_id)
        proof = chatgpt.preparation_response(binding, self.app.read_task(task_id)["record"])
        destination = url or "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
        return {"status": "completed", "execution_status": "succeeded", "exit_code": 0, "failure": None,
            "report": {"summary": "Fixture only", "final_url": destination, "tab_id": "fixture-tab",
                "pending_action": "", "evidence": [chatgpt.preparation_message(task_id,
                    binding.preparation.preparation_id, self.config.chat_recovery.connection),
                    proof["receipt"], "Receiver setup turn complete.", "Configured receiver route verified."]},
            "native": {"calls": 1, "origins": [chatgpt.ORIGIN], "snapshot": {
                "url": destination, "tab_id": "fixture-tab", "call_index": 1}}}

    def test_receiver_read_requires_external_correlation_and_preserves_original_receipt(self):
        arguments = self.receiver_fixture()
        path = self.state / "chat_recovery.json"
        task_before, before = self.path.read_bytes(), path.read_bytes()
        plain = self.view()
        self.assertNotIn("response_code", json.dumps(plain))
        payload = self.app.read_task(**arguments)
        self.assertEqual(path.read_bytes(), before)
        response = payload["preparation_read"]
        self.app.chat_recovery.external_activity("read_task", arguments, payload)
        observed = chatgpt.get(self.state, "structured-one").preparation
        self.assertEqual(observed.receiver_read.task_revision, payload["record"]["revision"])
        self.assertEqual(observed.receiver_read.record_sha256, hashlib.sha256(task_before).hexdigest())
        self.assertEqual(observed.status, "reserved")
        self.assertIsNone(observed.ready_at_ms)
        acknowledged = path.read_bytes()
        self.app.chat_recovery.external_activity("read_task", arguments, self.app.read_task(**arguments))
        self.assertEqual(path.read_bytes(), acknowledged)
        self.app = create_application(self.config, self.state)
        self.assertEqual(self.app.read_task(**arguments)["preparation_read"], response)
        self.assertEqual(path.read_bytes(), acknowledged)
        self.assertEqual(self.path.read_bytes(), task_before)

    def test_receiver_rejects_wrong_id_task_response_and_late_disabled_read(self):
        arguments = self.receiver_fixture()
        payload = self.app.read_task(**arguments)
        for identity in ("", "wrong", "a" * 32):
            with self.subTest(identity=identity), self.assertRaises(Fault):
                self.app.read_task("structured-one", identity)
        for change in ({"response_code": "0" * 32}, {"record_sha256": "0" * 64}, {"task_revision": 9}):
            forged = dict(payload, preparation_read=dict(payload["preparation_read"], **change))
            self.fault("chat_recovery_preparation_conflict", self.app.chat_recovery.external_activity,
                       "read_task", arguments, forged)
        self.fault("chat_recovery_preparation_conflict", self.app.chat_recovery.external_activity,
                   "read_task", dict(arguments, task_id="another-task"), payload)
        self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 1, {"action": "disable"})
        self.fault("chat_recovery_preparation_conflict", self.app.chat_recovery.external_activity,
                   "read_task", arguments, payload)
        self.assertIsNone(chatgpt.get(self.state, "structured-one").preparation.receiver_read)

    def test_receiver_late_pause_expiry_and_revision_are_fenced(self):
        arguments = self.receiver_fixture()
        payload = self.app.read_task(**arguments)
        with chatgpt._writing(self.state) as store:
            store.bindings["structured-one"].revision += 1
            chatgpt._save(self.state, store)
        self.fault("chat_recovery_preparation_conflict", self.app.chat_recovery.external_activity,
                   "read_task", arguments, payload)
        current = self.app.read_task(**arguments)
        expires = current["record"]["contract"]["expires_at_ms"]
        with patch.object(chatgpt, "now_ms", return_value=expires):
            self.fault("task_stopped", self.app.chat_recovery.external_activity, "read_task", arguments, current)
        self.control("pause", reason="Stop before the external read is acknowledged.")
        self.fault("task_stopped", self.app.chat_recovery.external_activity, "read_task", arguments, current)
        self.assertIsNone(chatgpt.get(self.state, "structured-one").preparation.receiver_read)

    def test_receiver_failed_persistence_cannot_acknowledge_or_accept_readiness(self):
        arguments = self.receiver_fixture()
        payload = self.app.read_task(**arguments)
        before = (self.state / "chat_recovery.json").read_bytes()
        with patch.object(chatgpt, "save_json", side_effect=OSError("Injected publication failure")):
            self.fault("chat_recovery_store_unavailable", self.app.chat_recovery.external_activity,
                       "read_task", arguments, payload)
        self.assertEqual((self.state / "chat_recovery.json").read_bytes(), before)
        self.assertIsNone(chatgpt.get(self.state, "structured-one").preparation.receiver_read)

    def delivery_result(self, summary=""):
        """Synthetic native-adapter facts, not live browser acceptance."""
        binding = chatgpt.get(self.state, "structured-one")
        return {"status": "blocked" if summary else "completed", "execution_status": "succeeded",
            "exit_code": 0, "failure": None,
            "report": {"summary": summary, "final_url": binding.chat_url, "tab_id": "fixture-tab",
                "pending_action": "", "evidence": ["No send action was taken."] if summary else [
                    "[OM auto-resume: " + binding.delivery.recovery_id + "]",
                    chatgpt.recovery_message(binding.task_id, binding.delivery.recovery_id)]},
            "native": {"calls": 1, "origins": [chatgpt.ORIGIN], "snapshot": {
                "url": binding.chat_url, "tab_id": "fixture-tab", "call_index": 1}}}

    def tick(self):
        self.app.task_observer._tick()
        self.app.chat_recovery._thread.join(5)
        self.assertFalse(self.app.chat_recovery.busy)
        self.assertIsNone(self.app.chat_recovery._failure)

    def enable(self, *, task_id="structured-one", expected_revision=None):
        """Complete a synthetic receiver, or exercise the real explicit override."""
        if expected_revision is not None:
            return self.app.configure_chat_recovery(task_id, uuid.uuid4().hex, expected_revision, {"action": "enable"})
        arguments = self.reserve_receiver(task_id)
        payload = self.app.read_task(**arguments)
        self.app.chat_recovery.external_activity("read_task", arguments, payload)
        with patch.object(codex, "read", return_value=self.preparation_result(task_id)), \
                patch.object(self.app.machines, "read_execution", return_value={
                    "status": "succeeded", "cleanup": {"status": "verified"}}):
            self.app.chat_recovery.read_preparation(task_id)
        return self.app.read_task(task_id)["chat_recovery"]

    def test_registration_crash_preserves_creation_and_restart_discovers_without_execution(self):
        with patch.object(chatgpt, "register_preparation", side_effect=OSError("Injected sidecar failure")):
            created = self.create()
        self.assertEqual(created["status"], "created")
        self.assertIsNone(created["record"]["chat_registration"])
        self.assertEqual(created["chat_recovery"]["blocker"], "chat_recovery_store_unavailable")
        original = self.path.read_bytes()
        self.app = create_application(self.config, self.state)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.view()["record"]["operations"], [])
        self.app.chat_recovery._discover()
        registered = self.view()
        identity = registered["record"]["chat_registration"]
        self.assertIsNotNone(identity)
        self.assertEqual(registered["chat_recovery"]["preparation"]["preparation_id"], identity)
        before = (self.path.read_bytes(), (self.state / "chat_recovery.json").read_bytes())
        self.assertEqual(self.create()["status"], "unchanged")
        self.assertEqual((self.path.read_bytes(), (self.state / "chat_recovery.json").read_bytes()), before)

    def test_registration_ack_gap_retains_original_id_and_budget(self):
        with patch.object(tasks, "update", side_effect=Fault("task_write_unverified", "Injected ack failure")):
            created = self.create()
        self.assertIsNone(created["record"]["chat_registration"])
        original = chatgpt.get(self.state, "structured-one").preparation
        self.app = create_application(self.config, self.state)
        self.app.chat_recovery._discover()
        self.assertEqual(self.view()["record"]["chat_registration"], original.preparation_id)
        self.assertEqual(chatgpt.get(self.state, "structured-one").preparation, original)
        self.assertEqual(self.view()["record"]["operations"], [])

    def test_preparing_entries_count_toward_capacity_without_losing_committed_tasks(self):
        with patch.object(chatgpt, "MAX_ACTIVE", 2):
            self.create()
            for identity in ("capacity-two", "capacity-three"):
                request = task_contract(self.context, identity)
                request["contract"].pop("recovery_policy")
                result = self.app.create_task(**request)
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["chat_recovery"]["blocker"], "chat_recovery_capacity")
            self.assertEqual(len(chatgpt.bindings(self.state)), 2)
            self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 1, {"action": "disable"})
            self.app.chat_recovery._discover()
            self.assertIsNotNone(self.app.read_task("capacity-three")["record"]["chat_registration"])
            self.assertEqual(len(chatgpt.bindings(self.state)), 3)

    def test_new_effects_wait_for_ready_but_original_receipts_remain_readable(self):
        self.accept_plan()
        arguments = self.start_arguments("from pathlib import Path; Path('counter').write_text('1')")
        with patch.object(self.app.machines, "start_execution") as dispatch:
            self.fault("chat_recovery_not_ready", self.app.start_execution, **arguments)
            dispatch.assert_not_called()
        self.fault("chat_recovery_not_ready", self.app.write_workspace_file, **self.write_arguments())
        self.assertEqual(self.view()["record"]["operations"], [])
        self.assertFalse((self.work / "result.txt").exists())
        self.enable()
        self.wait(self.app.start_execution(**arguments))
        self.control("pause", reason="Retain the original result while recovery is unavailable.")
        self.config.chat_recovery.model = "Changed Fixture Model"
        with patch.object(self.app.machines, "start_execution", side_effect=AssertionError("Never replay")):
            original = self.app.start_execution(**arguments)
        self.assertEqual(original["status"], "succeeded")
        self.assertFalse(original["task_operation_replayed"])
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_unconfigured_default_blocks_until_explicit_disable(self):
        self.config.chat_recovery = None
        created = self.accept_plan()
        self.assertEqual(created["chat_recovery"]["state"], "blocked")
        self.assertEqual(created["chat_recovery"]["blocker"], "chat_recovery_controller_unconfigured")
        arguments = self.start_arguments()
        with patch.object(self.app.machines, "start_execution") as dispatch:
            self.fault("chat_recovery_preparing", self.app.start_execution, **arguments)
            dispatch.assert_not_called()
        disabled = self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 0, {"action": "disable"})
        self.assertEqual(disabled["state"], "disabled")
        self.assertEqual(self.wait(self.app.start_execution(**arguments))["status"], "succeeded")

    def test_lost_acknowledged_entry_cannot_restore_the_creation_disabled_policy(self):
        self.request["contract"]["recovery_policy"] = "disabled"
        self.create()
        self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 0, {"action": "enable"})
        record = self.view()["record"]
        self.assertIsNotNone(record["chat_registration"])
        with chatgpt._writing(self.state) as store:
            store.bindings.pop("structured-one")  # Simulated loss, not a product repair API.
            chatgpt._save(self.state, store)
        original = self.path.read_bytes()
        self.app = create_application(self.config, self.state)
        self.app.chat_recovery._discover()
        self.fault("chat_recovery_registration_missing", self.app.chat_recovery.require_ready, record)
        self.assertEqual(self.view()["chat_recovery"]["state"], "blocked")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(chatgpt.bindings(self.state), [])

    def test_setup_starts_during_live_claim_without_abandonment_grace_or_second_launch(self):
        self.accept_plan()
        before = self.path.read_bytes()
        recovery = self.app.chat_recovery
        recovery.start()
        self.addCleanup(recovery.stop)
        with patch.object(codex, "start", return_value={"status": "accepted"}) as launch:
            recovery._scan()
            self.assertIsNone(recovery._failure)
            self.assertEqual(launch.call_count, 1)
            binding = chatgpt.get(self.state, "structured-one")
            self.assertEqual(binding.preparation.status, "reserved")
            self.assertIn(chatgpt.preparation_message(binding.task_id, binding.preparation.preparation_id,
                self.config.chat_recovery.connection), launch.call_args.args[6])
            self.assertNotIn(binding.preparation.response_code, launch.call_args.args[6])
            self.assertEqual(launch.call_args.args[11], chatgpt.BROWSER_SECONDS)
            with patch.object(codex, "read", return_value={"status": "running"}):
                recovery._scan()
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_native_setup_and_read_witness_are_both_required_before_verified_cleanup(self):
        arguments = self.receiver_fixture()
        route = self.config.chat_recovery.model_dump()
        report = self.preparation_result()
        binding = chatgpt.get(self.state, "structured-one")
        self.assertNotEqual(chatgpt.preparation_outcome(report, binding, route)[0], "ready")
        self.app.chat_recovery.external_activity("read_task", arguments, self.app.read_task(**arguments))
        binding = chatgpt.get(self.state, "structured-one")
        self.assertEqual(chatgpt.preparation_outcome(report, binding, route)[0], "ready")
        for index in range(len(report["report"]["evidence"])):
            incomplete = json.loads(json.dumps(report))
            incomplete["report"]["evidence"].pop(index)
            with self.subTest(missing=index):
                self.assertNotEqual(chatgpt.preparation_outcome(incomplete, binding, route)[0], "ready")
        for change in ({"pending_action": "Confirm tool access"}, {"tab_id": "another-tab"},
                       {"evidence": ["READY"]}, {"final_url": "https://chatgpt.com/share/fixture"}):
            incomplete = json.loads(json.dumps(report))
            incomplete["report"].update(change)
            self.assertNotEqual(chatgpt.preparation_outcome(incomplete, binding, route)[0], "ready")
        with patch.object(codex, "read", return_value=report), patch.object(self.app.machines, "read_execution",
                return_value={"status": "succeeded", "cleanup": {"status": "pending"}}):
            result = self.app.chat_recovery.read_preparation("structured-one")
        self.assertEqual(result["chat_recovery"]["preparation"]["reason"], "browser_cleanup_unverified")
        self.fault("chat_recovery_not_ready", self.app.chat_recovery.require_ready, self.view()["record"])

    def test_native_evidence_guards_apply_to_both_recovery_purposes(self):
        self.create()
        self.enable()
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        binding = chatgpt.get(self.state, "structured-one")
        cases = [
            (("execution_status",), "failed"), (("exit_code",), 1),
            (("failure",), "fixture_failure"),
            (("report", "pending_action"), "Confirm access"),
            (("report", "tab_id"), "another-tab"),
            (("report", "final_url"), "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            (("native", "calls"), 0), (("native", "origins"), []),
            (("native", "snapshot"), None),
            (("native", "snapshot", "call_index"), False),
            (("native", "snapshot", "call_index"), 2),
            (("native", "snapshot", "tab_id"), "another-tab"),
        ]
        for outcome, arguments, sample, accepted in (
                (chatgpt.preparation_outcome, (binding, self.config.chat_recovery.model_dump()),
                 self.preparation_result(), "ready"),
                (chatgpt.delivery_outcome, (binding,), self.delivery_result(), "sent_reported")):
            self.assertEqual(outcome(sample, *arguments)[0], accepted)
            for path, value in cases:
                with self.subTest(purpose=accepted, field=path, value=value):
                    changed = json.loads(json.dumps(sample))
                    owner = changed
                    for key in path[:-1]:
                        owner = owner[key]
                    owner[path[-1]] = value
                    self.assertEqual(outcome(changed, *arguments)[0], "outcome_unknown")

    def test_delivery_rechecks_controller_after_single_snapshot_validation(self):
        self.create()
        self.enable()
        recovery = self.app.chat_recovery
        record = self.view()["record"]
        with patch.object(chatgpt, "get", wraps=chatgpt.get) as read, \
                patch.object(recovery, "_controller", wraps=recovery._controller) as controller:
            recovery._binding(record)
            read.assert_called_once_with(self.state, "structured-one")
            controller.assert_called_once()

        reserve = chatgpt.reserve_delivery
        def reserve_then_revoke(*args, **kwargs):
            result = reserve(*args, **kwargs)
            self.config.machines["desktop"].execution_enabled = False
            return result
        with patch.object(chatgpt, "reserve_delivery", side_effect=reserve_then_revoke), \
                patch.object(codex, "start") as launch:
            self.fault("execution_disabled", self.app._start_chat_recovery_delivery,
                       "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
            launch.assert_not_called()
        delivery = chatgpt.get(self.state, "structured-one").delivery
        self.assertEqual((delivery.status, delivery.attempts, delivery.executions), ("deferred", 0, []))

    def test_late_setup_result_retains_destination_without_undoing_pause(self):
        arguments = self.receiver_fixture()
        self.app.chat_recovery.external_activity("read_task", arguments, self.app.read_task(**arguments))
        report = self.preparation_result()
        self.control("pause", reason="Stop before setup completes.")
        before = self.path.read_bytes()
        with patch.object(codex, "read", return_value=report), patch.object(self.app.machines, "read_execution",
                return_value={"status": "succeeded", "cleanup": {"status": "verified"}}):
            result = self.app.chat_recovery.read_preparation("structured-one")
        self.assertEqual(result["chat_recovery"]["preparation"]["status"], "blocked")
        self.assertEqual(result["chat_recovery"]["chat_url"], report["report"]["final_url"])
        self.assertIsNone(result["chat_recovery"]["preparation"]["ready_at_ms"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_setup_uncertainty_retains_original_execution_and_never_creates_another_chat(self):
        arguments = self.receiver_fixture()
        original = chatgpt.get(self.state, "structured-one").preparation
        with patch.object(codex, "read", return_value={"status": "timed_out"}):
            self.app.chat_recovery.read_preparation("structured-one")
        self.app = create_application(self.config, self.state)
        self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 1, {"action": "enable"})
        recovery = self.app.chat_recovery
        recovery.start()
        self.addCleanup(recovery.stop)
        with patch.object(codex, "start", side_effect=AssertionError("Never create a replacement Chat")):
            recovery._scan()
        retained = chatgpt.get(self.state, "structured-one").preparation
        self.assertEqual((retained.status, retained.execution_id, retained.deadline_ms, retained.attempts),
                         ("outcome_unknown", original.execution_id, original.deadline_ms, original.attempts))
        self.assertEqual(retained.preparation_id, arguments["preparation_id"])

    def test_exact_enable_retry_after_pause_is_observation_not_new_authority(self):
        self.create()
        self.enable()
        arguments = ("structured-one", uuid.uuid4().hex, 1, {"action": "enable"})
        self.app.configure_chat_recovery(*arguments)
        self.control("pause", reason="Original enable receipt remains readable.")
        original = (self.path.read_bytes(), (self.state / "chat_recovery.json").read_bytes())
        self.assertEqual(self.app.configure_chat_recovery(*arguments)["binding_revision"], 2)
        self.assertEqual((self.path.read_bytes(), (self.state / "chat_recovery.json").read_bytes()), original)

    def test_known_delivery_blocker_invalidates_ready_for_new_business_effects(self):
        self.accept_plan(claim=False)
        self.enable()
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        with patch.object(codex, "read", return_value=self.delivery_result("OM_RECOVERY_NO_SEND:blocked")):
            self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(self.view()["chat_recovery"]["state"], "blocked")
        self.control("claim", handoff_id=self.view()["record"]["handoff"]["id"])
        with patch.object(self.app.machines, "start_execution") as launch:
            self.fault("chat_recovery_route_blocked", self.app.start_execution, **self.start_arguments())
            launch.assert_not_called()

    def test_preparation_no_send_retries_keep_one_budget_across_restart(self):
        self.create()
        deadline, executions = None, []
        with patch("time.time_ns", return_value=time.time_ns()) as clock, \
                patch.object(codex, "start", return_value={"status": "accepted"}) as launch:
            for attempt in range(1, 4):
                recovery = self.app.chat_recovery
                recovery.start()
                identity = uuid.uuid4().hex
                recovery.start_preparation("structured-one", identity)
                preparation = chatgpt.get(self.state, "structured-one").preparation
                deadline = deadline or preparation.deadline_ms
                executions.append(identity)
                self.assertEqual((preparation.deadline_ms, preparation.attempts), (deadline, attempt))
                no_send = self.preparation_result()
                no_send["status"] = "blocked"
                no_send["report"].update(summary="OM_PREPARATION_NO_SEND:generating",
                                         evidence=["No send action was taken."])
                with patch.object(codex, "read", return_value=no_send):
                    recovery.read_preparation("structured-one")
                recovery.start_preparation("structured-one", uuid.uuid4().hex)
                self.assertEqual(launch.call_count, attempt)
                recovery.stop()
                self.app = create_application(self.config, self.state)
                clock.return_value += chatgpt.GRACE_MS * 1000000
            self.app.chat_recovery.start_preparation("structured-one", uuid.uuid4().hex)
            preparation = chatgpt.get(self.state, "structured-one").preparation
            self.assertEqual((preparation.status, preparation.executions, preparation.deadline_ms),
                             ("exhausted", executions, deadline))
            self.assertEqual(launch.call_count, 3)

    def test_historical_reads_do_not_enroll_but_explicit_plan_revision_can(self):
        from openmachine.tasks import records
        self.request["contract"]["recovery_policy"] = "disabled"
        historical = self.create()["record"]
        historical["format"] = 1
        historical.pop("chat_registration")
        historical["contract"].pop("recovery_policy")
        self.path.write_bytes(records.canonical(historical))  # Original-format reader fixture.
        self.app.rebuild_task_index()
        original = self.path.read_bytes()
        self.app.chat_recovery._discover()
        self.assertEqual(self.view()["record"], historical)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(chatgpt.bindings(self.state), [])
        self.control("claim", handoff_id=historical["handoff"]["id"])
        contract = dict(historical["contract"], recovery_policy="task_chat")
        enrolled = self.control("revise_plan", contract=contract, stages=self.request["stages"],
            invalidated_stage_ids=["verify"], reason="Explicitly enroll the original historical task.")
        self.assertEqual((enrolled["record"]["task_id"], enrolled["record"]["format"],
                          enrolled["record"]["plan"]["revision"]), ("structured-one", 3, 2))
        self.assertIsNotNone(enrolled["record"]["chat_registration"])
        self.assertEqual(enrolled["chat_recovery"]["state"], "preparing")

    def test_ready_projection_keeps_continuation_deadline_without_resending(self):
        self.create()
        self.enable()
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        with patch.object(codex, "read", return_value=self.delivery_result()):
            self.app._read_chat_recovery_delivery("structured-one")
        delivery = chatgpt.get(self.state, "structured-one").delivery
        original = (self.path.read_bytes(), (self.state / "chat_recovery.json").read_bytes())
        with patch.object(chatgpt, "now_ms", return_value=delivery.sent_at_ms + chatgpt.CONTINUATION_MS):
            recovery = self.view()["chat_recovery"]
        self.assertEqual((recovery["state"], recovery["blocker"]), ("ready", "continuation_not_observed"))
        self.assertEqual((self.path.read_bytes(), (self.state / "chat_recovery.json").read_bytes()), original)

    def test_ready_projection_reports_current_controller_grant_without_mutation(self):
        self.create()
        self.enable()
        original = (self.path.read_bytes(), (self.state / "chat_recovery.json").read_bytes())
        self.config.machines["desktop"].execution_enabled = False
        recovery = self.view()["chat_recovery"]
        self.assertEqual((recovery["state"], recovery["blocker"]), ("blocked", "execution_disabled"))
        self.assertEqual((self.path.read_bytes(), (self.state / "chat_recovery.json").read_bytes()), original)

    def test_default_policy_binding_disable_and_restart(self):
        self.create()
        before = self.app.read_task("structured-one")
        recovery = before["chat_recovery"]
        self.assertEqual(recovery["state"], "preparing")
        self.assertEqual(before["record"]["format"], 3)
        self.assertEqual(before["record"]["chat_registration"], recovery["preparation"]["preparation_id"])
        self.assertTrue(recovery["default_enabled"])
        self.assertFalse(recovery["bound"])
        self.assertTrue((self.state / "chat_recovery.json").exists())

        enabled = self.enable()
        self.assertEqual((enabled["state"], enabled["binding_revision"]), ("ready", 1))
        self.assertTrue(self.app.read_task("structured-one")["chat_recovery"]["bound"])

        # A new update ID consumes a revision, but does not renew authority.
        same = self.enable(expected_revision=1)
        self.assertEqual(same["binding_revision"], 2)
        self.assertEqual(same["expires_at_ms"], enabled["expires_at_ms"])
        original = (self.state / "chat_recovery.json").read_bytes()

        self.app = create_application(self.config, self.state)
        restarted = self.app.read_task("structured-one")["chat_recovery"]
        self.assertEqual((restarted["state"], restarted["binding_revision"]), ("ready", 2))
        self.assertEqual((self.state / "chat_recovery.json").read_bytes(), original)

        disabled = self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 2,
            {"action": "disable"})
        self.assertEqual((disabled["state"], disabled["binding_revision"]), ("disabled", 3))
        self.assertEqual(self.app.read_task("structured-one")["chat_recovery"]["state"], "disabled")

    def test_binding_rejects_ambiguous_routes_authority_and_shared_conversation(self):
        self.create()
        for url in ("https://chatgpt.com/", "https://chatgpt.com/share/abc",
                    "https://example.com/c/11111111-2222-3333-4444-555555555555",
                    "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                chatgpt.conversation(url)
        for field in ("chat_url", "authorization_ref", "machine", "expires_at_ms"):
            self.fault("invalid_chat_recovery_change", self.app.configure_chat_recovery,
                "structured-one", uuid.uuid4().hex, 1, {"action": "enable", field: "not-accepted"})
        self.enable()
        other = task_contract(self.context, "structured-two")
        other["contract"].pop("recovery_policy")
        self.app.create_task(**other)
        result = self.enable(task_id="structured-two")
        self.assertEqual((result["state"], result["blocker"]), ("blocked", "conversation_conflict"))
        self.assertIsNone(chatgpt.get(self.state, "structured-two").chat_url)

    def test_private_delivery_keeps_business_handoff_and_uses_fixed_message(self):
        created = self.create()
        handoff = created["record"]["handoff"]["id"]
        self.enable()
        recovery_id, execution_id = uuid.uuid4().hex, uuid.uuid4().hex
        calls = []

        def start(config, state, launch, machine, identity, cwd, goal, start_url,
                  allowed_origins, mode, browser, timeout, expected_head, task_context):
            calls.append(dict(machine=machine, identity=identity, cwd=cwd, goal=goal,
                              start_url=start_url, allowed_origins=allowed_origins,
                              mode=mode, browser=browser, timeout=timeout,
                              task_context=task_context, launch=launch))
            return {"status": "accepted", "execution_id": identity}

        with patch.object(codex, "start", side_effect=start):
            started = self.app._start_chat_recovery_delivery(
                "structured-one", recovery_id, execution_id)
        self.assertEqual(started["delivery_execution_id"], execution_id)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["browser"], "chrome")
        self.assertEqual(call["allowed_origins"], ["https://chatgpt.com"])
        self.assertIsNone(call["task_context"])
        self.assertIn("[OM auto-resume: " + recovery_id + "]", call["goal"])
        self.assertIn(chatgpt.claim_update_id(recovery_id), call["goal"])
        self.assertLess(len(chatgpt.recovery_message("structured-one", recovery_id).encode()), 2048)
        self.assertNotIn("\n", chatgpt.recovery_message("structured-one", recovery_id))

        current = self.app.read_task("structured-one")["record"]
        self.assertEqual(current["handoff"]["id"], handoff)
        self.assertIsNone(current["control"]["owner"])

        completed = self.delivery_result()
        marker_only = json.loads(json.dumps(completed))
        marker_only["report"]["evidence"] = marker_only["report"]["evidence"][:1]
        self.assertEqual(chatgpt.delivery_outcome(marker_only, chatgpt.get(self.state, "structured-one"))[0],
                         "outcome_unknown")
        with patch.object(codex, "read", return_value=completed):
            read = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(read["chat_recovery"]["delivery"]["status"], "sent_reported")

        # The same semantic handoff never launches another browser delivery.
        with patch.object(codex, "start", side_effect=AssertionError("must not relaunch")):
            reused = self.app._start_chat_recovery_delivery(
                "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        self.assertEqual(reused["chat_recovery"]["delivery"]["status"], "sent_reported")

    def test_delivery_uncertainty_is_retained_without_browser_replay(self):
        self.create()
        self.enable()
        recovery_id, execution_id = uuid.uuid4().hex, uuid.uuid4().hex
        with patch.object(codex, "start", return_value={"status": "accepted",
                                                        "execution_id": execution_id}):
            self.app._start_chat_recovery_delivery("structured-one", recovery_id, execution_id)
        with patch.object(codex, "read", return_value={"status": "timed_out",
                                                       "execution_status": "timed_out"}):
            read = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(read["chat_recovery"]["delivery"]["status"], "outcome_unknown")
        with patch.object(codex, "read", side_effect=AssertionError("terminal fact must be retained")):
            retained = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(retained["chat_recovery"]["delivery"]["status"], "outcome_unknown")

        proof = chatgpt.check_state(self.state)
        self.assertTrue(proof["chat_recovery_present"])
        self.assertNotIn("chatgpt.com/c/", json.dumps(proof))
        from openmachine.service.checks import activation_safe
        self.fault("activation_deferred", activation_safe, self.state)
        original_delivery = chatgpt.get(self.state, "structured-one").delivery
        self.fault("invalid_chat_recovery_change", self.app.configure_chat_recovery,
            "structured-one", uuid.uuid4().hex, 1, {"action": "enable",
                "chat_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})
        self.enable(expected_revision=1)
        self.app = create_application(self.config, self.state)
        self.assertEqual(chatgpt.get(self.state, "structured-one").delivery, original_delivery)
        with patch.object(codex, "start", side_effect=AssertionError("must not resend after rebind")):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)

    def test_dispatch_is_bounded_by_expiry_and_requires_binding_before_handoff(self):
        self.create()
        with patch.object(self.app.chat_recovery, "_ensure_handoff") as handoff:
            with self.assertRaises(Fault):
                self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
            handoff.assert_not_called()
        self.enable()
        binding = chatgpt.get(self.state, "structured-one")
        execution_id = uuid.uuid4().hex
        with patch.object(chatgpt, "now_ms", return_value=binding.expires_at_ms - 2500), patch.object(
                codex, "start", return_value={"status": "accepted", "execution_id": execution_id}) as start:
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, execution_id)
        self.assertEqual(start.call_args.args[11], 2)

    def test_missing_established_lock_is_not_recreated_by_configuration(self):
        self.create()
        self.enable()
        path = self.state / "chat_recovery.json"
        before = path.read_bytes()
        lock = self.state / "locks/chat-recovery.lock"
        lock.unlink()
        with self.assertRaises(Fault) as error:
            self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 1, {"action": "disable"})
        self.assertEqual(error.exception.code, "chat_recovery_store_invalid")
        self.assertFalse(lock.exists())
        self.assertEqual(path.read_bytes(), before)

    def test_nonterminal_delivery_states_remain_observable(self):
        self.create()
        self.enable()
        identity = uuid.uuid4().hex
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, identity)
        for status in ("accepted", "preparing", "running", "stopping", "finishing"):
            with self.subTest(status=status), patch.object(codex, "read", return_value={"status": status}):
                result = self.app._read_chat_recovery_delivery("structured-one")
                self.assertEqual(result["execution_status"], status)
                self.assertEqual(result["chat_recovery"]["delivery"]["status"], "reserved")
        with patch.object(codex, "read", return_value=self.delivery_result()):
            result = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(result["chat_recovery"]["delivery"]["status"], "sent_reported")

    def test_prelaunch_rejection_releases_only_proven_unsent_reservation(self):
        view = self.create()
        self.enable()
        with patch("time.time_ns", return_value=time.time_ns()) as clock:
            for code in ("task_stopped", "chat_recovery_conflict", "task_authority_expired"):
                with self.subTest(code=code), patch.object(self.app.chat_recovery, "_task",
                        side_effect=[(view, False), Fault(code, "Rejected before launch.")]), \
                        patch.object(codex, "start") as start:
                    self.fault(code, self.app._start_chat_recovery_delivery,
                               "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
                    start.assert_not_called()
                    delivery = chatgpt.get(self.state, "structured-one").delivery
                    self.assertEqual((delivery.status, delivery.attempts, delivery.executions), ("deferred", 0, []))
                clock.return_value += chatgpt.GRACE_MS * 1000000
            deadline = delivery.deadline_ms
            resource = hashlib.sha256(codex.RESOURCE.encode()).hexdigest()
            with lease(self.state / "locks" / f"resource-{resource}.lock"):
                self.fault("resource_busy", self.app._start_chat_recovery_delivery,
                           "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
            self.assertEqual(chatgpt.get(self.state, "structured-one").delivery.attempts, 0)
            self.assertEqual(chatgpt.get(self.state, "structured-one").delivery.deadline_ms, deadline)
            clock.return_value += chatgpt.GRACE_MS * 1000000
            identity = uuid.uuid4().hex
            with patch.object(self.app.machines, "start_execution",
                              side_effect=Fault("execution_start_unverified", "Original launch is uncertain.")):
                self.fault("execution_start_unverified", self.app._start_chat_recovery_delivery,
                           "structured-one", uuid.uuid4().hex, identity)
        retained = chatgpt.get(self.state, "structured-one").delivery
        self.assertEqual((retained.execution_id, retained.status), (identity, "reserved"))

    def test_reserved_delivery_keeps_its_original_controller_until_observed(self):
        self.create()
        self.enable()
        identity = uuid.uuid4().hex
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, identity)
        binding = chatgpt.get(self.state, "structured-one")
        self.fault("chat_recovery_delivery_busy", self.app.configure_chat_recovery,
            "structured-one", uuid.uuid4().hex, 1, {"action": "enable"})
        self.assertEqual(chatgpt.get(self.state, "structured-one"), binding)
        # Disable must still work without changing the original receipt route.
        self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 1, {"action": "disable"})
        self.app = create_application(self.config, self.state)
        with patch.object(codex, "read", return_value=self.delivery_result()) as read:
            self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(read.call_args.args[-2:], ("desktop", identity))

    def test_unchanged_policy_consumes_ids_but_exact_retries_preserve_bytes(self):
        self.create()
        self.enable()
        enabled = {"action": "enable"}
        disabled = {"action": "disable"}
        for change, conflicting in ((enabled, disabled), (disabled, enabled)):
            revision = chatgpt.get(self.state, "structured-one").revision
            self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, revision, change)
            identity = uuid.uuid4().hex
            arguments = ("structured-one", identity, revision + 1, change)
            result = self.app.configure_chat_recovery(*arguments)
            self.assertEqual(result["binding_revision"], revision + 2)
            before = (self.state / "chat_recovery.json").read_bytes()
            self.assertEqual(self.app.configure_chat_recovery(*arguments), result)
            self.fault("chat_recovery_change_conflict", self.app.configure_chat_recovery,
                       "structured-one", identity, revision + 1, conflicting)
            self.assertEqual((self.state / "chat_recovery.json").read_bytes(), before)

    def test_automatic_no_execution_and_finish_obligations_require_real_progress(self):
        with patch("time.time_ns", return_value=time.time_ns()) as clock, \
                patch.object(codex, "start", return_value={"status": "accepted"}) as start:
            self.create()
            self.enable()
            tasks.update(self.state, "structured-one", lambda record, now: record.update(handoff=None))
            recovery = self.app.chat_recovery
            recovery.start()
            self.addCleanup(recovery.stop)
            clock.return_value += chatgpt.GRACE_MS * 1000000
            self.tick()
            first = chatgpt.get(self.state, "structured-one").delivery
            self.assertEqual(start.call_count, 1)
            self.assertEqual(self.view()["record"]["operations"], [])
            with patch.object(codex, "read", return_value=self.delivery_result()):
                recovery.read_delivery("structured-one")
            for action in ("claim", "record_decision"):
                clock.return_value += 1000000
                arguments = self.control_arguments(action, **({"handoff_id": self.view()["record"]["handoff"]["id"]}
                    if action == "claim" else {"stage_id": "verify", "outcome": "accepted", "evidence": [], "reason": "Fixture decision."}))
                if action == "claim":
                    arguments["update_id"] = first.claim_update_id
                result = self.app.control_task(**arguments)
                recovery.external_activity("control_task", arguments, result)
                self.assertEqual(self.view()["chat_recovery"]["delivery_state"],
                                 "claim_observed" if action == "claim" else "continuation_observed")
            # The accepted final stage still needs finish. A read or claim alone
            # cannot create another episode; the actual accepted decision can.
            clock.return_value += chatgpt.GRACE_MS * 1000000
            self.tick()
            second = chatgpt.get(self.state, "structured-one").delivery
            self.assertEqual(start.call_count, 2)
            self.assertNotEqual(first.recovery_id, second.recovery_id)
            self.assertEqual(self.view()["record"]["plan"]["stages"][0]["state"], "accepted")
            recovery.stop()
            recovery.tick()
            self.assertEqual(start.call_count, 2)

    def test_activity_and_restart_grace_do_not_rewrite_task_or_delivery_budget(self):
        self.create()
        self.enable()
        recovery = self.app.chat_recovery
        with patch("time.time_ns", return_value=time.time_ns() + 10000000) as clock:
            recovery.start()
            original = self.path.read_bytes()
            binding = chatgpt.get(self.state, "structured-one")
            recovery.external_activity("get_capabilities", {"task_id": "structured-one"}, self.app.get_capabilities())
            self.app.read_task("structured-one")
            self.assertEqual(chatgpt.get(self.state, "structured-one"), binding)
            recovery.external_activity("read_task", {}, self.view())
            self.assertGreater(chatgpt.get(self.state, "structured-one").activity_at_ms, binding.activity_at_ms)
            self.assertEqual(self.path.read_bytes(), original)
            clock.return_value += chatgpt.GRACE_MS * 1000000 - 1000000
            self.fault("chat_recovery_caller_active", recovery._quiet, chatgpt.get(self.state, "structured-one"))
            clock.return_value += 1000000
            recovery._quiet(chatgpt.get(self.state, "structured-one"))
            recovery.stop()
            recovery.start()
            self.fault("chat_recovery_caller_active", recovery._quiet, chatgpt.get(self.state, "structured-one"))
            recovery.stop()

    def test_retry_budget_and_original_ids_survive_restart_and_handoff_changes(self):
        self.create()
        self.enable()
        with patch("time.time_ns", return_value=time.time_ns()) as clock, \
                patch.object(codex, "start", return_value={"status": "accepted"}) as start:
            episode, deadline, identities = None, None, []
            for attempt in range(1, 4):
                identity = uuid.uuid4().hex
                self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, identity)
                current = chatgpt.get(self.state, "structured-one").delivery
                episode, deadline = episode or current.recovery_id, deadline or current.deadline_ms
                identities.append(identity)
                self.assertEqual((current.recovery_id, current.deadline_ms, current.attempts), (episode, deadline, attempt))
                with patch.object(codex, "read", return_value=self.delivery_result("OM_RECOVERY_NO_SEND:generating")):
                    self.app._read_chat_recovery_delivery("structured-one")
                self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
                self.assertEqual(start.call_count, attempt)
                tasks.update(self.state, "structured-one", lambda record, now: record.update(handoff=None))
                self.app = create_application(self.config, self.state)
                clock.return_value += chatgpt.GRACE_MS * 1000000
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
            current = chatgpt.get(self.state, "structured-one").delivery
            self.assertEqual((current.status, current.executions, current.deadline_ms), ("exhausted", identities, deadline))
            self.assertEqual(start.call_count, 3)

    def test_running_original_and_unknown_receipt_are_distinct_without_replay(self):
        self.accept_plan()
        self.enable()
        arguments = self.start_arguments("from pathlib import Path; Path('counter').write_text('1')")
        self.wait(self.app.start_execution(**arguments))
        record = self.view()["record"]
        with patch("time.time_ns", return_value=(record["control"]["owner"]["expires_at_ms"] + 1) * 1000000):
            before = self.path.read_bytes()
            with patch.object(self.app.task_admission, "_read", return_value={"status": "running"}):
                self.fault("chat_recovery_work_running", self.app.chat_recovery._task, "structured-one")
            with patch.object(self.app.task_admission, "_read", side_effect=Fault("execution_not_found", "Unknown original receipt.")):
                self.assertTrue(self.app.chat_recovery._task("structured-one")[1])
            with patch.object(self.app.task_admission, "_observation", return_value=(
                    {"status": "partial"}, record["operations"][0]["locator"])):
                self.assertTrue(self.app.chat_recovery._task("structured-one")[1])
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(self.app.chat_recovery._task("structured-one")[1])
        self.control("pause", reason="Explicit human wait.")
        self.fault("task_stopped", self.app.chat_recovery._task, "structured-one")
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_legacy_state_and_no_send_evidence_fail_closed(self):
        self.create()
        self.enable()
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        binding = chatgpt.get(self.state, "structured-one")
        result = self.delivery_result("OM_RECOVERY_NO_SEND:generating")
        self.assertEqual(chatgpt.delivery_outcome(result, binding)[0], "deferred")
        result["native"]["snapshot"] = None
        self.assertEqual(chatgpt.delivery_outcome(result, binding)[0], "outcome_unknown")
        path = self.state / "chat_recovery.json"
        legacy = json.loads(path.read_bytes())
        legacy["format"] = 1
        item = legacy["bindings"]["structured-one"]
        item.pop("activity_at_ms")
        item.pop("preparation")  # This fixture represents an original format-1 binding.
        for key in chatgpt._DELIVERY_FIELDS:
            item["delivery"].pop(key)
        save_json(path, legacy)
        before = path.read_bytes()
        proof = chatgpt.check_state(self.state)
        self.assertEqual(proof["chat_recovery_format"], 1)
        self.assertEqual(proof["chat_recovery_sha256"], hashlib.sha256(chatgpt.canonical(legacy).encode()).hexdigest())
        self.assertEqual(path.read_bytes(), before)
        with patch.object(codex, "read", side_effect=Fault("execution_not_found", "No launch proof.")):
            self.assertEqual(self.app._read_chat_recovery_delivery("structured-one")["chat_recovery"]["delivery"]["status"], "outcome_unknown")
        self.assertEqual(chatgpt.check_state(self.state)["chat_recovery_format"], 3)
        self.assertEqual(chatgpt.get(self.state, "structured-one").delivery.execution_id, binding.delivery.execution_id)

    def test_recovery_slot_is_drained_under_the_observer_lease(self):
        entered, release = threading.Event(), threading.Event()
        recovery, observer = self.app.chat_recovery, self.app.task_observer
        def blocked_scan():
            entered.set()
            release.wait(5)
        with patch.object(recovery, "_scan", side_effect=blocked_scan) as scan:
            observer.start()
            try:
                self.assertTrue(entered.wait(2))
                recovery.tick()
                self.assertEqual(scan.call_count, 1)
                observer.stop()
                self.fault("resource_busy", lease(self.state / "locks/task-observer.lock").__enter__)
            finally:
                release.set()
                observer.stop()
                observer._thread.join(5)
        self.assertFalse(recovery.busy)
        with lease(self.state / "locks/task-observer.lock"):
            self.assertTrue(recovery._stop.is_set())
