"""Foreground task attention and bounded wait behavior."""

from copy import deepcopy
from contextlib import closing
import sqlite3
import threading
import unittest
from unittest.mock import patch
import uuid

from openmachine import tasks
from openmachine.app import task_attention
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from test.support.tasks import TaskFixture


class TaskAttentionTests(TaskFixture, unittest.TestCase):
    def test_execution_feedback_keeps_receipt_and_reads_only_its_exact_task(self):
        self.accept_plan()
        record = self.seed_running()
        identity = record["operations"][0]["id"]
        tasks.update(self.state, "structured-one", lambda record, now:
                     record["operations"][0]["result"].update(status="succeeded"))
        self.seed_running()
        self.assertNotIn(identity, [item["id"] for item in self.view()["record"]["operations"]])
        self.control("pause", reason="Pause this task only.")
        other = deepcopy(self.request)
        other["task_id"] = "unrelated"
        self.app.create_task(**other)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE tasks SET current=? WHERE task_id=?", ("Unrelated corrupt fixture", "unrelated"))
            connection.commit()
        receipt = {"execution_id": identity, "task_id": "structured-one", "status": "failed",
                   "exit_code": 1, "output": "Original failure output."}
        before = self.task_bytes()
        with patch.object(self.app.machines, "read_execution", return_value=receipt):
            result = self.app.read_execution("desktop", identity)
        feedback = result.pop("task_feedback")
        self.assertEqual(result, receipt)
        self.assertNotIn("task_feedback", receipt)
        self.assertEqual(feedback["status"], "supervised")
        self.assertEqual(feedback["foreground"]["reason"], "task_stopped")
        self.assertTrue(feedback["foreground"]["stop_foreground"])
        self.assertEqual(self.task_bytes(), before)

    def test_unbound_execution_never_infers_a_paused_task_from_its_workspace(self):
        self.accept_plan()
        self.control("pause", reason="Independent work stays independent.")
        receipt = {"execution_id": "a" * 32, "task_id": "", "status": "succeeded", "exit_code": 0}
        with patch.object(self.app.machines, "read_execution", return_value=receipt), \
                patch("openmachine.app.tasks.read", side_effect=AssertionError("Unbound receipt looked up a task.")):
            result = self.app.read_execution("desktop", receipt["execution_id"])
        self.assertEqual(result, {**receipt, "task_feedback": {"status": "unbound"}})

    def test_task_feedback_failures_never_hide_original_execution(self):
        self.accept_plan()
        identity = self.seed_running()["operations"][0]["id"]
        receipt = {"execution_id": identity, "task_id": "structured-one", "status": "succeeded", "exit_code": 0}
        with patch.object(self.app.machines, "read_execution", return_value=receipt):
            with lease(self.state / "locks/tasks.lock"):
                busy = self.app.read_execution("desktop", identity)
            self.assertEqual(busy["task_feedback"]["error_code"], "resource_busy")
            self.path.write_text("corrupt fixture")
            corrupt = self.app.read_execution("desktop", identity)
        for result in (busy, corrupt):
            feedback = result.pop("task_feedback")
            self.assertEqual(result, receipt)
            self.assertEqual(feedback["status"], "unavailable")
            self.assertIn("structured-one", feedback["resume_prompt"])
            self.assertNotIn("foreground", feedback)
        self.assertEqual(self.path.read_text(), "corrupt fixture")

    def test_legacy_or_mismatched_reference_never_claims_supervision(self):
        self.app.save_task("legacy", 0, "Original checkpoint", "Historical work.")
        self.accept_plan()
        self.seed_running()
        for identity, status in (("legacy", "legacy"), ("structured-one", "unavailable")):
            with self.subTest(task_id=identity):
                receipt = {"execution_id": "a" * 32, "task_id": identity, "status": "succeeded"}
                with patch.object(self.app.machines, "read_execution", return_value=receipt):
                    feedback = self.app.read_execution("desktop", receipt["execution_id"])["task_feedback"]
                self.assertEqual(feedback["status"], status)
                self.assertNotIn("foreground", feedback)

    def test_matching_execution_id_on_another_target_is_not_a_verified_binding(self):
        self.accept_plan()
        identity = self.seed_running(target_identity="f" * 64)["operations"][0]["id"]
        receipt = {"execution_id": identity, "task_id": "structured-one", "status": "succeeded"}
        with patch.object(self.app.machines, "read_execution", return_value=receipt):
            result = self.app.read_execution("desktop", identity)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["task_feedback"]["error_code"], "task_operation_conflict")
        self.assertNotIn("foreground", result["task_feedback"])

    def test_scope_rejection_keeps_original_error_and_adds_exact_recovery_context(self):
        self.accept_plan()
        cases = [("context_mismatch", self.app.write_workspace_file,
                  dict(self.write_arguments(), expected_workspace_identity="e" * 64)),
                 ("task_scope_denied", self.app.start_execution,
                  dict(self.start_arguments(), cwd=str(self.root)))]
        before = self.task_bytes()
        for code, operation, arguments in cases:
            with self.subTest(code=code), self.assertRaises(Fault) as caught:
                operation(**arguments)
            error = caught.exception
            self.assertEqual(error.code, code)
            self.assertEqual(error.evidence["task_id"], "structured-one")
            self.assertEqual(error.evidence["operation_id"], arguments["task_context"]["operation_id"])
            self.assertIn("Keep task_context", error.evidence["resume_prompt"])
            self.assertEqual(self.task_bytes(), before)
        self.assertFalse((self.work / "result.txt").exists())
        self.assertEqual(self.app.write_workspace_file(**self.write_arguments())["status"], "applied")

    def seed_running(self, target_identity=None) -> dict:
        identity = uuid.uuid4().hex

        def retain(record, now):
            record["operations"].append({"id": identity, "stage_id": "verify",
                "plan_revision": record["plan"]["revision"], "epoch": record["control"]["epoch"],
                "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
                "locator": {"kind": "execution", "machine": "desktop", "target_identity": target_identity,
                            "execution_id": identity, "control_seq": None},
                "admitted_at_ms": now, "state": "observed", "observed_at_ms": now,
                "result": {"status": "running", "receipt_sha256": "c" * 64,
                    "summary": "Retained running fixture.", "log_offset": 0}})
            record["control"]["state"] = record["plan"]["stages"][0]["state"] = "running"
            record["handoff"] = None

        return tasks.update(self.state, "structured-one", retain)["record"]

    def test_unclaimed_handoff_and_expired_continuation_are_distinct(self):
        record = self.accept_plan(claim=False)["record"]
        handoff = record["handoff"]
        ready = task_attention.project({"record": record, "observations": []},
            timestamp=handoff["created_at_ms"] + task_attention.HANDOFF_GRACE_MS - 1)["foreground"]
        self.assertEqual((ready["status"], ready["reason"], ready["stop_foreground"]),
                         ("ready", "plan_ready", False))
        attention = task_attention.project({"record": record, "observations": []},
            timestamp=handoff["created_at_ms"] + task_attention.HANDOFF_GRACE_MS)["foreground"]
        self.assertEqual((attention["status"], attention["reason"], attention["stop_foreground"]),
                         ("attention_required", "handoff_unconsumed", True))
        self.assertIn("structured-one", attention["resume_prompt"])

        expiry = handoff["created_at_ms"] + task_attention.HANDOFF_GRACE_MS + 1000
        tasks.update(self.state, "structured-one", lambda value, now: value["control"].update(
            owner={"principal_ref": "native-uid:501", "claim_id": "d" * 32, "expires_at_ms": expiry},
            state="planned"))
        expired = task_attention.project(tasks.read(self.state, "structured-one"),
                                      timestamp=expiry + 1)["foreground"]
        self.assertEqual((expired["status"], expired["reason"], expired["stop_foreground"]),
                         ("renewal_required", "continuation_expired", False))

    def test_stop_and_authority_expiry_are_not_recoverable_lease_expiry(self):
        original = self.accept_plan()["record"]
        for stop, reason in (("pause", "task_stopped"), ("cancel", "task_stopped"),
                             ("none", "task_authority_expired")):
            with self.subTest(stop=stop):
                record = deepcopy(original)
                record["control"]["stop_intent"] = stop
                timestamp = record["contract"]["expires_at_ms"]
                foreground = task_attention.project({"record": record}, timestamp=timestamp)["foreground"]
                self.assertEqual((foreground["status"], foreground["reason"], foreground["stop_foreground"]),
                                 ("attention_required", reason, True))

    def test_released_handoff_timeout_can_be_claimed_without_replaying_original_work(self):
        self.accept_plan()
        self.app.write_workspace_file(**self.write_arguments(path="original.txt"))
        released = self.control("release", reason="Hand over completed fixture work.")["record"]
        handoff = released["handoff"]
        original = self.app.read_task("structured-one", section="operations")["items"]
        timestamp = handoff["created_at_ms"] + task_attention.HANDOFF_GRACE_MS

        with patch.object(task_attention, "now_ms", return_value=timestamp):
            before = self.task_bytes()
            for _ in range(2):
                view = self.app.read_task("structured-one")
                foreground = view["foreground"]
                self.assertEqual((foreground["reason"], foreground["stop_foreground"]),
                                 ("handoff_unconsumed", True))
                self.assertIn("subsequent explicit user request", foreground["resume_prompt"])
                self.assertIn(handoff["id"], foreground["resume_prompt"])
                self.assertIn("Do not replay", foreground["resume_prompt"])
            self.assertEqual(self.task_bytes(), before)

            arguments = self.write_arguments(path="continued.txt")
            with self.assertRaises(Fault) as caught:
                self.app.write_workspace_file(**arguments)
            self.assertEqual(caught.exception.code, "task_attention_required")
            self.assertEqual(caught.exception.effect, "known_no_effect")
            self.assertFalse((self.work / "continued.txt").exists())

            self.fault("task_handoff_conflict", self.control, "claim", handoff_id="f" * 32)
            stale = self.control_arguments("claim", handoff_id=handoff["id"])
            stale["expected_revision"] -= 1
            self.fault("task_conflict", self.app.control_task, **stale)
            self.assertEqual(self.task_bytes(), before)

            # Model a later authorized caller; the runtime does not observe Chat turns.
            claimed = self.control("claim", handoff_id=handoff["id"])["record"]
            current = self.app.read_task("structured-one")
            self.assertEqual((current["foreground"]["status"], current["foreground"]["stop_foreground"]),
                             ("ready", False))
            self.assertEqual(claimed["control"]["epoch"], released["control"]["epoch"] + 1)
            self.assertEqual(claimed["handoff"], handoff)
            self.assertEqual(self.app.read_task("structured-one", section="operations")["items"], original)
            self.fault("task_owner_conflict", self.control, "claim", handoff_id=handoff["id"])
            self.fault("task_conflict", self.app.write_workspace_file, **arguments)
            arguments["task_context"] = self.context_for(arguments["change_id"])
            self.assertEqual(self.app.write_workspace_file(**arguments)["status"], "applied")
            self.assertEqual((self.work / "original.txt").read_text(), "original\n")
            self.assertEqual(self.app.read_task("structured-one")["counts"]["operation_count"], 2)

    def test_unclaimed_handoff_timeout_does_not_block_another_task(self):
        original = self.accept_plan(claim=False)["record"]
        timestamp = original["handoff"]["created_at_ms"] + task_attention.HANDOFF_GRACE_MS
        request = deepcopy(self.request)
        request["task_id"] = "independent-task"
        created = self.app.create_task(**request)["record"]
        claimed = self.app.control_task(task_id=created["task_id"], expected_revision=created["revision"],
            expected_plan_revision=created["plan"]["revision"], expected_epoch=created["control"]["epoch"],
            update_id=uuid.uuid4().hex, transition={"action": "claim", "handoff_id": created["handoff"]["id"]})["record"]
        arguments = self.write_arguments(path="independent.txt")
        arguments["task_context"].update(task_id=claimed["task_id"], task_revision=claimed["revision"],
            plan_revision=claimed["plan"]["revision"], continuation_epoch=claimed["control"]["epoch"])
        before = self.task_bytes()
        with patch.object(task_attention, "now_ms", return_value=timestamp):
            self.assertEqual(self.app.read_task("structured-one")["foreground"]["reason"], "handoff_unconsumed")
            self.assertEqual(self.app.write_workspace_file(**arguments)["status"], "applied")
            self.assertEqual(self.app.read_task("structured-one")["foreground"]["reason"], "handoff_unconsumed")
        self.assertEqual(self.task_bytes(), before)
        self.assertEqual((self.work / "independent.txt").read_text(), "original\n")

    def test_silent_verified_execution_keeps_running_but_blocked_observation_stops(self):
        self.accept_plan()
        record = self.seed_running()
        timestamp = record["updated_at_ms"] + 1000
        observing = {"kind": "runtime", "state": "observing", "source": "fixture",
            "observed_at_ms": timestamp, "last_progress_at_ms": record["updated_at_ms"],
            "next_attempt_at_ms": None, "blocker": None}
        running = task_attention.project({"record": record, "observations": [observing]},
                                         timestamp=timestamp)["foreground"]
        self.assertEqual((running["status"], running["stop_foreground"]), ("running", False))
        timestamp = record["control"]["owner"]["expires_at_ms"] + 1
        expired = task_attention.project({"record": record, "observations": [observing]},
                                         timestamp=timestamp)["foreground"]
        self.assertEqual((expired["status"], expired["stop_foreground"], expired["original_work_running"]),
                         ("renewal_required", False, True))
        blocked = dict(observing, state="blocked", blocker="observer_heartbeat_stale")
        attention = task_attention.project({"record": record, "observations": [blocked]},
                                           timestamp=timestamp)["foreground"]
        self.assertEqual((attention["status"], attention["reason"], attention["original_work_running"]),
                         ("attention_required", "execution_unverified", None))

    def test_unverified_unresolved_execution_blocks_disjoint_effect(self):
        self.accept_plan()
        record = self.seed_running()
        timestamp = record["updated_at_ms"] + 1000
        runtime = {"kind": "runtime", "state": "blocked", "source": "fixture",
            "observed_at_ms": timestamp, "last_progress_at_ms": record["updated_at_ms"],
            "next_attempt_at_ms": None, "blocker": "observer_heartbeat_stale"}

        def blocked(view):
            return {**view, "observations": [runtime]}

        arguments = self.write_arguments(path="disjoint.txt")
        with patch.object(self.app.task_observer, "project", side_effect=blocked):
            with self.assertRaises(Fault) as raised:
                self.app.write_workspace_file(**arguments)
        self.assertEqual(raised.exception.code, "task_attention_required")
        self.assertEqual(raised.exception.evidence["foreground"]["reason"], "execution_unverified")
        self.assertFalse((self.work / "disjoint.txt").exists())
        self.assertEqual(len(self.view()["record"]["operations"]), 1)

    def test_wait_task_expires_once_and_wakes_on_revision_change(self):
        self.accept_plan()
        record = self.seed_running()
        expired = self.app.wait_task("structured-one", record["revision"], 0)
        self.assertEqual((expired["wait"]["status"], expired["foreground"]["status"],
                          expired["foreground"]["stop_foreground"]),
                         ("expired", "wait_expired", True))
        self.assertTrue(expired["foreground"]["original_work_running"])

        current = self.view()["record"]
        def advance():
            tasks.update(self.state, "structured-one",
                lambda value, now: value["control"].update(pending_decision="Fixture progress."))
            self.app.task_observer.notify()
        timer = threading.Timer(0.05, advance)
        timer.start()
        try:
            changed = self.app.wait_task("structured-one", current["revision"], 0.5)
        finally:
            timer.join(1)
        self.assertEqual(changed["wait"]["status"], "changed")
        self.assertGreater(changed["wait"]["current_revision"], current["revision"])
        self.assertFalse(changed["foreground"]["stop_foreground"])

    def test_public_summaries_derive_attention_from_internal_operations(self):
        self.accept_plan()
        record = self.seed_running()
        identity = record["operations"][0]["id"]
        summary = self.app.read_task("structured-one")
        waited = self.app.wait_task("structured-one", record["revision"], 0)
        paused = self.app.control_task(**self.control_arguments("pause", reason="Retain the original operation."))
        for view in (summary, waited, paused):
            self.assertEqual((view["read_schema"], view["section"]), (2, "summary"))
            self.assertFalse(view["record_complete"])
            self.assertNotIn("operations", view["record"])
            self.assertNotIn("history", view["record"])
            self.assertNotIn("_facts", view["record"])
            self.assertNotIn("_decisions", view["record"])
        self.assertEqual(summary["foreground"]["operation_id"], identity)
        self.assertTrue(waited["foreground"]["original_work_running"])
        self.assertEqual(paused["record"]["control"]["stop_intent"], "pause")
        exact = self.app.read_task("structured-one", section="operations", operation_id=identity)
        self.assertEqual([operation["id"] for operation in exact["items"]], [identity])

    def test_stage_evidence_references_remain_exact_after_new_decisions(self):
        self.accept_plan()
        initial = self.app.read_task("structured-one")["record"]["plan"]["stages"][0]
        self.assertNotIn("evidence", initial)
        reference = initial["evidence_ref"]
        before = self.app.read_task(**reference)
        self.assertEqual((before["items"][0]["state"], before["items"][0]["evidence"]), ("pending", []))
        self.seed_running()
        current = self.app.read_task("structured-one")["record"]["plan"]["stages"][0]
        self.assertGreater(current["evidence_ref"]["decision_version"], reference["decision_version"])
        self.assertEqual(self.app.read_task(**reference)["items"], before["items"])
        self.assertEqual(self.app.read_task(**current["evidence_ref"])["items"][0]["state"], "running")
        preserved = self.task_bytes()
        for arguments in ({"stage_id": "verify"}, {"decision_version": 1},
                          {"stage_id": "verify", "decision_version": True},
                          {"stage_id": "verify", "decision_version": 1, "section": "summary"},
                          {"stage_id": "verify", "decision_version": 1, "cursor": "retained"},
                          {"stage_id": "verify", "decision_version": 1, "operation_id": "a" * 32}):
            self.fault("invalid_task_read", self.app.read_task, "structured-one", **{"section": "evidence", **arguments})
        self.assertEqual(self.task_bytes(), preserved)

    def test_expired_claim_requires_explicit_renewal_before_new_effect(self):
        accepted = self.accept_plan()["record"]
        original_args = self.write_arguments(path="original.txt")
        self.app.write_workspace_file(**original_args)
        original = self.view()["record"]["operations"]
        expiry = accepted["updated_at_ms"]
        def expire(record, now):
            record["control"]["owner"]["expires_at_ms"] = expiry
            record["control"]["state"] = "planned"
            record["handoff"] = None
        tasks.update(self.state, "structured-one", expire)
        before = self.task_bytes()
        arguments = self.write_arguments(path="blocked.txt")
        with self.assertRaises(Fault) as raised:
            self.app.write_workspace_file(**arguments)
        self.assertEqual(raised.exception.code, "task_renewal_required")
        self.assertEqual(raised.exception.effect, "known_no_effect")
        self.assertFalse(raised.exception.evidence["foreground"]["stop_foreground"])
        self.assertFalse((self.work / "blocked.txt").exists())
        self.assertEqual(self.view()["record"]["operations"], original)
        record = self.view()["record"]
        with patch.object(self.app.task_observer, "wait") as wait:
            ready = self.app.wait_task("structured-one", record["revision"], 30)
        wait.assert_not_called()
        self.assertEqual(ready["wait"]["status"], "renewal_required")
        self.assertFalse(ready["foreground"]["stop_foreground"])
        self.assertEqual(self.task_bytes(), before)
        retained = self.app.write_workspace_file(**original_args)
        self.assertFalse(retained["task_operation_replayed"])
        self.assertEqual(self.view()["record"]["operations"], original)
        renewed = self.control("renew", claim_id=record["control"]["owner"]["claim_id"])["record"]
        self.assertEqual(renewed["control"]["epoch"], record["control"]["epoch"])
        self.assertEqual(renewed["operations"], original)
        self.fault("task_conflict", self.app.write_workspace_file, **arguments)
        arguments["task_context"] = self.context_for(arguments["change_id"])
        self.assertEqual(self.app.write_workspace_file(**arguments)["status"], "applied")
        self.assertEqual((self.work / "blocked.txt").read_text(), "original\n")
        self.assertEqual(self.view()["facts"]["operation_count"], 2)
        self.assertEqual(len(self.view()["record"]["operations"]), 1)


if __name__ == "__main__":
    unittest.main()
