"""Behavior checks for SSHObservationTests."""

import unittest
from unittest.mock import patch
import uuid
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from test.support.tasks import ObserverFixture, task_contract, terminal_task_fixture


class SSHObservationTests(ObserverFixture, unittest.TestCase):
    def remote_tasks(self, count=1, *, routes=None):
        from openmachine.config import Machine
        for name, identity in (("remote", "a" * 64), ("alias", "a" * 64), ("healthy", "b" * 64)):
            self.config.machines[name] = Machine(transport="ssh", ssh_alias=name,
                ssh_worker="/fixture/" + "c" * 64 + ".pyz", ssh_identity=identity)
        self.app = create_application(self.config, self.state)
        self.addCleanup(self.app.task_observer.stop)
        self.enterContext(patch.object(self.observer_module, "SSH_INTERVAL_MS", 100))
        def configure(record, index):
            machine, target = (routes or {}).get(index, ("alias" if index % 2 else "remote", "a" * 64))
            record["operations"][0]["locator"].update(machine=machine, target_identity=target)
        return self.seed(count, configure=configure)

    def test_shared_outage_recovers_original_results_without_effect_dispatch(self):
        identities = self.remote_tasks(3)
        attempts = []
        def read(operation, **kwargs):
            attempts.append(operation["id"])
            if len(attempts) == 1:
                raise Fault("ssh_unavailable", "Fixture outage", {"ssh_retryable": True})
            return self.fixture_result(operation)
        observer = self.app.task_observer
        with patch.object(self.app.task_admission, "_read", side_effect=read), \
                patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("No replay")), observer.running():
            self.until(lambda: self.app.machines.ssh_recovery.read(self.state)["targets"].get("a" * 64))
            self.assertEqual(len(attempts), 1, "Aliases and tasks share the failed connection")
            self.until(lambda: all((self.raw(identity)["operations"][0]["result"] or {}).get("status") == "succeeded" for identity in identities))
        self.assertEqual(len(attempts), 4)
        for identity in identities:
            record = self.raw(identity)
            self.assertEqual(len(record["operations"]), 1)
            self.assertEqual(record["handoff"]["reason"], "result_ready")
            self.assertNotEqual(record["plan"]["stages"][0]["state"], "accepted")
        self.assertIsNone(self.app.machines.ssh_recovery.read(self.state)["targets"]["a" * 64])

    def test_pause_restart_and_aliases_do_not_reset_connection_budget(self):
        identities = self.remote_tasks(2)
        owner = self.app.machines.ssh_recovery
        attempts = []
        def read(operation, **kwargs):
            episode = owner.read(self.state)["targets"].get("a" * 64)
            attempts.append(None if episode is None else episode["attempts"])
            raise Fault("ssh_unavailable", "Fixture timeout", {"ssh_retryable": True})
        with patch.object(self.app.task_admission, "_read", side_effect=read), patch.object(self.observer_module.random, "uniform", return_value=1):
            with self.app.task_observer.running():
                self.until(lambda: owner.read(self.state)["targets"].get("a" * 64))
                self.until(lambda: self.raw(identities[0])["recovery"])
            first = owner.read(self.state)["targets"]["a" * 64]
            record = self.raw(identities[0])
            self.app.control_task(task_id=identities[0], expected_revision=record["revision"], expected_plan_revision=1,
                expected_epoch=0, update_id=uuid.uuid4().hex, transition={"action": "pause", "reason": "Retain remote work."})
            self.assertEqual(self.raw(identities[0])["recovery"]["episode_id"], first["episode_id"])
            with self.app.task_observer.running():
                self.until(lambda: owner.read(self.state)["targets"]["a" * 64]["attempts"] == 3)
                self.until(lambda: not self.app.task_observer._reads)
            current = owner.read(self.state)["targets"]["a" * 64]
            self.assertEqual(attempts, [None, 2, 3])
            self.assertEqual((current["episode_id"], current["deadline_ms"]), (first["episode_id"], first["deadline_ms"]))
            self.assertGreater(current["next_attempt_ms"] - self.observer_module.now_ms(), 59000)
            self.assertEqual(self.raw(identities[0])["control"]["stop_intent"], "pause")
            self.assertIsNone(self.raw(identities[0])["handoff"])
            restarted = create_application(self.config, self.state)
            with patch.object(restarted.task_admission, "_read", side_effect=AssertionError("Cooldown survives restart")), restarted.task_observer.running():
                self.until(lambda: restarted.task_observer._snapshot.get("connections"))
                for identity in identities:
                    self.assertEqual(restarted.read_task(identity)["observations"][1]["state"], "recovering")
            self.assertEqual(owner.read(self.state)["targets"]["a" * 64], current)

    def test_blocked_ssh_reader_does_not_consume_local_or_other_target_slots(self):
        import threading
        identities = self.remote_tasks(4, routes={2: ("healthy", "b" * 64), 3: ("desktop", None)})
        release, started = threading.Event(), threading.Event()
        calls = []
        def read(operation, **kwargs):
            target = operation["locator"]["target_identity"]
            calls.append(target)
            if target == "a" * 64:
                started.set()
                release.wait(10)
            return self.fixture_result(operation)
        observer = self.app.task_observer
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=read), patch.object(self.observer_module, "SSH_READ_SECONDS", 0.1), observer.running():
                self.assertTrue(started.wait(2))
                self.until(lambda: all((self.raw(identity)["operations"][0]["result"] or {}).get("status") == "succeeded" for identity in identities[2:]))
                self.until(lambda: self.raw(identities[0])["recovery"])
                self.assertEqual(calls.count("a" * 64), 1)
                self.assertTrue(any(item.target == "a" * 64 for item in observer._reads.values()))
                release.set()
                self.until(lambda: not observer._reads)
        finally:
            release.set()
            observer.stop()

    def test_exhausted_permission_and_expired_authority_never_contact_target(self):
        identity = self.remote_tasks()[0]
        owner = self.app.machines.ssh_recovery
        now = self.observer_module.now_ms()
        first = {"episode_id": "e" * 32, "started_at_ms": now, "deadline_ms": now + 3600000,
            "attempts": 60, "next_attempt_ms": now, "reason_code": "ssh_transport_unavailable"}
        owner.replace(self.state, "a" * 64, None, first)
        for episode in (first, dict(first, attempts=1, reason_code="blocked_ssh_host_key_mismatch")):
            owner.replace(self.state, "a" * 64, owner.read(self.state)["targets"]["a" * 64], episode)
            for _ in range(2):
                with patch.object(self.app.task_admission, "_read", side_effect=AssertionError("No forbidden read")), self.app.task_observer.running():
                    self.until(lambda: self.app.task_observer._snapshot["errors"].get(identity))
            self.assertEqual(owner.read(self.state)["targets"]["a" * 64], episode)
        owner.replace(self.state, "a" * 64, episode, None)
        with patch.object(self.app.task_admission, "_read", side_effect=Fault("source_unavailable", "Unclassified remote source fault")) as read, self.app.task_observer.running():
            self.until(lambda: (self.raw(identity)["recovery"] or {}).get("reason_code") == "blocked_source_unavailable")
            self.assertEqual(read.call_count, 1)
        tasks.update(self.state, identity, lambda value, timestamp: value["contract"].update(expires_at_ms=timestamp))
        with patch.object(self.app.task_admission, "_read", side_effect=AssertionError("Expired authority")), self.app.task_observer.running():
            self.until(lambda: self.app.task_observer._snapshot["errors"].get(identity) == "task_authority_expired")

    def test_receipt_failure_does_not_block_connection_or_lose_budget_during_outage(self):
        identities = self.remote_tasks(2)
        def read(operation, **kwargs):
            if operation["id"] == "1".zfill(32):
                raise Fault("execution_not_found", "Original receipt unavailable")
            return self.fixture_result(operation)
        with patch.object(self.app.task_admission, "_read", side_effect=read), self.app.task_observer.running():
            self.until(lambda: self.raw(identities[0])["recovery"])
            self.until(lambda: (self.raw(identities[1])["operations"][0]["result"] or {}).get("status") == "succeeded")
        record = self.raw(identities[0])
        before = dict(record["recovery"])
        connection = {"episode_id": "f" * 32, "started_at_ms": record["updated_at_ms"],
            "deadline_ms": record["updated_at_ms"] + 3600000, "attempts": 1,
            "next_attempt_ms": record["updated_at_ms"] + 2000, "connection_ref": "a" * 64,
            "reason_code": "ssh_transport_unavailable"}
        self.observer_module.fail_observation(record, record["updated_at_ms"], record["updated_at_ms"], "ssh_transport_unavailable", connection)
        self.assertEqual(record["recovery"], before)
        self.assertIsNone(self.app.machines.ssh_recovery.read(self.state)["targets"]["a" * 64])

    def test_missing_connection_history_and_expiry_never_advertise_a_retry(self):
        identity = self.remote_tasks()[0]
        owner = self.app.machines.ssh_recovery
        timestamp = self.observer_module.now_ms()
        episode = {"episode_id": "e" * 32, "started_at_ms": timestamp, "deadline_ms": timestamp + 3600000,
            "attempts": 3, "next_attempt_ms": timestamp + 60000, "reason_code": "ssh_transport_unavailable"}
        owner.replace(self.state, "a" * 64, None, episode)
        def configure(record, now):
            record["contract"]["expires_at_ms"] = timestamp + 10000
            record["recovery"] = dict(episode, category="ssh_observation", connection_ref="a" * 64,
                deadline_ms=timestamp + 10000, next_attempt_ms=timestamp + 10000)
            record["control"]["state"] = "recovering"
        tasks.update(self.state, identity, configure)
        observer = self.app.task_observer
        with patch.object(self.app.task_admission, "_read", side_effect=AssertionError("No allowed retry")), observer.running():
            self.until(lambda: observer._snapshot.get("connections"))
            self.assertIsNone(self.app.read_task(identity)["observations"][1]["next_attempt_at_ms"])
            with patch.object(self.observer_module, "now_ms", return_value=timestamp + 10001):
                runtime = self.app.read_task(identity)["observations"][1]
                self.assertEqual(runtime["blocker"], "task_authority_expired")
                self.assertIsNone(runtime["next_attempt_at_ms"])
        # Even losing both owner files cannot erase a canonical connection reference.
        (self.state / "ssh-recovery.json").unlink()
        (self.state / "locks/ssh-recovery.lock").unlink()
        with patch.object(self.app.task_admission, "_read", side_effect=AssertionError("Missing owner")), observer.running():
            self.until(lambda: observer._snapshot["errors"].get(identity) == "ssh_recovery_corrupt")
        self.assertFalse((self.state / "ssh-recovery.json").exists())
