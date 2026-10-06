"""Focused lifecycle failures; mocked launchctl is not native-service acceptance."""

from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch
import zipfile

from openmachine import app as application, service, tasks
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import maintenance
from openmachine.service import checks as service_checks
from openmachine.service import install as service_install
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from openmachine.service import transition as service_transition


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-m2-service-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = private_directory(self.root / "state")
        private_directory(self.state / "locks")
        private_directory(self.state / "service")
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        self.a, self.b = "a" * 64, "b" * 64
        for identity in (self.a, self.b):
            directory = private_directory(self.state / "service/versions" / identity)
            save_json(directory / "installation.json", {
                "identity": identity, "version": "0.1.0a7", "protocol": 1, "state_contract": 1})
        self.record = {"format": 1, "config": str(self.config), "probe": {"workspace": "project", "path": "AGENTS.md"},
                       "current": self.a, "previous": None, "pending": None,
                       "launch_python": str(Path(sys._base_executable).resolve()), "path_environment": os.defpath}
        self.save(self.record)

    def save(self, record):
        save_json(self.state / "service/service.json", record)

    def assert_fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def retired_events_fixture(self):
        # Original format-1 writer shape with synthetic, never-contacted values.
        import base64
        import hashlib
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        task, principal, url = "retired-task", "native-uid:0", "https://callback.example/retired"
        identity = digest([principal, url, "task.handoff_ready", {"task_id": task}])
        handoff = "a" * 32
        budget = {"started_at_ms": 1000000, "deadline_ms": 4600000, "attempts": 1, "next_attempt_ms": 1001000}
        delivery = {"id": digest([identity, handoff]), "payload": {"task_id": task,
            "handoff_id": handoff, "task_revision": 1, "plan_revision": 1, "reason": "result_ready"},
            "occurred_at_ms": 1000000, "epoch": 1, "budget": budget,
            "accepted_at_ms": None, "read_at_ms": None, "blocker": None}
        subscription = {"id": identity, "generation": "b" * 32, "principal": principal,
            "task_id": task, "url": url, "secret": "whsec_" + base64.b64encode(b"f" * 32).decode(),
            "previous_secret": None, "rotation_until_ms": 0, "verified_at_ms": 1000000,
            "expires_at_ms": 4600000, "delivery": delivery}
        value = {"format": 1, "subscriptions": {identity: subscription},
                 "connections": {"callback.example:443": budget}}
        save_json(self.state / "events.json", value)
        with lease(self.state / "locks/events.lock"):
            pass
        return value, digest(value)

    def test_retired_events_archive_is_passive_and_blocks_reactivation(self):
        value, digest = self.retired_events_fixture()
        path, lock = self.state / "events.json", self.state / "locks/events.lock"
        before, inode, lock_inode = path.read_bytes(), path.stat().st_ino, lock.stat().st_ino
        proof = service.check_state(self.config, self.state)
        self.assertTrue(proof["events_retired"])
        self.assertEqual((proof["events_subscriptions"], proof["events_sha256"]), (1, digest))
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state), proof)
        self.assertNotIn("whsec_", json.dumps(proof))
        self.assertNotIn("callback.example", json.dumps(proof))
        legacy = {key: item for key, item in proof.items() if key != "events_retired"}
        for candidate in (legacy, proof | {"events_retired": False}, proof | {"events_retired": 1},
                          proof | {"events_sha256": "0" * 64}, proof | {"events_present": False}):
            with self.subTest(candidate=candidate), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(candidate), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(
                    Path(sys.executable), self.config, self.state))
        self.assertEqual((path.read_bytes(), path.stat().st_ino, lock.stat().st_ino), (before, inode, lock_inode))

    def test_retired_events_archive_rejects_corrupt_unsafe_or_missing_history(self):
        self.retired_events_fixture()
        path, lock = self.state / "events.json", self.state / "locks/events.lock"
        original = path.read_bytes()
        for raw in (b'{"format":1,"format":1}', b'{}', b'[]', b'\xff', b'x' * 524289,
                    b'{"format":true,"subscriptions":{},"connections":{}}',
                    b'{"format":1,"subscriptions":[],"connections":{}}'):
            with self.subTest(raw_size=len(raw)):
                path.write_bytes(raw)
                self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
                self.assertEqual(path.read_bytes(), raw)
        path.write_bytes(original)
        path.chmod(0o644)
        self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
        path.chmod(0o600)
        target = self.state / "archive-backup"
        path.rename(target)
        path.symlink_to(target)
        self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
        path.unlink()
        self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
        target.rename(path)
        lock.unlink()
        self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
        self.assertFalse(lock.exists())
        self.assertEqual(path.read_bytes(), original)

    def test_retired_archive_busy_lock_is_not_replaced_or_repaired(self):
        self.retired_events_fixture()
        path = self.state / "events.json"
        before = path.read_bytes()
        with lease(self.state / "locks/events.lock"):
            self.assert_fault("resource_busy", lambda: service_checks._events_state(self.state))
        self.assertEqual(path.read_bytes(), before)

    def activation_mocks(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        verify = stack.enter_context(patch.object(service_install, "_verify_candidate"))
        stack.enter_context(patch.object(service_checks, "_candidate_state", return_value={}))
        stop = stack.enter_context(patch.object(service_runtime, "_stop"))
        start = stack.enter_context(patch.object(service_runtime, "_start", return_value={"status": "ready"}))
        return verify, stop, start

    def managed_fixture(self):
        path = self.state / "service/versions" / self.a / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 9})

    def supervisor_mocks(self):
        self.managed_fixture()
        (self.state / "mcp.sock").touch()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stop, clock, child = threading.Event(), [1000.0], Mock(pid=2468)
        child.poll.return_value = None
        stack.enter_context(patch.object(service_runtime.threading, "Event", return_value=stop))
        stack.enter_context(patch.object(service_runtime.time, "time_ns", side_effect=lambda: int(clock[0] * 1e9)))
        stack.enter_context(patch.object(service_runtime.time, "monotonic", side_effect=lambda: clock[0]))
        stack.enter_context(patch.object(service_runtime.random, "uniform", return_value=1))
        stack.enter_context(patch.object(service_runtime, "_verify_files"))
        stack.enter_context(patch.object(service_runtime, "_native", return_value={"loaded": True, "pid": os.getpid()}))
        stack.enter_context(patch.object(service_runtime.os, "getpgrp", return_value=os.getpid()))
        stack.enter_context(patch.object(sys, "prefix", str(service_state._runtime(self.state, self.a))))
        launch = stack.enter_context(patch.object(service_runtime, "_launch_mcp", return_value=child))
        def advance(seconds):
            clock[0] += seconds
            if clock[0] > 2000:
                self.fail("The bounded supervisor did not stop")
        stack.enter_context(patch.object(stop, "wait", side_effect=advance))
        return stop, clock, child, launch

    def test_recovery_accounting_is_durable_bounded_and_reader_verified(self):
        self.assertIsNone(service_state._read_recovery(self.state))
        with patch.object(service_runtime.random, "uniform", return_value=1):
            episode = service_runtime._recovery_attempt(self.state, None, 1000000)
            self.assertEqual(episode["next_attempt_ms"], 1006000)
            original_id = episode["id"]
            self.assert_fault("runtime_recovery_cooldown", lambda: service_runtime._recovery_attempt(self.state, episode, 1005999))
            for attempt in range(2, 16):
                now = episode["next_attempt_ms"]
                episode = service_runtime._recovery_attempt(self.state, service_state._read_recovery(self.state)["episode"], now)
                self.assertEqual((episode["id"], episode["attempts"], episode["deadline_ms"]), (original_id, attempt, 1900000))
                self.assertEqual(episode["next_attempt_ms"] - now, 7000 if attempt == 2 else 65000)
            self.assert_fault("runtime_recovery_blocked", lambda: service_runtime._recovery_attempt(self.state, episode, episode["next_attempt_ms"]))
            self.assert_fault("runtime_recovery_blocked", lambda: service_runtime._recovery_attempt(self.state, dict(episode, attempts=1), 1900000))
        for jitter in (0.8, 1.2):
            with patch.object(service_runtime.random, "uniform", return_value=jitter):
                self.assertEqual([service_runtime._recovery_delay(n) for n in (1, 2, 3, 15)],
                                 [int(base * jitter) for base in (1000, 2000, 60000, 60000)])
        before = (self.state / "service/service.json").read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual(service_checks._candidate_state(
            Path(sys.executable), self.config, self.state, service_state.STATE_CONTRACT), proof)
        for delta in ({"state_contract": 7}, {"runtime_recovery_present": False},
                      {"runtime_recovery_format": True}, {"runtime_recovery_sha256": "e" * 64}):
            with self.subTest(delta=delta), patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(Path(sys.executable), self.config, self.state))
        self.assertEqual((self.state / "service/service.json").read_bytes(), before)

    def test_recovery_corruption_and_missing_history_never_reset(self):
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        path = self.state / "service/service.json"
        for delta in ({"attempts": True}, {"attempts": 16}, {"deadline_ms": 1900001},
                      {"next_attempt_ms": 0}, {"reason": "raw error payload!"}):
            save_json(path, self.record | {"recovery": {"format": 1, "episode": episode | delta}})
            before = path.read_bytes()
            self.assert_fault("runtime_recovery_invalid", lambda: service.check_state(self.config, self.state))
            self.assertEqual(path.read_bytes(), before)
        path.unlink()
        with lease(self.state / "service/recovery.lock"):
            self.assert_fault("runtime_recovery_invalid", lambda: service_state._read_recovery(self.state))

    def test_supervisor_charges_before_effect_and_clears_only_actual_readiness(self):
        stop, clock, child, launch = self.supervisor_mocks()
        service_state._save_recovery(self.state, None)
        attempts, times = [], []
        def launch_after_accounting(*_):
            self.assertEqual(service_state._read_recovery(self.state)["episode"]["attempts"], 1)
            return child
        launch.side_effect = launch_after_accounting
        def probe(*args, **kwargs):
            episode = service_state._read_recovery(self.state)["episode"]
            attempts.append(episode["attempts"])
            times.append(clock[0])
            self.assertEqual(kwargs["child"], child.pid)
            self.assertLessEqual(kwargs["timeout_seconds"], 5)
            with self.assertRaises(Fault) as busy:
                with lease(self.state / "service/readiness.lock"):
                    self.fail("A second readiness probe acquired the occupied slot")
            self.assertEqual(busy.exception.code, "resource_busy")
            if len(attempts) < 3:
                raise Fault("service_unready", "transient fixture")
        with patch.object(service_checks, "_managed_probe", side_effect=probe):
            def advance(seconds):
                clock[0] += seconds
                if service_state._read_recovery(self.state)["episode"] is None:
                    stop.set()
            stop.wait.side_effect = advance
            service.supervise(self.config, self.state)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(times, [1000, 1001, 1003])
        launch.assert_called_once()
        child.terminate.assert_called_once()
        self.assertIsNone(service_state._read_recovery(self.state)["episode"])

    def test_supervisor_exhaustion_is_preserved_across_parent_restart(self):
        _, _, child, launch = self.supervisor_mocks()
        service_state._save_recovery(self.state, None)
        with patch.object(service_checks, "_managed_probe", side_effect=Fault("service_unready", "fixture")) as probe:
            service.supervise(self.config, self.state)
            episode = service_state._read_recovery(self.state)["episode"]
            self.assertEqual((episode["attempts"], episode["reason"]), (15, "blocked_budget_exhausted"))
            self.assertEqual(probe.call_count, 15)
            service.supervise(self.config, self.state)
            self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)
            self.assertEqual(probe.call_count, 15)
        launch.assert_called_once()
        child.terminate.assert_called_once()

    def test_post_ready_hang_replaces_only_owned_child_after_durable_accounting(self):
        stop, clock, child, launch = self.supervisor_mocks()
        service_state._save_recovery(self.state, None)
        replacement = Mock(pid=2469)
        replacement.poll.return_value = None
        launch.side_effect = [child, replacement]
        probes = []
        def probe(*args, **kwargs):
            probes.append((clock[0], kwargs["child"]))
            if len(probes) == 2:
                self.assertIsNone(service_state._read_recovery(self.state)["episode"])
                raise Fault("service_unready", "post-ready hang")
            if len(probes) == 3:
                self.assertEqual(service_state._read_recovery(self.state)["episode"]["attempts"], 2)
        def terminate():
            episode = service_state._read_recovery(self.state)["episode"]
            self.assertEqual((episode["attempts"], episode["reason"]), (1, "service_unready"))
        child.terminate.side_effect = terminate
        child.wait.side_effect = [subprocess.TimeoutExpired("owned child", 5), 0]
        def advance(seconds):
            clock[0] += seconds
            if len(probes) == 3:
                stop.set()
        stop.wait.side_effect = advance
        with patch.object(service_checks, "_managed_probe", side_effect=probe):
            service.supervise(self.config, self.state)
        self.assertEqual(probes, [(1000, 2468), (1030, 2468), (1031, 2469)])
        self.assertEqual(launch.call_count, 2)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_args_list, [call(timeout=5), call(timeout=5)])
        replacement.terminate.assert_called_once()
        self.assertIsNone(service_state._read_recovery(self.state)["episode"])

    def test_healthy_checks_and_busy_slot_never_restart_or_spend_recovery_budget(self):
        stop, clock, child, launch = self.supervisor_mocks()
        service_state._save_recovery(self.state, None)
        times, healthy_record = [], []
        def probe(*args, **kwargs):
            times.append(clock[0])
            if len(times) == 2:
                healthy_record.append((self.state / "service/service.json").read_bytes())
                raise Fault("resource_busy", "another readiness caller")
            if len(times) > 2:
                self.assertEqual((self.state / "service/service.json").read_bytes(), healthy_record[0])
            if len(times) == 4:
                stop.set()
        with patch.object(service_checks, "_managed_probe", side_effect=probe):
            service.supervise(self.config, self.state)
        self.assertEqual(times, [1000, 1030, 1031, 1061])
        launch.assert_called_once()
        child.terminate.assert_called_once()
        self.assertIsNone(service_state._read_recovery(self.state)["episode"])

    def test_post_ready_fatal_identity_failure_blocks_without_replacement(self):
        _, _, child, launch = self.supervisor_mocks()
        service_state._save_recovery(self.state, None)
        with patch.object(service_checks, "_managed_probe", side_effect=[{}, Fault("service_identity_mismatch", "fixture")]):
            service.supervise(self.config, self.state)
        episode = service_state._read_recovery(self.state)["episode"]
        self.assertEqual((episode["attempts"], episode["reason"]), (1, "blocked_service_identity_mismatch"))
        launch.assert_called_once()
        child.terminate.assert_called_once()

    def test_supervisor_fatal_failure_and_accounting_failure_do_not_retry(self):
        _, _, _, launch = self.supervisor_mocks()
        service_state._save_recovery(self.state, None)
        with patch.object(service_checks, "_managed_probe", side_effect=Fault("service_identity_mismatch", "fixture")) as probe:
            service.supervise(self.config, self.state)
        episode = service_state._read_recovery(self.state)["episode"]
        self.assertEqual((episode["attempts"], episode["reason"]), (1, "blocked_service_identity_mismatch"))
        probe.assert_called_once()
        launch.reset_mock()
        service_state._save_recovery(self.state, None)
        self.config.write_text("invalid TOML =")
        service.supervise(self.config, self.state)
        episode = service_state._read_recovery(self.state)["episode"]
        self.assertEqual((episode["attempts"], episode["reason"]), (1, "blocked_invalid_config"))
        launch.assert_not_called()
        service_state._save_recovery(self.state, None)
        with patch.object(service_state, "_save_recovery", side_effect=Fault("runtime_recovery_unverified", "fixture")):
            service.supervise(self.config, self.state)
        launch.assert_not_called()

    def test_stop_during_original_cooldown_starts_no_child_and_keeps_budget(self):
        stop, _, _, launch = self.supervisor_mocks()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        stop.wait.side_effect = lambda _: stop.set()
        service.supervise(self.config, self.state)
        launch.assert_not_called()
        self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)

    def test_status_does_not_probe_or_renew_an_active_recovery_episode(self):
        self.managed_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        with patch.object(service_runtime, "_verify_files"), patch.object(service_runtime, "_native", return_value={"loaded": True, "pid": 123}), \
                patch.object(service_checks, "_managed_probe") as probe:
            status = service.status(self.config, self.state)
            self.assertEqual((status["status"], status["pid"], status["supervisor_pid"]), ("recovering", None, 123))
            self.assertEqual(status["recovery"], episode)
            service_state._save_recovery(self.state, dict(episode, reason="blocked_budget_exhausted"))
            self.assertEqual(service.status(self.config, self.state)["status"], "blocked")
            probe.assert_not_called()

    def test_only_explicit_restart_renews_budget_after_verified_stop(self):
        self.managed_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        with patch.object(sys, "platform", "darwin"), patch.object(service_runtime, "_verify_files"), \
                patch.object(service_checks, "_candidate_state"), patch.object(service_runtime, "_start", return_value={"status": "ready"}), \
                patch.object(service_runtime, "_stop") as stop:
            for action in ("start", "restart", "stop"):
                service.operate(self.config, self.state, action)
                self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)
            stop.side_effect = Fault("service_stop_unverified", "fixture")
            self.assert_fault("service_stop_unverified", lambda: service.operate(self.config, self.state, "restart", reset_recovery=True))
            self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)
            stop.side_effect = None
            service.operate(self.config, self.state, "restart", reset_recovery=True)
            self.assertIsNone(service_state._read_recovery(self.state)["episode"])
            save_json(self.state / "service/service.json", self.record | {"recovery": {"format": 999}})
            self.assert_fault("runtime_recovery_invalid", lambda: service.operate(self.config, self.state, "restart", reset_recovery=True))

    def test_managed_probe_binds_child_parent_and_overall_deadline(self):
        with patch.object(service_checks, "probe_service", return_value={"status": "ready", "pid": 123}) as probe, \
                patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, "456\n", "")):
            self.assertEqual(service_checks._managed_probe(self.state, self.record, 456)["supervisor_pid"], 456)
            self.assertEqual(probe.call_args.kwargs["timeout_seconds"], 5)
            self.assert_fault("service_identity_mismatch", lambda: service_checks._managed_probe(self.state, self.record, 999))
            with patch.object(service_runtime.time, "monotonic", side_effect=[100, 105]):
                self.assert_fault("service_unready", lambda: service_checks._managed_probe(self.state, self.record, 456))

    def test_first_upgrade_preserves_old_state_until_readiness_and_restores_on_failure(self):
        self.activation_mocks()
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 9})
        def start(state, record):
            self.assertIsNone(service_state._read_recovery(state))
            if record["current"] == self.b:
                raise Fault("service_unready", "fixture")
            return {"status": "ready"}
        with patch.object(service_runtime, "_start", side_effect=start), patch.object(service_runtime, "_sync_definition") as sync:
            self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, self.record, self.b))
            sync.assert_called_once()
        self.assertIsNone(service_state._read_recovery(self.state))
        self.assertTrue(service_runtime._definition(self.state, self.record)["AbandonProcessGroup"])
        with patch.object(service_runtime, "_start", return_value={"status": "ready"}):
            service_transition._activate(self.state, self.record, self.b)
        self.assertEqual(service_state._read_recovery(self.state), {"format": 1, "episode": None})
        self.assertFalse(service_runtime._definition(self.state, service_state._record(self.state))["AbandonProcessGroup"])

    def test_first_upgrade_commit_crash_leaves_rollback_or_committed_selection(self):
        self.activation_mocks()
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 9})
        for committed in (False, True):
            self.save(self.record)
            def crash_at_commit(path, value):
                if path == self.state / "service/service.json" and value["current"] == self.b and value["pending"] is None:
                    self.assertEqual(value["recovery"], {"format": 1, "episode": None})
                    if committed:
                        save_json(path, value)
                    raise OSError("Injected controller loss at atomic commit")
                save_json(path, value)
            with self.subTest(committed=committed), patch.object(service_transition, "save_json", side_effect=crash_at_commit):
                with self.assertRaises(OSError):
                    service_transition._activate(self.state, self.record, self.b)
            observed = service_state._record(self.state)
            if committed:
                self.assertIsNone(observed["pending"])
                self.assertEqual(observed["current"], self.b)
                self.assertEqual(service_state._read_recovery(self.state), {"format": 1, "episode": None})
            else:
                self.assertEqual(observed["pending"]["from"], self.a)
                self.assertIsNone(service_state._read_recovery(self.state))
                import plistlib
                owned_plist = self.root / "owned.plist"
                for owned, content in ((owned_plist, plistlib.dumps(service_runtime._definition(self.state, observed))),
                                       (self.state / "service/launch.py", service_state.LAUNCHER.encode()),
                                       (self.state / "service/service.log", b"")):
                    owned.write_bytes(content)
                    owned.chmod(0o600)
                def restore(state, selection):
                    service_runtime._verify_files(state, selection)
                    return {"status": "ready"}
                with patch.object(service_runtime, "_plist_path", return_value=owned_plist), patch.object(service_runtime, "_start", side_effect=restore):
                    service_transition._activate(self.state, observed, self.a)
                self.assertEqual(service_state._record(self.state)["current"], self.a)
                self.assertIsNone(service_state._read_recovery(self.state))

    def test_activation_preserves_recovery_advanced_during_candidate_preflight(self):
        verify, _, start = self.activation_mocks()
        self.managed_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        later = dict(episode, attempts=2)
        verify.side_effect = lambda *_args, **_kwargs: service_state._save_recovery(self.state, later)
        start.side_effect = Fault("service_unready", "fixture")
        self.assert_fault("recovery_required", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._read_recovery(self.state)["episode"], later)

    def test_state_check_uses_actual_task_reader_and_preserves_bytes(self):
        tasks.save(self.state, "original", 0, "Original task", "Preserve original execution evidence.\n", [])
        paths = [self.state / "tasks/index.json", self.state / "tasks/original/task.md"]
        before = [path.read_bytes() for path in paths]
        result = service.check_state(self.config, self.state)
        self.assertEqual(result["task_records"], 1)
        self.assertEqual([path.read_bytes() for path in paths], before)
        value = json.loads(paths[0].read_text())
        value["format_version"] = 999
        save_json(paths[0], value)
        self.assert_fault("unsupported_task_format", lambda: service.check_state(self.config, self.state))
        self.assertEqual(json.loads(paths[0].read_text())["format_version"], 999)

    def test_actual_task_reader_covers_terminal_history_and_rejects_partial_candidate_proof(self):
        from support_tasks import task_contract, terminal_task_fixture
        from openmachine.tasks import records
        request = task_contract()
        created = tasks.create(self.state, request.pop("task_id"), **request)
        path = self.state / "tasks/structured-one/task.json"
        path.write_bytes(records.canonical(terminal_task_fixture(created["record"])))
        tasks.rebuild_index(self.state)
        raw = path.read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["task_records"], proof["structured_task_records"]), (15, 1, 1))
        actual = service_checks._candidate_state(Path(sys.executable), self.config, self.state, 15)
        self.assertEqual(actual, proof)
        for change in ({"state_contract": 14}, {"structured_task_formats_present": [1, 2]},
                       {"structured_task_read_formats": [1, 2]}, {"structured_task_records": 0}, {"task_records": True},
                       {"task_store_present": 1}, {"structured_task_read_formats": [True]},
                       {"structured_task_read_formats": [1, 1]}, {"task_index_coverage": "incomplete"},
                       {"task_index_generation": proof["task_index_generation"] - 1}):
            with self.subTest(change=change), patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(proof | change), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(Path(sys.executable), self.config, self.state))
        self.assertEqual(path.read_bytes(), raw)
        path.unlink()
        self.assert_fault("corrupt_tasks", lambda: service.check_state(self.config, self.state))

    def test_candidate_ignoring_canonical_tasks_is_rejected_before_healthy_service_stop(self):
        from support_tasks import task_contract
        request = task_contract()
        tasks.create(self.state, request.pop("task_id"), **request)
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 6})
        ignored = subprocess.CompletedProcess([], 0, json.dumps({"protocol": 1, "state_contract": 6}), "")
        original = (self.state / "tasks/structured-one/task.json").read_bytes()
        with patch.object(service_install, "_verify_candidate"), patch.object(service_checks, "_run", return_value=ignored), \
                patch.object(service_runtime, "_stop") as stop, patch.object(service_runtime, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service_transition._activate(self.state, self.record, self.b))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)
        self.assertEqual((self.state / "tasks/structured-one/task.json").read_bytes(), original)

    def test_retained_ssh_budget_requires_actual_reader_before_service_stop(self):
        from integrations.ssh import recovery
        recovery.replace(self.state, "c" * 64, None, {"episode_id": "d" * 32,
            "started_at_ms": 1, "deadline_ms": 3600001, "attempts": 60,
            "next_attempt_ms": 3600001, "reason_code": "ssh_transport_unavailable"})
        before = (self.state / "ssh-recovery.json").read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["ssh_recovery_targets"]), (15, 1))
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state, 15), proof)
        for delta in ({"state_contract": 6}, {"ssh_recovery_present": False},
                      {"ssh_recovery_targets": 0}, {"ssh_recovery_targets": True},
                      {"ssh_recovery_sha256": "e" * 64}, {"ssh_recovery_format": 2}):
            with self.subTest(delta=delta), patch.object(service_install, "_verify_candidate"), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")), \
                    patch.object(service_runtime, "_stop") as stop:
                self.assert_fault("incompatible_state", lambda: service_transition._activate(self.state, self.record, self.b))
                stop.assert_not_called()
        self.assertEqual((self.state / "ssh-recovery.json").read_bytes(), before)

    def test_retained_chat_recovery_requires_actual_reader_before_service_stop(self):
        from integrations import chatgpt
        chatgpt.register_preparation(self.state, task_id="structured-one", preparation_id="a" * 32,
            acknowledged=False, controller_sha256="b" * 64,
            machine="local", workspace="project", workspace_identity="c" * 64,
            authority_ref="fixture:authority", authority_sha256="d" * 64,
            expires_at_ms=chatgpt.now_ms() + 3600000)
        before = (self.state / "chat_recovery.json").read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["chat_recovery_bindings"]), (15, 1))
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state, 15), proof)
        for delta in ({"state_contract": 14}, {"chat_recovery_present": False},
                      {"chat_recovery_bindings": 0}, {"chat_recovery_bindings": True},
                      {"chat_recovery_sha256": "e" * 64}, {"chat_recovery_format": 1}):
            with self.subTest(delta=delta), patch.object(service_install, "_verify_candidate"), patch.object(
                    service_checks, "_run", return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(proof | delta), "")), patch.object(service_runtime, "_stop") as stop:
                self.assert_fault("incompatible_state", lambda: service_transition._activate(
                    self.state, self.record, self.b))
                stop.assert_not_called()
        self.assertEqual((self.state / "chat_recovery.json").read_bytes(), before)

    def test_task_creation_and_index_maintenance_share_lifecycle_admission(self):
        from support_tasks import task_contract
        config = Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}}})
        app = create_application(config, self.state)
        self.record["pending"] = {"from": self.a, "to": self.b, "previous": None}
        self.save(self.record)
        with patch.object(tasks, "create") as create, patch.object(tasks, "rebuild_index") as rebuild:
            self.assert_fault("activation_pending", lambda: app.create_task(**task_contract()))
            self.assert_fault("activation_pending", app.rebuild_task_index)
            create.assert_not_called()
            rebuild.assert_not_called()
        self.assertFalse((self.state / "tasks").exists())

    def test_incompatible_receipt_never_becomes_empty_history(self):
        directory = private_directory(self.state / "executions" / ("c" * 32))
        save_json(directory / "record.json", {"status": "running", "format_version": 999})
        self.assert_fault("unsupported_state_version", lambda: service.check_state(self.config, self.state))
        self.assertTrue((directory / "record.json").exists())

    def test_candidate_state_rejects_failure_or_protocol_drift(self):
        for value, code in (({"error": "unsupported_task_format"}, 1),
                            ({"protocol": 2, "state_contract": 1}, 0)):
            with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], code, json.dumps(value), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(Path(sys.executable), self.config, self.state))

    def test_pending_and_wrong_runtime_block_new_writes(self):
        self.record["pending"] = {"from": self.a, "to": self.b, "previous": None}
        self.save(self.record)
        self.assert_fault("activation_pending", lambda: service.guard_writes(self.state))
        self.record["pending"] = None
        self.save(self.record)
        self.assert_fault("service_identity_mismatch", lambda: service.guard_writes(self.state))
        with patch.object(sys, "prefix", str(service_state._runtime(self.state, self.a))):
            service.guard_writes(self.state)

    def test_application_mutations_share_admission_and_preserve_dispatch_arguments(self):
        import inspect
        config = Settings.model_validate({"schema_version": 1,
            "machines": {"local": {"transport": "local", "execution_enabled": True}},
            "github": {"repositories": ["example/project"]}})
        app = create_application(config, self.state)
        issue = ("update", "example/project", "Title", "Body", 1, "closed", "observed-time")
        pull = ("update", "example/project", "Title", "Body", 1, "topic", "main", False,
                "closed", "observed-time", "f" * 40)
        cases = [
            ("apply_workspace_patch", ("local", "project", "patch", "a" * 64, "", [], False, "c" * 32, False, ""),
             app.machines, "apply_workspace_patch"),
            ("edit_workspace_file", ("local", "project", "file", [{"old_text": "a", "new_text": "b"}],
                                     "a" * 64, "", "b" * 64, "c" * 32, False, ""), app.machines, "edit_workspace_file"),
            ("write_workspace_file", ("local", "project", "file", "text", "", "", "", ""), app.machines, "write_workspace_file"),
            ("start_execution", ("local", "c" * 32, str(self.root), ["true"], 600, "", "exclusive", None, "", None, False, None),
             app.machines, "start_execution"),
            ("cancel_execution", ("local", "c" * 32), app.machines, "cancel_execution"),
            ("control_execution", ("local", "c" * 32, 0, "interrupt", None, None), app.machines, "control_execution"),
            ("save_task", ("task", 0, "Title", "Body", []), tasks, "save"),
            ("github_issue", issue, app.github, "issue"),
            ("github_pull_request", pull, app.github, "pull_request"),
            ("github_comment", ("create", "example/project", "Body", 1, 0, ""), app.github, "comment"),
            ("github_review", ("create", "example/project", "Body", 1, 0, "f" * 40, "COMMENT"), app.github, "review"),
            ("github_merge", ("example/project", 1, "f" * 40, "e" * 40, "squash", "Title", "Body"), app.github, "merge"),
        ]
        for name, arguments, owner, method in cases:
            operation = getattr(app, name)
            signature = inspect.signature(getattr(owner, method))
            with self.subTest(operation=name), patch.object(owner, method) as dispatch, patch.object(app.connections, "request") as request:
                self.record["pending"] = {"from": self.a, "to": self.b, "previous": None}
                self.save(self.record)
                self.assert_fault("activation_pending", lambda: operation(*arguments))
                self.record["pending"] = None
                self.save(self.record)
                self.assert_fault("service_identity_mismatch", lambda: operation(*arguments))
                with lease(self.state / "locks/lifecycle.lock"):
                    self.assert_fault("resource_busy", lambda: operation(*arguments))
                    self.assertEqual(app.find_tasks()["status"], "uninitialized")
                    self.assertEqual(app.get_capabilities()["lifecycle"]["state_contract"], 15)
                dispatch.assert_not_called()

                def admitted(*args, **kwargs):
                    with self.assertRaises(Fault) as caught:
                        with lease(self.state / "locks/lifecycle.lock"):
                            self.fail("Admission lease was released before dispatch")
                    self.assertEqual(caught.exception.code, "resource_busy")
                    return {"dispatched": True}

                dispatch.side_effect = admitted
                with patch.object(sys, "prefix", str(service_state._runtime(self.state, self.a))), \
                        patch.object(application, "lease", wraps=lease) as admission:
                    self.assertEqual(operation(*arguments), {"dispatched": True})
                    admission.assert_called_once_with(self.state / "locks/lifecycle.lock", shared=True)
                prefix = (config,) if name.startswith("github_") else (self.state,) if name == "save_task" else ()
                self.assertEqual(dispatch.call_count, 1)
                actual = signature.bind(*dispatch.call_args.args, **dispatch.call_args.kwargs).arguments
                if name.startswith("github_"):
                    self.assertIs(actual.pop("transport"), request.return_value)
                    request.assert_called_once_with("example/project", connection_id="", binding=None, write=True, legacy=False)
                else:
                    request.assert_not_called()
                self.assertEqual(actual, signature.bind(*prefix, *arguments).arguments)
                with lease(self.state / "locks/lifecycle.lock"):
                    pass

    def test_successful_activation_retains_rollback_target(self):
        verify, stop, start = self.activation_mocks()
        result = service_transition._activate(self.state, self.record, self.b)
        self.assertEqual((result["current"], result["previous"]), (self.b, self.a))
        self.assertIsNone(service_state._record(self.state)["pending"])
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(start.call_count, 1)
        verify.assert_called_once()
        self.assertTrue(service_state._runtime(self.state, self.a).is_dir())

    def test_bad_candidate_does_not_stop_or_select_over_healthy_version(self):
        verify, stop, start = self.activation_mocks()
        verify.side_effect = Fault("candidate_unready", "fixture")
        self.assert_fault("candidate_unready", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._record(self.state), self.record)
        stop.assert_not_called()
        start.assert_not_called()

    def test_failed_activation_restores_original_service_and_returns_failure(self):
        _, stop, start = self.activation_mocks()
        start.side_effect = [Fault("service_read_unavailable", "fixture"), {"status": "ready"}]
        self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._record(self.state), self.record)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual([call.args[1]["current"] for call in start.call_args_list], [self.b, self.a])

    def test_failed_restoration_retains_exact_recovery_intent(self):
        _, _, start = self.activation_mocks()
        start.side_effect = Fault("service_unready", "fixture")
        self.assert_fault("recovery_required", lambda: service_transition._activate(self.state, self.record, self.b))
        observed = service_state._record(self.state)
        self.assertEqual(observed["current"], self.a)
        self.assertEqual(observed["pending"], {"from": self.a, "to": self.b, "previous": None})
        self.assert_fault("activation_pending", lambda: service.guard_writes(self.state))

    def test_interrupted_selection_recovers_original_not_unverified_candidate(self):
        self.activation_mocks()
        pending = {**self.record, "current": self.b,
                   "pending": {"from": self.a, "to": self.b, "previous": None}}
        self.save(pending)
        result = service_transition._activate(self.state, pending, self.a)
        self.assertEqual(result["current"], self.a)
        self.assertIsNone(result["previous"])
        self.assertIsNone(service_state._record(self.state)["pending"])

    def test_activation_lock_conflict_preserves_selection(self):
        _, stop, start = self.activation_mocks()
        with lease(self.state / "locks/lifecycle.lock", shared=True):
            self.assert_fault("resource_busy", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._record(self.state), self.record)
        stop.assert_not_called()
        start.assert_not_called()

    def test_user_service_definition_preserves_workers_and_owns_no_tunnel(self):
        definition = service_runtime._definition(self.state, self.record)
        self.assertTrue(definition["AbandonProcessGroup"])
        self.assertTrue(definition["RunAtLoad"])
        self.assertEqual(definition["EnvironmentVariables"], {"PATH": os.defpath})
        self.assertNotIn("tunnel", json.dumps(definition).lower())
        self.assertEqual(definition["ProgramArguments"][0], self.record["launch_python"])
        self.assertNotEqual(service_runtime._label(self.state), service_runtime._label(self.root / "other-state"))

    def test_missing_and_unverifiable_native_states_are_distinct(self):
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 3, "", "Could not find service fixture")):
            self.assertFalse(service_runtime._native(self.state)["loaded"])
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 1, "", "Unknown GUI domain")):
            self.assert_fault("user_service_unavailable", lambda: service_runtime._native(self.state))
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, "path = /other/job.plist\npid = 123\n", "")):
            self.assert_fault("service_identity_mismatch", lambda: service_runtime._native(self.state))

    def test_invalid_lock_is_rejected_before_package_commands(self):
        wheel = self.root / "openmachine-0.1.0a7-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("openmachine-0.1.0a7.dist-info/METADATA", "Name: openmachine\nVersion: 0.1.0a7\n")
        requirements = self.root / "requirements.lock"
        requirements.write_text("mcp>=1\n")
        with patch.object(service_checks, "_run") as run:
            self.assert_fault("invalid_installation_artifact", lambda: service_install._prepare(self.state, wheel, requirements, self.config))
            run.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)

    def test_corrupt_selection_is_not_replaced(self):
        value = {**self.record, "current": "../other"}
        self.save(value)
        self.assert_fault("service_state_invalid", lambda: service_state._record(self.state))
        self.assertEqual(json.loads((self.state / "service/service.json").read_text()), value)

    def test_missing_selection_with_launcher_is_not_an_empty_installation(self):
        (self.state / "service/service.json").unlink()
        (self.state / "service/launch.py").write_text(service_state.LAUNCHER)
        self.assert_fault("service_state_invalid", lambda: service.status(self.config, self.state))

    def test_incomplete_installation_metadata_rejects_missing_version(self):
        path = self.state / "service/versions" / self.a / "installation.json"
        value = service_state._json(path)
        del value["version"]
        save_json(path, value)
        self.assert_fault("incompatible_runtime", lambda: service_state._installed(self.state, self.a))

    def test_restart_rejects_incompatible_state_before_stopping_service(self):
        with patch.object(sys, "platform", "darwin"), patch.object(service_runtime, "_verify_files"), \
                patch.object(service_checks, "_candidate_state", side_effect=Fault("incompatible_state", "fixture")), \
                patch.object(service_runtime, "_stop") as stop, patch.object(service_runtime, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service.operate(self.config, self.state, "restart"))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)

    def test_directional_metadata_and_candidate_contract_are_explicit(self):
        path = self.state / "service/versions" / self.b / "installation.json"
        original = service_state._json(path)
        for contract in range(1, service_state.STATE_CONTRACT + 1):
            save_json(path, original | {"state_contract": contract})
            self.assertEqual(service_state._installed(self.state, self.b)["state_contract"], contract)
            reply = {"protocol": 1, "state_contract": contract}
            with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")):
                self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state, contract), reply)
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(
                    Path(sys.executable), self.config, self.state, 1 if contract != 1 else 3))
        for contract in (0, service_state.STATE_CONTRACT + 1, True, "3"):
            save_json(path, original | {"state_contract": contract})
            self.assert_fault("incompatible_runtime", lambda: service_state._installed(self.state, self.b))

    def test_explicit_configuration_survives_update_and_exact_rollback(self):
        self.activation_mocks()
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# explicitly selected candidate configuration\n")
        service_transition._activate(self.state, self.record, self.b, candidate)
        self.assertEqual(self.config.read_bytes(), candidate.read_bytes())
        selected = service_state._record(self.state)
        self.assertEqual(set(selected["configurations"]), {self.a, self.b})
        service_transition._activate(self.state, selected, self.a)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service_state._record(self.state)["current"], self.a)

    def test_failed_activation_restores_exact_configuration_bytes(self):
        _, stop, start = self.activation_mocks()
        start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# candidate configuration\n")
        self.assert_fault("activation_failed_restored", lambda: service_transition._activate(
            self.state, self.record, self.b, candidate))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service_state._record(self.state), self.record)
        self.assertEqual(stop.call_count, 2)

    def test_changed_configuration_is_preserved_during_uncertain_recovery(self):
        _, stop, start = self.activation_mocks()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(self.config.read_bytes() + b"# candidate\n")
        manual = self.config.read_bytes() + b"# independent user edit\n"
        def fail_with_edit(*_):
            self.config.write_bytes(manual)
            raise Fault("service_unready", "fixture")
        start.side_effect = fail_with_edit
        self.assert_fault("recovery_required", lambda: service_transition._activate(
            self.state, self.record, self.b, candidate))
        self.assertEqual(self.config.read_bytes(), manual)
        self.assertIsNotNone(service_state._record(self.state)["pending"])
        self.assertEqual(stop.call_count, 1)

    def test_remote_candidate_must_validate_retained_receipts_before_stop(self):
        verify, stop, start = self.activation_mocks()
        candidate = self.root / "candidate.toml"
        candidate.write_text(self.config.read_text() + '\n[machines.remote]\ntransport="ssh"\n'
                             'ssh_alias="uncontacted-target"\nssh_worker="/remote/' + 'a' * 64 + '.pyz"\n')
        def reject_remote(*args, **kwargs):
            if kwargs.get("check_remote"):
                raise Fault("incompatible_remote_state", "Old candidate cannot read v4 receipts")
        verify.side_effect = reject_remote
        original = self.config.read_bytes()
        self.assert_fault("incompatible_remote_state", lambda: service_transition._activate(
            self.state, self.record, self.b, candidate))
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service_state._record(self.state), self.record)

    def test_failed_update_restores_current_bytes_not_a_stale_snapshot(self):
        _, _, start = self.activation_mocks()
        service_transition._activate(self.state, self.record, self.b)
        current = service_state._record(self.state)
        edited = self.config.read_bytes() + b"# valid edit since the previous activation\n"
        self.config.write_bytes(edited)
        start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
        self.assert_fault("activation_failed_restored", lambda: service_transition._activate(
            self.state, current, self.a, self.config))
        self.assertEqual(self.config.read_bytes(), edited)
        restored = service_state._record(self.state)
        saved = service_state._configuration_for(self.state, restored, self.b)
        self.assertEqual(saved.read_bytes(), edited)

    def test_unrelated_retained_configuration_is_not_an_allowed_racing_edit(self):
        _, _, start = self.activation_mocks()
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# first explicit configuration\n")
        service_transition._activate(self.state, self.record, self.b, candidate)
        current = service_state._record(self.state)
        third = "c" * 64
        directory = private_directory(self.state / "service/versions" / third)
        save_json(directory / "installation.json", {
            "identity": third, "version": "0.1.0a7", "protocol": 1, "state_contract": 1})
        candidate.write_bytes(original + b"# next explicit configuration\n")
        def independently_restore_older_bytes(*_):
            self.config.write_bytes(original)
            raise Fault("service_unready", "fixture")
        start.side_effect = independently_restore_older_bytes
        self.assert_fault("recovery_required", lambda: service_transition._activate(
            self.state, current, third, candidate))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIsNotNone(service_state._record(self.state)["pending"])

    def test_corrupt_configuration_snapshot_cannot_be_selected(self):
        self.activation_mocks()
        service_transition._activate(self.state, self.record, self.b)
        selected = service_state._record(self.state)
        digest = selected["configurations"][self.a]
        path = service_state._saved_configuration(self.state, digest)
        path.write_text("corrupt")
        self.assert_fault("configuration_unavailable", lambda: service_transition._activate(self.state, selected, self.a))
        self.assertEqual(service_state._record(self.state), selected)


    def test_remote_workspace_reader_uses_exact_candidate_and_validates_complete_proof(self):
        pin, archive = "d" * 64, "e" * 64
        with self.config.open("a") as file:
            file.write('\n[machines.remote]\ntransport="ssh"\nssh_alias="fixture"\nssh_identity="' + pin +
                       '"\nssh_worker="/target/' + archive + '.pyz"\n')
        proof = {"identity_verified": True, "target_identity": pin, "worker_sha256": archive,
                 "workspace_change_format": 1, "workspace_change_records": 1,
                 "workspace_change_store_present": True, "legacy_workspace_compatible": True,
                 "unavailable_workspaces": []}
        python = self.root / "candidate/bin/python"
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof), "")) as call:
            self.assertEqual(service_checks._remote_workspace_state(python, self.config, "remote"), proof)
            self.assertEqual(call.call_args.args[0], [str(python), "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                                       "doctor", "--machine", "remote"])
        for delta in ({"workspace_change_format": True}, {"workspace_change_store_present": False},
                      {"target_identity": "f" * 64}, {"legacy_workspace_compatible": None}):
            with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_remote_state", lambda: service_checks._remote_workspace_state(python, self.config, "remote"))
        # Missing unrelated source is not unreadable target history or a reason to disrupt a healthy service.
        missing = proof | {"unavailable_workspaces": ["unrelated"]}
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 1, json.dumps(missing), "")):
            self.assertEqual(service_checks._remote_workspace_state(python, self.config, "remote"), missing)

    def test_retained_remote_workspace_state_blocks_old_selection_before_stop(self):
        pin, archive = "d" * 64, "e" * 64
        with self.config.open("a") as file:
            file.write('\n[machines.remote]\ntransport="ssh"\nssh_alias="fixture"\nssh_identity="' + pin +
                       '"\nssh_worker="/target/' + archive + '.pyz"\n')
        installation = self.state / "service/versions" / self.a / "installation.json"
        save_json(installation, service_state._json(installation) | {"state_contract": 4})
        _, stop, start = self.activation_mocks()
        for present, compatible in ((True, True), (False, False)):
            proof = {"workspace_change_store_present": present, "legacy_workspace_compatible": compatible}
            with patch.object(service_checks, "_remote_workspace_state", return_value=proof) as read:
                self.assert_fault("incompatible_remote_state", lambda: service_transition._activate(self.state, self.record, self.b))
                self.assertEqual(read.call_args.args, (service_state._runtime(self.state, self.a) / "bin/python", self.config, "remote"))
            self.assertEqual(service_state._record(self.state), self.record)
            stop.assert_not_called()
            start.assert_not_called()

    def test_actual_local_change_reader_blocks_old_candidate_that_ignores_store(self):
        from openmachine import workspace
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\nexecution_enabled=true\n'
                               '[workspaces.project]\nmachine="local"\nroot=' + json.dumps(str(self.root)) + '\n')
        config = service_checks.load(self.config)
        file = self.root / "source.py"
        file.write_text("answer = 41")
        read = workspace.read_text_file(config, "local", "project", "source.py")
        changed = workspace.edit_text_file(config, self.state, "local", "project", "source.py",
            [{"old_text": "41", "new_text": "42"}], read["context"]["workspace_identity"], "", read["sha256"], "d" * 32)
        self.assertEqual(changed["status"], "applied")
        record = self.state / "workspace-changes" / ("d" * 32) / "record.json"
        retained = record.read_bytes()
        ignored = subprocess.CompletedProcess([], 0, json.dumps({"protocol": 1, "state_contract": 1}), "")
        with patch.object(service_install, "_verify_candidate"), patch.object(service_checks, "_run", return_value=ignored), \
             patch.object(service_runtime, "_stop") as stop, patch.object(service_runtime, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service_transition._activate(self.state, self.record, self.b))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(record.read_bytes(), retained)
        self.assertEqual(service_state._record(self.state), self.record)


    def test_change_format_coverage_is_directional_and_requires_actual_readers(self):
        from openmachine import workspace
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\nworkspace_write_enabled=true\n'
                               '[workspaces.project]\nmachine="local"\nroot='+json.dumps(str(self.root))+'\n')
        config = service_checks.load(self.config)
        file = self.root / "source.py"
        file.write_text("a\n")
        read = workspace.read_text_file(config,"local","project","source.py")
        first = workspace.edit_text_file(config,self.state,"local","project","source.py",
            [{"old_text":"a","new_text":"b"}],read["context"]["workspace_identity"],"",read["sha256"],"1"*32)
        self.assertEqual(first["format"],1)
        legacy = {"protocol":1,"state_contract":4,"workspace_change_format":1,
                  "workspace_change_store_present":True,"workspace_change_records":1}
        with patch.object(service_checks,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(legacy),"")):
            self.assertEqual(service_checks._candidate_state(Path(sys.executable),self.config,self.state),legacy)
        second = workspace.apply_patch(config,self.state,"local","project",
            "*** Begin Patch\n*** Add File: new\n+x\n*** End Patch\n",read["context"]["workspace_identity"],"",
            [{"path":"new","expected_sha256":""}],change_id="2"*32)
        self.assertEqual(second["format"],2)
        legacy["workspace_change_records"] = 2
        with patch.object(service_checks,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(legacy),"")):
            self.assert_fault("incompatible_state",lambda:service_checks._candidate_state(Path(sys.executable),self.config,self.state))
        current = service.check_state(self.config,self.state)
        self.assertEqual(current["workspace_change_formats_present"],[1,2])
        records = list((self.state / "workspace-changes").glob("*/record.json"))
        before = [path.read_bytes() for path in records]
        with patch.object(service_checks,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(current),"")):
            service_checks._candidate_state(Path(sys.executable),self.config,self.state)
        self.assertEqual([path.read_bytes() for path in records],before)

    def policy_fixture(self):
        binding = {"identity": self.a, "launch_python": str(Path(sys._base_executable).resolve())}
        record = self.record | {"format": 2, "management": binding,
                                "policy": {"desired": "stopped", "start_at_login": True}}
        for identity in (self.a, self.b):
            path = self.state / "service/versions" / identity / "installation.json"
            save_json(path, service_state._json(path) | {"state_contract": 14, "management_contract": 1})
        self.save(record)
        return record

    def operation_fixture(self, phase="prepared"):
        import hashlib
        private_directory(self.state / "service/operations")
        digest = hashlib.sha256(self.config.read_bytes()).hexdigest()
        record = {"format": 1, "operation_id": "c" * 32, "created_at_ms": 1000, "updated_at_ms": 1000,
                  "phase": phase, "reason": "", "outcome": None,
                  "request": {"action": "update", "config": str(self.config), "source": str(self.root / "payload"),
                              "candidate_config": str(self.config), "payload_sha256": "d" * 64,
                              "config_sha256": digest, "candidate_sha256": digest,
                              "selection_sha256": maintenance._selection(self.state), "workspace": "", "path": ""},
                  "prepared": {"identity": self.b, "payload_sha256": "d" * 64,
                               "runtime": str(self.state / "service/versions" / self.b),
                               "status": "prepared", "version": "0.1.0a7", "launch_python": sys.executable,
                               "path_environment": os.defpath, "selection": "unchanged", "service": "not_started"}}
        if phase in {"accepted", "retaining", "retained", "preparing"}:
            record["prepared"] = None
        if phase == "completed":
            record["outcome"] = {"status": "ready"}
        maintenance._save(self.state, record, phase)
        with lease(self.state / "service/operation.lock"):
            pass
        return maintenance._read(self.state, record["operation_id"])

    def test_persistent_policy_requires_actual_new_reader_and_blocks_old_selection_format(self):
        record = self.policy_fixture()
        proof = service.check_state(self.config, self.state)
        self.assertTrue(proof["service_policy_present"])
        self.assertEqual(proof["management_contract"], 1)
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state), proof)
        for delta in ({"state_contract": 13}, {"service_policy_present": False},
                      {"service_policy_sha256": "0" * 64}, {"service_policy_format": True}):
            with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(Path(sys.executable), self.config, self.state))
        self.save(record | {"format": 1})
        self.assert_fault("service_state_invalid", lambda: service_state._record(self.state))
        self.save(record)
        self.assert_fault("runtime_stopped", lambda: service.guard_writes(self.state))
        self.assert_fault("runtime_stopped", lambda: service_runtime._start(self.state, record))

    def test_product_definition_selects_retained_git_and_accepts_exact_prior_profile(self):
        import plistlib
        original = service_runtime._definition(self.state, self.record)
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 14, "management_contract": 1,
                                                   "payload_sha256": "d" * 64})
        pending = self.record | {"current": self.b, "pending": {"from": self.a, "to": self.b, "previous": None}}
        selected = service_runtime._definition(self.state, pending)
        self.assertEqual(selected["EnvironmentVariables"]["PATH"].split(os.pathsep)[0],
                         str(self.state / "service/payloads" / ("d" * 64) / "bin"))
        owned = self.root / "fixture.plist"
        for path, content in ((owned, plistlib.dumps(original)),
                              (self.state / "service/launch.py", service_state.LAUNCHER.encode()),
                              (self.state / "service/service.log", b"")):
            path.write_bytes(content)
            path.chmod(0o600)
        with patch.object(service_runtime, "_plist_path", return_value=owned):
            service_runtime._verify_files(self.state, pending)
            service_runtime._sync_definition(self.state, pending)
            self.assertEqual(owned.read_bytes(), plistlib.dumps(selected))
            restored = pending | {"current": self.a}
            service_runtime._sync_definition(self.state, restored)
            self.assertEqual(owned.read_bytes(), plistlib.dumps(original))

    def test_stopped_update_and_restoration_preserve_recovery_without_starting(self):
        record = self.policy_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        verify, stop, start = self.activation_mocks()
        with patch.object(service_runtime, "_sync_definition"):
            result = service_transition._activate(self.state, record, self.b)
        self.assertEqual(result["status"], "stopped")
        start.assert_not_called()
        self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)
        current = service_state._record(self.state)
        with patch.object(service_runtime, "_sync_definition", side_effect=[OSError("fixture"), None]):
            self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, current, self.a))
        self.assertEqual(service_state._record(self.state)["current"], self.b)
        self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)
        start.assert_not_called()

    def test_policy_native_definition_preserves_exact_legacy_files_and_rejects_changed_bytes(self):
        import plistlib
        old = self.record
        record = self.policy_fixture()
        owned = self.root / "fixture.plist"
        for path, content in ((owned, plistlib.dumps(service_runtime._definition(self.state, old))),
                              (self.state / "service/launch.py", service_state.LAUNCHER.encode()),
                              (self.state / "service/manage.py", service_state.management_entry(self.state, record)),
                              (self.state / "service/service.log", b"")):
            path.write_bytes(content)
            path.chmod(0o600)
        legacy = (self.state / "service/launch.py").read_bytes()
        with patch.object(service_runtime, "_plist_path", return_value=owned):
            service_runtime._verify_files(self.state, record)
            service_runtime._sync_definition(self.state, record)
            self.assertEqual(plistlib.loads(owned.read_bytes())["ProgramArguments"][-1], "--launch")
            self.assertEqual((self.state / "service/launch.py").read_bytes(), legacy)
            owned.write_bytes(owned.read_bytes() + b"\n")
            self.assert_fault("service_definition_mismatch", lambda: service_runtime._verify_files(self.state, record))
        # Login takes the same entry, which exits before any selected runtime.
        entry = self.state / "service/manage.py"
        result = subprocess.run([sys.executable, "-I", str(entry), "--launch"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_startup_observes_effective_override_and_denial(self):
        label = service_runtime._label(self.state)
        def reply(disabled):
            return subprocess.CompletedProcess([], 0, f'disabled services = {{\n "{label}" => {disabled}\n}}', "")
        with patch.object(service_checks, "_run", return_value=reply("true")):
            self.assertFalse(service_runtime._startup(self.state)["enabled"])
            self.assert_fault("startup_denied", lambda: service_runtime._set_startup(self.state, True))
        with patch.object(service_checks, "_run", return_value=reply("false")):
            self.assertTrue(service_runtime._startup(self.state)["enabled"])
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, "unrecognized", "")):
            self.assert_fault("startup_unknown", lambda: service_runtime._startup(self.state))

    def test_explicit_policy_controls_keep_login_stop_and_recovery_separate(self):
        record = self.policy_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        with patch.object(service_runtime, "_verify_files"), patch.object(service_checks, "_candidate_state"), \
                patch.object(service_state, "_bind_management", side_effect=lambda state, record, identity: record), \
                patch.object(service_runtime, "_sync_definition"), patch.object(maintenance, "observe", return_value={}), \
                patch.object(service_runtime, "_set_startup") as startup, patch.object(service_runtime, "_start") as start, \
                patch.object(service_runtime, "_stop") as stop:
            maintenance.control(self.config, self.state, "startup", enabled=False)
            self.assertTrue(service_state.stopped(service_state._record(self.state)))
            stop.assert_not_called()
            start.assert_not_called()
            maintenance.control(self.config, self.state, "start")
            self.assertFalse(service_state.stopped(service_state._record(self.state)))
            self.assertFalse(service_state._record(self.state)["policy"]["start_at_login"])
            maintenance.control(self.config, self.state, "stop")
            self.assertTrue(service_state.stopped(service_state._record(self.state)))
            stop.assert_called_once()
            start.assert_called_once()
            startup.assert_called_once_with(self.state, False)
            self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)

    def test_management_contract_reports_timestamp_busy_incompatible_and_actual_selection(self):
        self.enterContext(patch.object(service_transition.sys, "platform", "darwin"))
        self.policy_fixture()
        with patch.object(service_runtime, "status", return_value={"status": "stopped", "loaded": False}), \
                patch.object(service_runtime, "_startup", return_value={"enabled": False}), \
                patch.object(service_runtime, "socket_alive", return_value=False):
            result = maintenance.dispatch(self.config, self.state, "status")
        self.assertEqual((result["status"], result["management_contract"], result["state_contract"]), ("stopped", 1, 14))
        self.assertEqual(result["entry"]["identity"], self.a)
        self.assertTrue(result["observed_at_ms"] > 0)
        self.assertFalse(result["startup"]["enabled"])
        self.assertTrue(result["policy"]["start_at_login"])
        self.assertEqual(maintenance.dispatch(self.config, self.state, "status", contract=99)["status"], "incompatible")
        with patch.object(maintenance, "observe", side_effect=Fault("resource_busy", "fixture")):
            self.assertEqual(maintenance.dispatch(self.config, self.state, "status")["status"], "busy")

    def test_manage_configure_accepts_complete_recovery_selections_and_rejects_partial_input(self):
        from openmachine.cli import main
        from openmachine.config import load
        path, state = self.root / "first.toml", self.root / "first-state"
        argv = ["openmachine", "--config", str(path), "--state-dir", str(state), "manage",
                "--contract", "1", "configure", "--workspace", str(self.root),
                "--recovery-connection", "OM", "--browser-account", "operator",
                "--browser-workspace", "Personal", "--recovery-model", "selected-model"]
        self.enterContext(patch.object(maintenance.sys, "platform", "darwin"))
        self.enterContext(patch.object(service_runtime, "_native", return_value={"loaded": False}))
        for invalid in (argv[:-2], [*argv[:-1], "bad\x7flabel"]):
            with patch.object(sys, "argv", invalid), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 1)
            self.assertEqual(json.loads(output.getvalue())["reason"], "invalid_config")
            self.assertFalse(path.exists())
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertIn("configure:chat_recovery", result["capabilities"])
        self.assertEqual(load(path).chat_recovery.workspace, "project")
        self.assertEqual(load(path).chat_recovery.connection, "OM")
        self.assertFalse(state.exists())

    def test_operation_same_id_reads_original_and_conflicting_ids_are_busy(self):
        record = self.operation_fixture()
        with patch.object(maintenance, "_spawn") as spawn, patch.object(service_checks, "_run") as run:
            result = maintenance.submit(self.config, self.state, "update", record["operation_id"], Path("missing"), "wrong")
            self.assertEqual(result["operation"], record)
            self.assertEqual(result["status"], "continuation_required")
            spawn.assert_not_called()
            run.assert_not_called()
        self.assert_fault("maintenance_busy", lambda: maintenance._available(self.state))
        with lease(self.state / "service/operation.lock"):
            self.assertEqual(maintenance.read_operation(self.config, self.state, record["operation_id"])["status"], "busy")
        self.assertEqual(maintenance._read(self.state, record["operation_id"]), record)

    def test_uncertain_phases_never_replay_and_interrupted_known_steps_require_continuation(self):
        for phase in ("retaining", "preparing", "activating", "recovery_required"):
            record = self.operation_fixture(phase)
            with patch.object(maintenance, "_spawn") as spawn:
                self.assertEqual(maintenance.read_operation(self.config, self.state, record["operation_id"])["status"], "recovery_required")
                self.assert_fault("maintenance_not_continuable", lambda: maintenance.continue_operation(self.config, self.state, record["operation_id"]))
                spawn.assert_not_called()
        for phase in ("accepted", "retained", "prepared", "deferred"):
            record = self.operation_fixture(phase)
            with patch.object(maintenance, "_spawn") as spawn, patch.object(maintenance.payload, "verify"):
                result = maintenance.continue_operation(self.config, self.state, record["operation_id"])
            self.assertEqual(result["operation"]["operation_id"], record["operation_id"])
            spawn.assert_called_once()

    def test_prepared_operation_defers_without_repreparing_then_explicitly_activates(self):
        record = self.operation_fixture()
        with patch.object(service_install, "_prepare_payload") as prepare, \
                patch.object(maintenance, "_bound"), patch.object(service_transition, "_activate",
                    side_effect=Fault("activation_deferred", "fixture")) as activate:
            with lease(self.state / "service/operation.lock") as lock:
                maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
            deferred = maintenance._read(self.state, record["operation_id"])
            self.assertEqual(deferred["phase"], "deferred")
            prepare.assert_not_called()
            def effect(state, selected, identity, snapshot, *, before_change, require_safe):
                before_change()
                self.assertEqual(maintenance._read(state, record["operation_id"])["phase"], "activating")
                self.assertTrue(require_safe)
                return {"status": "ready", "current": identity}
            activate.side_effect = effect
            with patch.object(service_state, "_bind_management", side_effect=lambda state, selected, identity: selected), \
                    lease(self.state / "service/operation.lock") as lock:
                maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
            original = maintenance.read_operation(self.config, self.state, record["operation_id"])
            self.assertEqual(original["status"], "completed")
            self.assertEqual(original["operation"]["outcome"]["current"], self.b)
            prepare.assert_not_called()

    def test_operation_binds_configuration_and_never_rebases_after_policy_drift(self):
        record = self.operation_fixture()
        original = self.config.read_bytes()
        self.config.write_bytes(original + b"\n# External edit\n")
        with patch.object(service_transition, "_activate") as activate, lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        result = maintenance._read(self.state, record["operation_id"])
        self.assertEqual((result["phase"], result["reason"]), ("rejected", "maintenance_input_changed"))
        activate.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original + b"\n# External edit\n")

    def test_lost_activation_result_is_uncertain_with_original_id(self):
        record = self.operation_fixture()
        def lost(*args, before_change, **kwargs):
            before_change()
            self.save(self.record | {"current": self.b, "previous": self.a})
            raise OSError("lost result after effect")
        with patch.object(maintenance, "_bound"), patch.object(service_transition, "_activate", side_effect=lost), \
                lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        result = maintenance.read_operation(self.config, self.state, record["operation_id"])
        self.assertEqual(result["status"], "recovery_required")
        self.assertEqual(result["operation"]["operation_id"], record["operation_id"])
        self.assertEqual(service_state._record(self.state)["current"], self.b)
        self.assert_fault("maintenance_not_continuable", lambda: maintenance.continue_operation(self.config, self.state, record["operation_id"]))

    def test_maintenance_spawn_is_independent_and_inherits_original_lease(self):
        record = self.operation_fixture("accepted")
        with lease(self.state / "service/operation.lock") as lock, patch.object(maintenance.subprocess, "Popen") as spawn:
            maintenance._spawn(self.state, record["operation_id"], lock, sys.executable)
            args, kwargs = spawn.call_args
            self.assertEqual(kwargs["pass_fds"], (lock.fileno(),))
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertIn(record["operation_id"], args[0])

    def test_service_subprocess_reply_and_deadline_are_bounded(self):
        result = service_checks._run([sys.executable, "-c", 'print("bounded")'])
        self.assertEqual(result.stdout, "bounded\n")
        self.assert_fault("service_output_limit", lambda: service_checks._run([sys.executable, "-c", 'print("x" * 70000)']))
        self.assert_fault("service_command_unverified", lambda: service_checks._run([sys.executable, "-c", 'import time; time.sleep(10)'], timeout=0.05))


    def test_product_install_registers_bound_stopped_service_before_reporting_completion(self):
        record = self.operation_fixture()
        (self.state / "service/service.json").unlink()
        record["request"].update(action="install", selection_sha256=None, workspace="project", path="config.toml")
        maintenance._save(self.state, record, "prepared")
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 14, "management_contract": 1,
                                                   "payload_sha256": "d" * 64})
        python = self.state / "service/payloads" / ("d" * 64) / "python/bin/python3"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        owned = self.root / "fixture.plist"
        with patch.object(service_runtime, "_plist_path", return_value=owned), \
                patch.object(service_runtime, "_native", return_value={"loaded": False, "pid": None}), \
                patch.object(service_install, "_verify_candidate"), patch.object(service_runtime, "_start") as start, \
                patch.object(service_runtime, "_set_startup", return_value={"enabled": False}) as startup, \
                lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
            selected = service_state._record(self.state)
            service_runtime._verify_files(self.state, selected)
        self.assertEqual(selected["current"], self.b)
        self.assertEqual(selected["policy"], {"desired": "stopped", "start_at_login": False})
        self.assertEqual(selected["management"]["identity"], self.b)
        self.assertEqual((self.state / "service/launch.py").read_text(), service_state.LAUNCHER)
        original = maintenance.read_operation(self.config, self.state, record["operation_id"])
        self.assertEqual(original["status"], "completed")
        self.assertEqual(original["operation"]["outcome"]["service"], "stopped")
        start.assert_not_called()
        startup.assert_called_once_with(self.state, False)

    def test_maintenance_capacity_and_corruption_are_not_reinitialized(self):
        record = self.operation_fixture("completed")
        with patch.object(maintenance, "MAX_OPERATIONS", 1):
            self.assert_fault("maintenance_store_full", lambda: maintenance._available(self.state))
        path = maintenance._operation_path(self.state, record["operation_id"])
        path.write_text('{"format": 99}')
        before = path.read_bytes()
        self.assert_fault("maintenance_state_invalid", lambda: maintenance._available(self.state))
        self.assertEqual(path.read_bytes(), before)
        self.assert_fault("invalid_operation_id", lambda: maintenance.read_operation(self.config, self.state, "../other"))

    def test_incoming_handoff_is_retained_verified_and_keeps_original_lock(self):
        record = self.operation_fixture("accepted")
        retained = self.state / "service/payloads" / record["request"]["payload_sha256"]
        proof = {"management_contract": 1, "version": "next", "prefix": str(retained / "python")}
        def handoff(python, argv, env):
            self.assertEqual(maintenance._read(self.state, record["operation_id"])["phase"], "retained")
            self.assertEqual(python, str(retained / "python/bin/python3"))
            self.assertIn(record["operation_id"], argv)
            self.assertEqual(env["PIP_NO_INDEX"], "1")
            self.assertTrue(os.get_inheritable(int(argv[-1])))
            raise SystemExit(0)
        with patch.object(maintenance.payload, "retain", return_value=(retained, {"core": {"version": "next"}})), \
                patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof), "")), \
                patch.object(maintenance.os, "execve", side_effect=handoff), \
                patch.object(service_install, "_prepare_payload") as prepare, lease(self.state / "service/operation.lock") as lock:
            with self.assertRaises(SystemExit):
                maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
            prepare.assert_not_called()
        self.assertEqual(maintenance.read_operation(self.config, self.state, record["operation_id"])["status"], "continuation_required")

    def test_uncertain_preparation_and_original_terminal_records_are_never_replayed(self):
        record = self.operation_fixture("retained")
        with patch.object(service_install, "_prepare_payload", side_effect=Fault("service_command_unverified", "fixture")), \
                lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        self.assertEqual(maintenance._read(self.state, record["operation_id"])["phase"], "recovery_required")
        for phase in ("completed", "restored", "rejected", "preparing", "activating"):
            record = self.operation_fixture(phase)
            with patch.object(maintenance, "_inputs_unchanged", side_effect=AssertionError("No replay")), \
                    lease(self.state / "service/operation.lock") as lock:
                maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
            self.assertEqual(maintenance._read(self.state, record["operation_id"]), record)

    def test_real_detached_maintenance_process_keeps_inherited_lock_and_original_record(self):
        record = self.operation_fixture("preparing")
        # This interrupted phase admits no effects. The real module startup
        # validates the inherited lock and exits without touching native jobs.
        original = maintenance._operation_path(self.state, record["operation_id"]).read_bytes()
        with lease(self.state / "service/operation.lock") as lock:
            process = subprocess.Popen([sys.executable, "-I", "-m", "openmachine.service.maintenance",
                str(self.state), record["operation_id"], str(lock.fileno())], cwd="/",
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(lock.fileno(),), start_new_session=True)
            self.assertEqual(process.communicate(timeout=10), (b"", b""))
            self.assertEqual(process.returncode, 0)
            self.assert_fault("resource_busy", lambda: maintenance.continue_operation(self.config, self.state, record["operation_id"]))
        self.assertEqual(maintenance._operation_path(self.state, record["operation_id"]).read_bytes(), original)

    def test_foreign_first_run_state_and_old_persistent_controls_are_rejected(self):
        private_directory(self.state / "executions")
        self.assert_fault("service_adoption_required", lambda: maintenance._uninstalled(self.state))
        with patch.object(service_runtime, "_verify_files"), patch.object(service_runtime, "_stop") as stop:
            self.assert_fault("incompatible_management", lambda: maintenance.control(self.config, self.state, "stop"))
            stop.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)

    def test_product_admission_defers_actual_unresolved_task_and_execution_readers(self):
        from support_tasks import task_contract
        from openmachine.tasks import records
        request = task_contract()
        created = tasks.create(self.state, request.pop("task_id"), **request)
        service_checks.activation_safe(self.state)
        # Compatibility still uses the actual canonical reader. An incomplete
        # canonical write cannot become an empty or safe task store.
        path = self.state / "tasks/structured-one/task.json"
        original = path.read_bytes()
        path.write_bytes(b'{}')
        self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        path.write_bytes(original)
        with patch.object(service_checks.tasks, "observation_page", return_value={
                "complete": True, "has_more": False, "entries": [{"task_id": "original", "metadata_sha256": "a" * 64,
                "record": {"operations": [{"state": "unknown", "result": None}]}}]}):
            self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        private_directory(self.state / "executions/original")
        with patch.object(service_checks.execution, "read_record", return_value={"status": "running"}):
            self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        self.assertEqual(path.read_bytes(), records.canonical(created["record"]))

    def test_stable_entry_is_a_real_cli_consumer_and_cannot_follow_a_changed_binding(self):
        record = self.policy_fixture()
        import sysconfig
        import venv
        for identity in (self.a, self.b):
            directory = self.state / "service/versions" / identity
            venv.EnvBuilder(with_pip=False).create(directory)
            site = directory / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
            (site / "fixture-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
        entry = self.state / "service/manage.py"
        entry.write_bytes(service_state.management_entry(self.state, record))
        entry.chmod(0o600)
        command = [sys.executable, "-I", str(entry), "--contract", "99", "status"]
        # Unsupported contract is rejected before any native observation.
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertTrue(result.stdout, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "incompatible_management")
        self.save(record | {"current": self.b, "previous": self.a})
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertTrue(result.stdout, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "incompatible_management")
        self.save(record | {"config": str(self.root / "foreign.toml")})
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "management_identity_unavailable")


    def test_startup_choice_does_not_change_cached_legacy_launch_arguments(self):
        path = self.state / "service/versions" / self.a / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 14, "management_contract": 1})
        before = (self.state / "service/service.json").read_bytes()
        with patch.object(service_runtime, "_verify_files"), patch.object(maintenance, "observe", return_value={}), \
                patch.object(service_runtime, "_set_startup") as startup, patch.object(service_runtime, "_stop") as stop, \
                patch.object(service_runtime, "_start") as start:
            maintenance.control(self.config, self.state, "startup", enabled=False)
            startup.assert_called_once_with(self.state, False)
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual((self.state / "service/service.json").read_bytes(), before)

    def test_idle_stopped_login_reports_stopped_and_start_reloads_disabled_job(self):
        record = self.policy_fixture()
        with patch.object(service_runtime, "status", return_value={"status": "unready", "loaded": True,
                    "pid": None, "supervisor_pid": None}), patch.object(service_runtime, "_startup", return_value={"enabled": False}), \
                patch.object(service_runtime, "socket_alive", return_value=False):
            self.assertEqual(maintenance.observe(self.config, self.state)["status"], "stopped")
        running = record | {"policy": record["policy"] | {"desired": "running", "start_at_login": False}}
        self.save(running)
        with patch.object(service_runtime, "_native", side_effect=[{"loaded": True, "pid": None},
                    {"loaded": False, "pid": None}, {"loaded": True, "pid": 123}]), \
                patch.object(service_runtime, "_stop") as stop, patch.object(service_runtime, "_sync_definition"), \
                patch.object(service_runtime, "_startup", return_value={"enabled": False}), \
                patch.object(service_runtime, "_set_startup") as startup, \
                patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(service_checks, "_managed_probe", return_value={"status": "ready"}):
            self.assertEqual(service_runtime._start(self.state, running)["status"], "ready")
        stop.assert_called_once_with(self.state)
        self.assertEqual(startup.call_args_list, [call(self.state, True), call(self.state, False)])
        self.assertFalse(service_state._record(self.state)["policy"]["start_at_login"])


    def test_native_startup_spellings_are_observed_without_guessing(self):
        label = service_runtime._label(self.state)
        for spelling, enabled in (("true", False), ("false", True), ("disabled", False), ("enabled", True)):
            output = 'disabled services = {\n    "' + label + '" => ' + spelling + '\n}\n'
            with self.subTest(spelling=spelling), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, output, "")):
                self.assertEqual(service_runtime._startup(self.state)["enabled"], enabled)
        for spelling in ("unknown", "true extra", "", "enabled unexpected"):
            output = 'disabled services = {\n    "' + label + '" => ' + spelling + '\n}\n'
            with self.subTest(spelling=spelling), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, output, "")):
                self.assert_fault("startup_unknown", lambda: service_runtime._startup(self.state))

    def test_old_reader_restoration_keeps_only_the_remaining_native_handoff(self):
        for identity, contract in ((self.a, 13), (self.b, 14)):
            path = self.state / "service/versions" / identity / "installation.json"
            save_json(path, service_state._json(path) | {"state_contract": contract})
        _, _, start = self.activation_mocks()
        def actual_reader_boundary(state, selected):
            if selected["current"] == self.b:
                raise Fault("service_unready", "Rejected candidate fixture")
            # Old supervisors validate every installation named by pending.
            # Keep the admission marker, but never ask that reader to load 14.
            pending = service_state._record(state)["pending"]
            self.assertIsNotNone(pending)
            with patch.object(service_state, "STATE_CONTRACT", 13):
                for identity in {selected["current"], pending["from"], pending["to"]}:
                    service_state._installed(state, identity)
            return {"status": "ready"}
        start.side_effect = actual_reader_boundary
        with patch.object(service_runtime, "_sync_definition") as sync:
            for interrupted in (False, True):
                with self.subTest(interrupted=interrupted):
                    record = dict(self.record)
                    if interrupted:
                        record.update(current=self.b, pending={"from": self.a, "to": self.b, "previous": None})
                    self.save(record)
                    if interrupted:
                        result = service_transition._activate(self.state, record, self.a)
                        self.assertEqual(result["status"], "ready")
                    else:
                        self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, record, self.b))
                    self.assertEqual(service_state._record(self.state)["current"], self.a)
                    self.assertIsNone(service_state._record(self.state)["pending"])
                    self.assertEqual(sync.call_args.args[1]["current"], self.a)
                    self.assertTrue((self.state / "service/versions" / self.b).is_dir())

    def test_policy_and_binding_preserve_recovery_advanced_before_admission(self):
        self.enterContext(patch.object(service_transition.sys, "platform", "darwin"))
        from contextlib import contextmanager
        self.policy_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        later = episode | {"attempts": 2}
        @contextmanager
        def admission(path, **options):
            if path.name == "lifecycle.lock":
                service_state._save_recovery(self.state, later)
            with lease(path, **options) as owned:
                yield owned
        with patch.object(maintenance, "lease", side_effect=admission), \
                patch.object(service_runtime, "_verify_files"), patch.object(service_checks, "_candidate_state"), \
                patch.object(service_state, "_bind_management", side_effect=lambda state, record, identity: record), \
                patch.object(service_runtime, "_sync_definition"), patch.object(service_runtime, "_stop"), \
                patch.object(maintenance, "observe", return_value={}):
            for action in ("stop", "bind"):
                service_state._save_recovery(self.state, episode)
                maintenance.control(self.config, self.state, action)
                self.assertEqual(service_state._read_recovery(self.state)["episode"], later)
        with patch.object(service_transition, "lease", side_effect=admission), \
                patch.object(service_runtime, "_verify_files"), patch.object(service_checks, "_candidate_state"), \
                patch.object(service_runtime, "_start", return_value={"status": "ready"}):
            service_state._save_recovery(self.state, episode)
            service_transition.operate(self.config, self.state, "start")
            self.assertFalse(service_state.stopped(service_state._record(self.state)))
            self.assertEqual(service_state._read_recovery(self.state)["episode"], later)


    def test_explicit_start_preserves_disabled_login_for_legacy_format(self):
        self.policy_fixture()
        self.save(self.record)
        with patch.object(service_runtime, "_native", side_effect=[
                    {"loaded": False, "pid": None}, {"loaded": True, "pid": 123}]), \
                patch.object(service_runtime, "_sync_definition"), \
                patch.object(service_runtime, "_startup", return_value={"enabled": False}), \
                patch.object(service_runtime, "_set_startup") as startup, \
                patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(service_checks, "_managed_probe", return_value={"status": "ready"}):
            self.assertEqual(service_runtime._start(self.state, self.record)["status"], "ready")
        self.assertEqual(startup.call_args_list, [call(self.state, True), call(self.state, False)])
        self.assertEqual(service_state._record(self.state)["format"], 1)
        self.assertNotIn("policy", service_state._record(self.state))


if __name__ == "__main__":
    unittest.main()
