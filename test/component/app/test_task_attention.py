"""Foreground task attention and bounded wait behavior."""

import threading
import unittest
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
        lost = task_attention.project(tasks.read(self.state, "structured-one"),
                                      timestamp=expiry + 1)["foreground"]
        self.assertEqual((lost["status"], lost["reason"]),
                         ("attention_required", "continuation_lost"))

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
        blocked = dict(observing, state="blocked", blocker="observer_heartbeat_stale")
        attention = task_attention.project({"record": record, "observations": [blocked]},
                                           timestamp=timestamp)["foreground"]
        self.assertEqual((attention["status"], attention["reason"], attention["original_work_running"]),
                         ("attention_required", "execution_unverified", None))

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

    def test_attention_blocks_new_effect_but_keeps_reads_available(self):
        accepted = self.accept_plan()["record"]
        expiry = accepted["updated_at_ms"]
        def expire(record, now):
            record["control"]["owner"]["expires_at_ms"] = expiry
            record["control"]["state"] = "planned"
            record["handoff"] = None
        tasks.update(self.state, "structured-one", expire)
        arguments = self.write_arguments(path="blocked.txt")
        with self.assertRaises(Fault) as raised:
            self.app.write_workspace_file(**arguments)
        self.assertEqual(raised.exception.code, "task_attention_required")
        self.assertEqual(raised.exception.evidence["foreground"]["reason"], "continuation_lost")
        self.assertFalse((self.work / "blocked.txt").exists())
        self.assertEqual(self.view()["record"]["operations"], [])
        self.assertEqual(self.app.read_task("structured-one")["foreground"]["reason"], "continuation_lost")


if __name__ == "__main__":
    unittest.main()
