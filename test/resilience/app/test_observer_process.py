"""Tests for app behavior at the resilience layer."""

import json
import unittest
from unittest.mock import patch
import uuid
from openmachine.tasks import store as task_store
from openmachine import tasks
from openmachine.bootstrap import create_application
from test.support.task_observer_cases import TaskObserverCaseFixture


class TaskObserverTests(TaskObserverCaseFixture, unittest.TestCase):
    def test_expired_claim_preserves_live_command_and_renews_after_result(self):
        args, started = self.owned()
        observer = self.app.task_observer
        tasks.update(self.state, "structured-one", lambda value, now:
                     value["control"]["owner"].update(expires_at_ms=now - 1))
        with observer.running():
            self.until(lambda: observer._snapshot["last"].get("structured-one"))
            expired = self.until(self.view)
            owner = expired["record"]["control"]["owner"]
            self.assertEqual(expired["foreground"]["status"], "renewal_required")
            self.assertFalse(expired["foreground"]["stop_foreground"])
            live = self.app.read_execution("desktop", args["execution_id"])
            self.assertEqual(live["status"], "running")
            self.assertIsNone(live["stop"]["reason"])
            # The caller performs no renewal while the original worker finishes.
            with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("No background effects")):
                (self.work / "release").touch()
                self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
                completed = self.until(self.view)
                self.assertEqual(completed["record"]["control"]["owner"], owner)
                self.assertEqual(completed["foreground"]["status"], "renewal_required")
                self.assertFalse(completed["foreground"]["stop_foreground"])
                renewed = self.control("renew", claim_id=owner["claim_id"])["record"]
                self.assertEqual(renewed["operations"], completed["record"]["operations"])
                self.assertEqual(renewed["control"]["epoch"], completed["record"]["control"]["epoch"])
        self.assertEqual(self.wait(started)["status"], "succeeded")
        retained = self.app.start_execution(**args)
        self.assertFalse(retained["task_operation_replayed"])
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.assertEqual(self.app.write_workspace_file(**self.write_arguments())["status"], "applied")
        self.assertEqual(len(self.view()["record"]["operations"]), 2)

    def test_absent_caller_result_and_handoff_publish_without_replaying_or_accepting(self):
        args, started = self.owned()
        archive = self.state / "events.json"
        archive.write_text("obsolete malformed Events archive")
        archive_before = archive.read_bytes()
        observer = self.app.task_observer
        with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("No background effects")), observer.running():
            self.until(lambda: observer._snapshot["last"].get("structured-one"))
            before = self.raw()
            progress = before["operations"][0]["observed_at_ms"]
            observed = observer._snapshot["last"]["structured-one"][args["execution_id"]]
            self.until(lambda: observer._snapshot["last"].get("structured-one", {}).get(args["execution_id"], 0) > observed)
            self.assertEqual(self.raw(), before, "Quiet observations must not revise canonical history")
            self.assertEqual(self.raw()["operations"][0]["observed_at_ms"], progress)
            (self.work / "release").touch()
            # Only read durable bytes after the caller leaves; no Application
            # reconciliation, task control, execution retry or result-save call.
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
            final = self.raw()
            self.assertEqual(final["handoff"]["reason"], "result_ready")
            self.assertEqual(final["control"]["state"], "waiting_for_ai")
            self.assertEqual(final["control"]["owner"], before["control"]["owner"])
            self.assertEqual(final["plan"]["stages"][0]["state"], "running")
            self.assertIsNone(final["terminal"])
            self.assertEqual(final["operations"][0]["id"], args["execution_id"])
            self.until(lambda: "structured-one" not in observer._snapshot["active"])
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.assertEqual(self.wait(started)["created_at"], started["created_at"])
        self.assertEqual(archive.read_bytes(), archive_before)
        self.assertFalse(self.view()["support"]["events_delivery"])
        self.assertFalse((self.state / "locks/events.lock").exists())

    def test_observer_restart_collects_a_result_completed_while_it_was_stopped(self):
        args, started = self.owned()
        with self.app.task_observer.running():
            self.until(lambda: self.app.task_observer._snapshot["last"].get("structured-one"))
        before = self.path.read_bytes()
        (self.work / "release").touch()
        original = self.wait(started)
        self.assertEqual(original["status"], "succeeded")
        self.assertEqual(self.path.read_bytes(), before)
        other = create_application(self.config, self.state)
        with other.task_observer.running():
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
        self.assertEqual(len(self.raw()["operations"]), 1)
        self.assertEqual(self.raw()["operations"][0]["id"], args["execution_id"])
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_pause_wins_over_a_late_read_and_original_results_still_arrive(self):
        import threading
        _, started = self.owned()
        entered, release = threading.Event(), threading.Event()
        original = self.app.task_admission._read
        def delayed(operation):
            result = original(operation)
            if not entered.is_set():
                entered.set()
                release.wait(5)
            return result
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=delayed), self.app.task_observer.running():
                self.assertTrue(entered.wait(3))
                paused = self.control("pause", reason="Pause during an original read.")["record"]
                release.set()
                (self.work / "release").touch()
                self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
                record = self.raw()
                self.assertEqual(record["control"], paused["control"])
                self.assertIsNone(record["handoff"])
                self.assertIsNone(record["terminal"])
        finally:
            release.set()
        self.assertEqual(self.wait(started)["status"], "succeeded")
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_late_background_result_cannot_change_finished_history(self):
        import threading
        args, started = self.owned()
        (self.work / "release").touch()
        self.wait(started)
        entered, release = threading.Event(), threading.Event()
        original = self.app.task_admission._read
        def delayed(operation, *args, **kwargs):
            result = original(operation, *args, **kwargs)
            if threading.current_thread().name == "om-task-read":
                entered.set()
                release.wait(5)
            return result
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=delayed), self.app.task_observer.running():
                self.assertTrue(entered.wait(3))
                self.app.task_admission.reconcile(self.view())
                self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["execution_id"]], reason="Read the original result.")
                self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["execution_id"]}], limitations="Local fixture.")
                final = self.path.read_bytes()
                release.set()
                self.until(lambda: not self.app.task_observer._reads)
                self.view()
                self.assertEqual(self.path.read_bytes(), final)
                self.assertEqual(self.view()["observations"][1]["state"], "retired")
        finally:
            release.set()

    def test_process_loss_before_result_publication_retains_original_work(self):
        self.publication_crash("before")

    def test_process_loss_after_result_publication_retains_the_same_handoff(self):
        self.publication_crash("after")

    def test_original_pty_input_result_is_collected_without_sending_input_again(self):
        self.accept_plan()
        code = ('from pathlib import Path\nimport sys,time\nprint("READY",flush=True)\n'
            'line=sys.stdin.readline()\np=Path("input-count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
            'print("ACK",flush=True)\nwhile not Path("input-release").exists(): time.sleep(0.02)\n')
        args = self.start_arguments(code, tty=True, timeout_seconds=15)
        started = self.app.start_execution(**args)
        self.addCleanup(lambda: self.wait(self.app.cancel_execution("desktop", args["execution_id"])))
        def ready():
            original = self.app.read_execution("desktop", args["execution_id"])
            return original if original["control_ready"] and "READY" in original["output"] else None
        self.until(ready)
        self.app.task_admission.reconcile(self.view())
        operation_id = uuid.uuid4().hex
        input_args = dict(machine="desktop", execution_id=args["execution_id"], expected_control_seq=0,
            action="write_stdin", text="single-observed-pty-input\n", task_context=self.context_for(operation_id))
        original_replace = task_store._replace
        def lose_result(path, raw):
            if path == self.path and any(item["id"] == operation_id and item["result"] is not None for item in json.loads(raw)["operations"]):
                raise OSError("Disposable lost input-result publication")
            return original_replace(path, raw)
        with patch.object(task_store, "_replace", side_effect=lose_result):
            self.fault("task_result_unverified", self.app.control_execution, **input_args)
        self.until(lambda: (self.work / "input-count").exists())
        with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("Never resend terminal input")), self.app.task_observer.running():
            self.until(lambda: (self.raw()["operations"][1]["result"] or {}).get("status") == "succeeded")
            self.assertEqual(self.raw()["operations"][1]["locator"]["control_seq"], 1)
            self.assertNotIn("single-observed-pty-input", self.path.read_text())
            self.assertEqual((self.work / "input-count").read_text(), "1")
            (self.work / "input-release").touch()
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
        self.assertEqual(self.wait(started)["status"], "succeeded")
        self.assertEqual((self.work / "input-count").read_text(), "1")
        self.assertIsNone(self.raw()["terminal"])

    def test_lost_cancellation_result_retires_only_after_original_worker_evidence(self):
        self.accept_plan()
        code = ('from pathlib import Path\nimport signal,time,sys\n'
            'def stop(*args):\n time.sleep(0.35)\n sys.exit(0)\n'
            'signal.signal(signal.SIGTERM,stop)\n'
            'p=Path("cancel-count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
            'print("READY",flush=True)\nwhile True: time.sleep(0.02)\n')
        args = self.start_arguments(code, timeout_seconds=15)
        started = self.app.start_execution(**args)
        self.addCleanup(lambda: self.wait(self.app.cancel_execution("desktop", args["execution_id"])))
        self.until(lambda: "READY" in self.app.read_execution("desktop", args["execution_id"])["output"])
        self.app.task_admission.reconcile(self.view())
        original_replace = task_store._replace
        def lose_result(path, raw):
            if path == self.path and any(item["id"] != args["execution_id"] and item["result"] is not None for item in json.loads(raw)["operations"]):
                raise OSError("Disposable lost cancellation-result publication")
            return original_replace(path, raw)
        with patch.object(task_store, "_replace", side_effect=lose_result):
            self.fault("task_result_unverified", self.control, "cancel", reason="Cancel the original fixture only.")
        self.assertEqual(self.raw()["control"]["stop_intent"], "cancel")
        self.assertIsNone(self.raw()["terminal"])
        with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("No repeated cancellation")), self.app.task_observer.running():
            self.until(lambda: self.raw()["terminal"])
            record = self.raw()
            self.assertEqual(record["terminal"]["outcome"], "cancelled")
            self.assertEqual(record["control"]["state"], "cancelled")
            self.assertIsNone(record["handoff"])
            self.assertTrue(all(item["result"]["status"] == "cancelled" for item in record["operations"]))
            self.until(lambda: "structured-one" not in self.app.task_observer._snapshot["active"])
        original = self.wait(started)
        self.assertEqual(original["status"], "cancelled")
        self.assertEqual(original["exit_code"], 0, "Handled cancellation is not successful execution")
        self.assertEqual((self.work / "cancel-count").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
