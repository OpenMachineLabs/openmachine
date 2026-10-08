"""Foreground task attention and bounded wait behavior."""

from copy import deepcopy
import threading
import unittest
from unittest.mock import patch
import uuid

from openmachine import tasks
from openmachine.app import task_attention
from openmachine.foundation.sanitize import Fault
from test.support.tasks import TaskFixture


class TaskAttentionTests(TaskFixture, unittest.TestCase):
    def seed_running(self) -> dict:
        identity = uuid.uuid4().hex

        def retain(record, now):
            record["operations"].append({"id": identity, "stage_id": "verify",
                "plan_revision": record["plan"]["revision"], "epoch": record["control"]["epoch"],
                "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
                "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
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
        before = self.path.read_bytes()
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
        self.assertEqual(self.path.read_bytes(), before)
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
        self.assertEqual(len(self.view()["record"]["operations"]), 2)


if __name__ == "__main__":
    unittest.main()
