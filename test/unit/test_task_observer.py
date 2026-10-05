"""Behavior checks for TaskObserverTests."""

import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from openmachine import tasks
from openmachine.tasks import store as task_store
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from support_tasks import ObserverFixture, task_contract, terminal_task_fixture


class TaskObserverTests(ObserverFixture, unittest.TestCase):
    def test_construction_reads_and_discovery_never_start_or_initialize_an_observer(self):
        self.assertFalse(self.state.exists())
        self.assertEqual(tasks.observation_page(self.state, {})["entries"], [])
        self.assertFalse(self.state.exists())
        self.assertIsNone(self.app.task_observer._thread)
        self.accept_plan()
        before = self.path.read_bytes()
        for _ in range(3):
            result = self.view()
            self.app.find_tasks()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(result["support"]["local_result_observation"])
        self.assertEqual(result["observations"][0]["state"], "unknown")
        self.assertFalse((self.state / "locks/task-observer.lock").exists())
        self.interval.stop()
        self.assertEqual((self.observer_module.INTERVAL_MS, self.observer_module.READ_SECONDS,
            self.observer_module.MAX_READERS, self.observer_module.MAX_ACTIVE,
            self.observer_module.EPISODE_MS, self.observer_module.MAX_ATTEMPTS), (2000, 2.0, 4, 32, 3600000, 120))

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

    def test_lost_file_result_is_collected_from_the_original_change_only(self):
        self.accept_plan()
        args = self.write_arguments()
        original = task_store._replace
        def fail_result(path, raw):
            if path.name == "task.json" and any(item["result"] is not None for item in json.loads(raw)["operations"]):
                raise OSError("Disposable lost result publication")
            return original(path, raw)
        with patch.object(task_store, "_replace", side_effect=fail_result):
            self.fault("task_result_unverified", self.app.write_workspace_file, **args)
        receipt = self.app.read_workspace_change("desktop", args["change_id"])
        with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("No repeated change")), self.app.task_observer.running():
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")
        self.assertEqual(self.app.read_workspace_change("desktop", args["change_id"])["request_sha256"], receipt["request_sha256"])
        self.assertEqual(self.raw()["handoff"]["reason"], "result_ready")

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

    def test_failure_episode_pause_restart_and_cooldown_preserve_attempts(self):
        identity = self.seed()[0]
        observer = self.app.task_observer
        attempts = []
        def unavailable(operation):
            recovery = self.raw(identity)["recovery"]
            attempts.append(None if recovery is None else recovery["attempts"])
            raise Fault("source_unavailable", "Disposable observation failure")
        with patch.object(self.app.task_admission, "_read", side_effect=unavailable), patch.object(self.observer_module.random, "uniform", return_value=1):
            with observer.running():
                self.until(lambda: self.raw(identity)["recovery"])
            first = self.raw(identity)["recovery"]
            record = self.app.read_task(identity)["record"]
            paused = self.app.control_task(task_id=identity, expected_revision=record["revision"], expected_plan_revision=1,
                expected_epoch=0, update_id=uuid.uuid4().hex, transition={"action": "pause", "reason": "Keep the observation budget."})["record"]
            self.assertEqual(paused["recovery"], first)
            with observer.running():
                self.until(lambda: self.raw(identity)["recovery"]["attempts"] == 3)
            record = self.raw(identity)
            self.assertEqual(attempts, [None, 2, 3], "Each retry must reserve its attempt before reading")
            self.assertEqual(record["recovery"]["episode_id"], first["episode_id"])
            self.assertEqual(record["recovery"]["deadline_ms"], first["deadline_ms"])
            self.assertGreater(record["recovery"]["next_attempt_ms"] - record["updated_at_ms"], 29000)
            self.assertEqual(record["control"]["state"], "paused")
            self.assertIsNone(record["handoff"])
            self.assertEqual(sum(item["kind"] == "recovery" for item in record["history"]), 1)
            snapshot = self.path.parent.parent / identity / "task.json"
            before = snapshot.read_bytes()
            with observer.running():
                time.sleep(0.15)
            self.assertEqual(snapshot.read_bytes(), before)

    def test_exhausted_and_permission_episodes_do_not_restart_reads(self):
        identity = self.seed()[0]
        def exhausted(record, now):
            self.observer_module.fail_observation(record, now, now, "source_unavailable")
            record["recovery"]["attempts"] = 120
        tasks.update(self.state, identity, exhausted)
        before = self.raw(identity)
        with patch.object(self.app.task_admission, "_read", side_effect=AssertionError("Budget exhausted")) as read:
            for _ in range(2):
                with self.app.task_observer.running():
                    self.until(lambda: self.app.task_observer._snapshot["errors"].get(identity))
                read.assert_not_called()
        self.assertEqual(self.raw(identity), before)
        self.assertEqual(self.app.read_task(identity)["observations"][1]["blocker"], "observation_budget_exhausted")
        tasks.update(self.state, identity, lambda record, now: record.update(recovery=None))
        with patch.object(self.app.task_admission, "_read", side_effect=Fault("path_denied", "Never retry permission")) as read:
            with self.app.task_observer.running():
                self.until(lambda: self.raw(identity)["recovery"])
                time.sleep(0.2)
            self.assertEqual(read.call_count, 1)
        self.assertEqual(self.raw(identity)["recovery"]["reason_code"], "blocked_path_denied")

    def test_timed_out_threads_keep_all_four_slots_and_owner_until_actual_exit(self):
        import threading
        identities = self.seed(5)
        release = threading.Event()
        entered, lock = [], threading.Lock()
        def slow(operation):
            with lock:
                entered.append(operation["id"])
            release.wait(10)
            return self.fixture_result(operation)
        observer = self.app.task_observer
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=slow):
                observer.start()
                self.until(lambda: len(entered) == 4)
                with lease(self.state / "locks/lifecycle.lock"), lease(self.state / "locks/tasks.lock"):
                    pass
                self.until(lambda: all(self.raw(identity)["recovery"] for identity in identities[:4]))
                self.assertEqual(len(entered), 4)
                self.assertEqual(len(observer._reads), 4)
                self.assertIsNone(self.raw(identities[4])["recovery"])
                self.until(lambda: self.app.read_task(identities[0]))
                observer.stop()
                with self.assertRaises(Fault):
                    with lease(self.state / "locks/task-observer.lock"):
                        pass
                self.fault("task_observer_busy", observer.start)
                release.set()
                self.until(lambda: not observer._thread.is_alive())
                self.assertEqual(len(entered), 4)
                with lease(self.state / "locks/task-observer.lock"):
                    pass
        finally:
            release.set()
            observer.stop()

    def test_coordinator_failure_does_not_release_a_live_reader_owner(self):
        import threading
        self.seed()
        release, entered = threading.Event(), threading.Event()
        def slow(operation):
            entered.set()
            release.wait(10)
            return self.fixture_result(operation)
        observer = self.app.task_observer
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=slow):
                observer.start()
                self.assertTrue(entered.wait(3))
                with patch.object(tasks, "observation_page", side_effect=RuntimeError("Disposable coordinator failure")):
                    observer.notify()
                    self.until(lambda: observer._snapshot["state"] == "blocked")
                    with self.assertRaises(Fault):
                        with lease(self.state / "locks/task-observer.lock"):
                            pass
                release.set()
                observer.stop()
        finally:
            release.set()
            observer.stop()

    def test_no_remote_or_provider_observation_and_changed_local_route_is_blocked(self):
        identities = self.seed(3)
        def remote(record, now):
            record["operations"][0]["locator"]["target_identity"] = "a" * 64
        def provider(record, now):
            binding = {"connection_id": "c" * 32, "account_id": 1, "authorization_revision": 1}
            record["contract"]["repositories"] = ["Example/repo"]
            record["contract"]["github_connections"] = {"Example/repo": binding}
            record["operations"][0]["locator"] = {"kind": "github", "repository": "Example/repo", "object_type": "issue",
                "number": 1, "parent_number": None, "anchor": "fixture-observation", "connection": binding}
        tasks.update(self.state, identities[0], remote)
        tasks.update(self.state, identities[1], provider)
        del self.config.machines["desktop"]
        with patch.object(self.app.task_admission, "_read", side_effect=AssertionError("No alternate route")) as read, self.app.task_observer.running():
            self.until(lambda: self.raw(identities[2])["recovery"])
            time.sleep(0.2)
            read.assert_not_called()
            self.assertEqual(set(self.app.task_observer._known), set(identities))
        self.assertIsNone(self.raw(identities[0])["recovery"])
        self.assertIsNone(self.raw(identities[1])["recovery"])
        self.assertTrue(self.raw(identities[2])["recovery"]["reason_code"].startswith("blocked_"))

    def test_discovery_pages_do_not_reread_unchanged_history_and_report_stale_index(self):
        for index in range(55):
            self.app.create_task(**task_contract(self.context, f"history-{index:03}"))
        first = tasks.observation_page(self.state, {})
        self.assertEqual(len(first["entries"]), 50)
        self.assertTrue(first["has_more"])
        known = {item["task_id"]: item["metadata_sha256"] for item in first["entries"]}
        second = tasks.observation_page(self.state, known)
        self.assertEqual(len(second["entries"]), 5)
        known.update({item["task_id"]: item["metadata_sha256"] for item in second["entries"]})
        with patch.object(task_store, "read_structured", side_effect=AssertionError("Do not poll history")):
            self.assertEqual(tasks.observation_page(self.state, known)["entries"], [])
        with patch.object(task_store, "_write_index", side_effect=OSError("Stale derived index")):
            tasks.update(self.state, "history-000", lambda record, now: record["control"].update(pending_decision="Fixture pending decision."))
        observed = tasks.observation_page(self.state, {})
        self.assertFalse(observed["complete"])
        self.assertEqual(observed["entries"][0]["record"]["control"]["pending_decision"], "Fixture pending decision.")
        self.assertEqual(tasks.read(self.state, "history-000")["index_status"], "stale")

    def test_service_admission_blocks_publication_but_not_original_reads(self):
        import threading
        self.seed()
        entered, release = threading.Event(), threading.Event()
        def slow(operation):
            entered.set()
            release.wait(5)
            return self.fixture_result(operation)
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=slow), self.app.task_observer.running():
                self.assertTrue(entered.wait(3))
                with lease(self.state / "locks/lifecycle.lock"):
                    release.set()
                    time.sleep(0.2)
                    self.assertIsNone(self.raw("observer-000")["operations"][0]["result"])
                    self.app.read_task("observer-000")
                self.until(lambda: self.raw("observer-000")["operations"][0]["result"])
        finally:
            release.set()

    def publication_crash(self, phase):
        _, started = self.owned()
        (self.work / "release").touch()
        self.wait(started)
        payload = self.root / "observer-crash.json"
        payload.write_text(json.dumps({"config": self.config.model_dump(mode="json"), "state": str(self.state)}))
        code = '''import json,os,sys,time
from pathlib import Path
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.tasks import store
value=json.loads(Path(sys.argv[1]).read_text())
original=store._replace
def crash(path,raw):
 if path.name=="task.json" and any((item["result"] or {}).get("status")=="succeeded" for item in json.loads(raw)["operations"]):
  if sys.argv[2]=="before": os._exit(67)
  original(path,raw)
  os._exit(68)
 return original(path,raw)
store._replace=crash
app=create_application(Settings.model_validate(value["config"]),Path(value["state"]))
with app.task_observer.running(): time.sleep(12)
os._exit(69)
'''
        result = subprocess.run([sys.executable, "-I", "-c", code, str(payload), phase], capture_output=True, text=True, timeout=18)
        self.assertEqual(result.returncode, 67 if phase == "before" else 68, result.stderr)
        prior = self.raw()
        with self.app.task_observer.running():
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
            self.until(lambda: self.app.task_observer._snapshot["state"] == "running")
        final = self.raw()
        self.assertEqual(final["handoff"]["reason"], "result_ready")
        if phase == "after":
            self.assertEqual(final, prior, "A committed result/handoff must not be republished")
            self.assertEqual(self.view()["index_status"], "stale")
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.assertEqual(len(final["operations"]), 1)
        self.assertEqual(self.wait(started)["created_at"], started["created_at"])

    def test_process_loss_before_result_publication_retains_original_work(self):
        self.publication_crash("before")

    def test_process_loss_after_result_publication_retains_the_same_handoff(self):
        self.publication_crash("after")

    def test_start_during_lifecycle_activation_waits_without_losing_original_work(self):
        identity = self.seed()[0]
        observer = self.app.task_observer
        with patch.object(self.app.task_admission, "_read", side_effect=self.fixture_result) as read:
            with lease(self.state / "locks/lifecycle.lock"):
                observer.start()
                time.sleep(0.15)
                read.assert_not_called()
                self.assertIsNone(self.raw(identity)["operations"][0]["result"])
            self.until(lambda: (self.raw(identity)["operations"][0]["result"] or {}).get("status") == "succeeded")
            self.assertEqual(read.call_count, 1)
        self.assertEqual(self.raw(identity)["handoff"]["reason"], "result_ready")

    def test_active_capacity_is_visible_and_overflow_is_collected_after_retirement(self):
        import threading
        identities = self.seed(35)
        release = threading.Event()
        observed = []
        def slow(operation):
            observed.append(operation["id"])
            release.wait(8)
            return self.fixture_result(operation)
        observer = self.app.task_observer
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=slow), observer.running():
                self.until(lambda: len(observer._snapshot["active"]) == 32)
                self.until(lambda: observer._snapshot["errors"].get(identities[-1]) == "task_observer_capacity")
                self.assertLessEqual(len(observer._reads), 4)
                self.assertEqual(self.app.read_task(identities[-1])["observations"][1]["blocker"], "task_observer_capacity")
                release.set()
                self.until(lambda: all((self.raw(identity)["operations"][0]["result"] or {}).get("status") == "succeeded" for identity in identities))
                self.until(lambda: not observer._snapshot["active"])
                count = len(observed)
                time.sleep(0.2)
                self.assertEqual(len(observed), count)
                self.assertEqual(count, 35)
        finally:
            release.set()
            observer.stop()

    def test_corrupt_discovery_reads_still_charge_the_aggregate_byte_budget(self):
        for index in range(35):
            identity = f"corrupt-{index:03}"
            self.app.create_task(**task_contract(self.context, identity))
        for path in (self.state / "tasks").glob("corrupt-*/task.json"):
            path.write_bytes(b"x" * tasks.MAX_RECORD)
        # Invalid retained bytes are intentional fixture corruption. They must
        # not bypass the 2 MiB budget, be repaired, or be treated as missing work.
        page = tasks.observation_page(self.state, {})
        self.assertEqual(len(page["entries"]), 32)
        self.assertTrue(page["has_more"])
        self.assertFalse(page["complete"])
        self.assertTrue(all(item["error"] is not None for item in page["entries"]))
        self.assertEqual((self.state / "tasks/corrupt-000/task.json").read_bytes(), b"x" * tasks.MAX_RECORD)

    def test_stopped_observer_does_not_advertise_a_live_retry_timer(self):
        identity = self.seed()[0]
        with patch.object(self.app.task_admission, "_read", side_effect=Fault("source_unavailable", "Fixture outage")), self.app.task_observer.running():
            self.until(lambda: self.raw(identity)["recovery"])
        before = self.raw(identity)
        view = self.app.read_task(identity)
        runtime = view["observations"][1]
        self.assertEqual(runtime["state"], "stopped")
        self.assertIsNone(runtime["next_attempt_at_ms"])
        self.assertIsNotNone(view["record"]["recovery"]["next_attempt_ms"])
        self.assertEqual(self.raw(identity), before)

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

    def test_partial_receipt_read_does_not_refresh_an_unread_control(self):
        from copy import deepcopy
        identity = self.seed()[0]
        def add_control(record, now):
            operation = deepcopy(record["operations"][0])
            operation["id"] = "f" * 32
            operation["locator"]["control_seq"] = 1
            record["operations"].append(operation)
        tasks.update(self.state, identity, add_control)
        def partial(operation):
            if operation["locator"]["control_seq"] is not None:
                raise Fault("task_evidence_unavailable", "Original control receipt unavailable.")
            return {"execution_id": operation["id"], "status": "running", "created_at": 1.0}
        with patch.object(self.app.task_admission, "_read", side_effect=partial), self.app.task_observer.running():
            self.until(lambda: self.app.task_observer._snapshot["last"].get(identity))
            view = self.until(lambda: self.app.read_task(identity))
            command, control = view["observations"][2:4]
            self.assertIsNotNone(command["observed_at_ms"])
            self.assertIsNone(control["observed_at_ms"])
            self.assertEqual(control["state"], "stale")
            self.assertEqual(view["record"]["recovery"]["reason_code"], "blocked_task_evidence_unavailable")

    def test_stale_recovery_heartbeat_has_no_live_timer(self):
        identity = self.seed()[0]
        tasks.update(self.state, identity, lambda value, now: self.observer_module.fail_observation(value, now, now, "source_unavailable"))
        observer = self.app.task_observer
        observer._snapshot = dict(observer._snapshot, state="running", observed_at_ms=self.observer_module.now_ms() - 1000)
        view = self.app.read_task(identity)
        self.assertEqual(view["observations"][1]["state"], "stale")
        self.assertEqual(view["observations"][1]["blocker"], "observer_heartbeat_stale")
        self.assertIsNone(view["observations"][1]["next_attempt_at_ms"])
        self.assertIsNotNone(view["record"]["recovery"])

    def test_permanent_result_publication_failure_stops_the_affected_read_loop(self):
        identity = self.seed()[0]
        before = self.raw(identity)
        with patch.object(self.app.task_admission, "_read", side_effect=self.fixture_result) as read:
            with patch.object(task_store, "_replace", side_effect=Fault("task_capacity", "Disposable publication blocker.")):
                with self.app.task_observer.running():
                    self.until(lambda: self.app.task_observer._snapshot["errors"].get(identity) == "task_capacity")
                    time.sleep(0.3)
                    self.assertEqual(read.call_count, 1)
                    self.assertNotIn(identity, self.app.task_observer._snapshot["active"])
                    self.assertEqual(self.raw(identity), before)
                    view = self.app.read_task(identity)
                    self.assertEqual(view["observations"][1]["state"], "blocked")
                    self.assertIsNone(view["observations"][1]["next_attempt_at_ms"])
