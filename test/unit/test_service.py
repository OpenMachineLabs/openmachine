"""Focused lifecycle failures; mocked launchctl is not native-service acceptance."""

from contextlib import ExitStack
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


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-service-", dir="/tmp")
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

    def activation_mocks(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        verify = stack.enter_context(patch.object(service, "_verify_candidate"))
        stack.enter_context(patch.object(service, "_candidate_state", return_value={}))
        stop = stack.enter_context(patch.object(service, "_stop"))
        start = stack.enter_context(patch.object(service, "_start", return_value={"status": "ready"}))
        return verify, stop, start

    def managed_fixture(self):
        path = self.state / "service/versions" / self.a / "installation.json"
        save_json(path, service._json(path) | {"state_contract": 9})

    def supervisor_mocks(self):
        self.managed_fixture()
        (self.state / "mcp.sock").touch()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stop, clock, child = threading.Event(), [1000.0], Mock(pid=2468)
        child.poll.return_value = None
        stack.enter_context(patch.object(service.threading, "Event", return_value=stop))
        stack.enter_context(patch.object(service.time, "time_ns", side_effect=lambda: int(clock[0] * 1e9)))
        stack.enter_context(patch.object(service.time, "monotonic", side_effect=lambda: clock[0]))
        stack.enter_context(patch.object(service.random, "uniform", return_value=1))
        stack.enter_context(patch.object(service, "_verify_files"))
        stack.enter_context(patch.object(service, "_native", return_value={"loaded": True, "pid": os.getpid()}))
        stack.enter_context(patch.object(service.os, "getpgrp", return_value=os.getpid()))
        stack.enter_context(patch.object(sys, "prefix", str(service._runtime(self.state, self.a))))
        launch = stack.enter_context(patch.object(service, "_launch_mcp", return_value=child))
        def advance(seconds):
            clock[0] += seconds
            if clock[0] > 2000:
                self.fail("The bounded supervisor did not stop")
        stack.enter_context(patch.object(stop, "wait", side_effect=advance))
        return stop, clock, child, launch

    def test_recovery_accounting_is_durable_bounded_and_reader_verified(self):
        self.assertIsNone(service._read_recovery(self.state))
        with patch.object(service.random, "uniform", return_value=1):
            episode = service._recovery_attempt(self.state, None, 1000000)
            self.assertEqual(episode["next_attempt_ms"], 1006000)
            original_id = episode["id"]
            self.assert_fault("runtime_recovery_cooldown", lambda: service._recovery_attempt(self.state, episode, 1005999))
            for attempt in range(2, 16):
                now = episode["next_attempt_ms"]
                episode = service._recovery_attempt(self.state, service._read_recovery(self.state)["episode"], now)
                self.assertEqual((episode["id"], episode["attempts"], episode["deadline_ms"]), (original_id, attempt, 1900000))
                self.assertEqual(episode["next_attempt_ms"] - now, 7000 if attempt == 2 else 65000)
            self.assert_fault("runtime_recovery_blocked", lambda: service._recovery_attempt(self.state, episode, episode["next_attempt_ms"]))
            self.assert_fault("runtime_recovery_blocked", lambda: service._recovery_attempt(self.state, dict(episode, attempts=1), 1900000))
        for jitter in (0.8, 1.2):
            with patch.object(service.random, "uniform", return_value=jitter):
                self.assertEqual([service._recovery_delay(n) for n in (1, 2, 3, 15)],
                                 [int(base * jitter) for base in (1000, 2000, 60000, 60000)])
        before = (self.state / "service/service.json").read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual(service._candidate_state(Path(sys.executable), self.config, self.state, 9), proof)
        for delta in ({"state_contract": 7}, {"runtime_recovery_present": False},
                      {"runtime_recovery_format": True}, {"runtime_recovery_sha256": "e" * 64}):
            with self.subTest(delta=delta), patch.object(service, "_run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_state", lambda: service._candidate_state(Path(sys.executable), self.config, self.state))
        self.assertEqual((self.state / "service/service.json").read_bytes(), before)

    def test_recovery_corruption_and_missing_history_never_reset(self):
        episode = service._recovery_attempt(self.state, None, 1000000)
        path = self.state / "service/service.json"
        for delta in ({"attempts": True}, {"attempts": 16}, {"deadline_ms": 1900001},
                      {"next_attempt_ms": 0}, {"reason": "raw error payload!"}):
            save_json(path, self.record | {"recovery": {"format": 1, "episode": episode | delta}})
            before = path.read_bytes()
            self.assert_fault("runtime_recovery_invalid", lambda: service.check_state(self.config, self.state))
            self.assertEqual(path.read_bytes(), before)
        path.unlink()
        with lease(self.state / "service/recovery.lock"):
            self.assert_fault("runtime_recovery_invalid", lambda: service._read_recovery(self.state))

    def test_supervisor_charges_before_effect_and_clears_only_actual_readiness(self):
        stop, clock, child, launch = self.supervisor_mocks()
        service._save_recovery(self.state, None)
        attempts, times = [], []
        def launch_after_accounting(*_):
            self.assertEqual(service._read_recovery(self.state)["episode"]["attempts"], 1)
            return child
        launch.side_effect = launch_after_accounting
        def probe(*args, **kwargs):
            episode = service._read_recovery(self.state)["episode"]
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
        with patch.object(service, "_managed_probe", side_effect=probe):
            def advance(seconds):
                clock[0] += seconds
                if service._read_recovery(self.state)["episode"] is None:
                    stop.set()
            stop.wait.side_effect = advance
            service.supervise(self.config, self.state)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(times, [1000, 1001, 1003])
        launch.assert_called_once()
        child.terminate.assert_called_once()
        self.assertIsNone(service._read_recovery(self.state)["episode"])

    def test_supervisor_exhaustion_is_preserved_across_parent_restart(self):
        _, _, child, launch = self.supervisor_mocks()
        service._save_recovery(self.state, None)
        with patch.object(service, "_managed_probe", side_effect=Fault("service_unready", "fixture")) as probe:
            service.supervise(self.config, self.state)
            episode = service._read_recovery(self.state)["episode"]
            self.assertEqual((episode["attempts"], episode["reason"]), (15, "blocked_budget_exhausted"))
            self.assertEqual(probe.call_count, 15)
            service.supervise(self.config, self.state)
            self.assertEqual(service._read_recovery(self.state)["episode"], episode)
            self.assertEqual(probe.call_count, 15)
        launch.assert_called_once()
        child.terminate.assert_called_once()

    def test_post_ready_hang_replaces_only_owned_child_after_durable_accounting(self):
        stop, clock, child, launch = self.supervisor_mocks()
        service._save_recovery(self.state, None)
        replacement = Mock(pid=2469)
        replacement.poll.return_value = None
        launch.side_effect = [child, replacement]
        probes = []
        def probe(*args, **kwargs):
            probes.append((clock[0], kwargs["child"]))
            if len(probes) == 2:
                self.assertIsNone(service._read_recovery(self.state)["episode"])
                raise Fault("service_unready", "post-ready hang")
            if len(probes) == 3:
                self.assertEqual(service._read_recovery(self.state)["episode"]["attempts"], 2)
        def terminate():
            episode = service._read_recovery(self.state)["episode"]
            self.assertEqual((episode["attempts"], episode["reason"]), (1, "service_unready"))
        child.terminate.side_effect = terminate
        child.wait.side_effect = [subprocess.TimeoutExpired("owned child", 5), 0]
        def advance(seconds):
            clock[0] += seconds
            if len(probes) == 3:
                stop.set()
        stop.wait.side_effect = advance
        with patch.object(service, "_managed_probe", side_effect=probe):
            service.supervise(self.config, self.state)
        self.assertEqual(probes, [(1000, 2468), (1030, 2468), (1031, 2469)])
        self.assertEqual(launch.call_count, 2)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_args_list, [call(timeout=5), call(timeout=5)])
        replacement.terminate.assert_called_once()
        self.assertIsNone(service._read_recovery(self.state)["episode"])

    def test_healthy_checks_and_busy_slot_never_restart_or_spend_recovery_budget(self):
        stop, clock, child, launch = self.supervisor_mocks()
        service._save_recovery(self.state, None)
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
        with patch.object(service, "_managed_probe", side_effect=probe):
            service.supervise(self.config, self.state)
        self.assertEqual(times, [1000, 1030, 1031, 1061])
        launch.assert_called_once()
        child.terminate.assert_called_once()
        self.assertIsNone(service._read_recovery(self.state)["episode"])

    def test_post_ready_fatal_identity_failure_blocks_without_replacement(self):
        _, _, child, launch = self.supervisor_mocks()
        service._save_recovery(self.state, None)
        with patch.object(service, "_managed_probe", side_effect=[{}, Fault("service_identity_mismatch", "fixture")]):
            service.supervise(self.config, self.state)
        episode = service._read_recovery(self.state)["episode"]
        self.assertEqual((episode["attempts"], episode["reason"]), (1, "blocked_service_identity_mismatch"))
        launch.assert_called_once()
        child.terminate.assert_called_once()

    def test_supervisor_fatal_failure_and_accounting_failure_do_not_retry(self):
        _, _, _, launch = self.supervisor_mocks()
        service._save_recovery(self.state, None)
        with patch.object(service, "_managed_probe", side_effect=Fault("service_identity_mismatch", "fixture")) as probe:
            service.supervise(self.config, self.state)
        episode = service._read_recovery(self.state)["episode"]
        self.assertEqual((episode["attempts"], episode["reason"]), (1, "blocked_service_identity_mismatch"))
        probe.assert_called_once()
        launch.reset_mock()
        service._save_recovery(self.state, None)
        self.config.write_text("invalid TOML =")
        service.supervise(self.config, self.state)
        episode = service._read_recovery(self.state)["episode"]
        self.assertEqual((episode["attempts"], episode["reason"]), (1, "blocked_invalid_config"))
        launch.assert_not_called()
        service._save_recovery(self.state, None)
        with patch.object(service, "_save_recovery", side_effect=Fault("runtime_recovery_unverified", "fixture")):
            service.supervise(self.config, self.state)
        launch.assert_not_called()

    def test_stop_during_original_cooldown_starts_no_child_and_keeps_budget(self):
        stop, _, _, launch = self.supervisor_mocks()
        episode = service._recovery_attempt(self.state, None, 1000000)
        stop.wait.side_effect = lambda _: stop.set()
        service.supervise(self.config, self.state)
        launch.assert_not_called()
        self.assertEqual(service._read_recovery(self.state)["episode"], episode)

    def test_status_does_not_probe_or_renew_an_active_recovery_episode(self):
        self.managed_fixture()
        episode = service._recovery_attempt(self.state, None, 1000000)
        with patch.object(service, "_verify_files"), patch.object(service, "_native", return_value={"loaded": True, "pid": 123}), \
                patch.object(service, "_managed_probe") as probe:
            status = service.status(self.config, self.state)
            self.assertEqual((status["status"], status["pid"], status["supervisor_pid"]), ("recovering", None, 123))
            self.assertEqual(status["recovery"], episode)
            service._save_recovery(self.state, dict(episode, reason="blocked_budget_exhausted"))
            self.assertEqual(service.status(self.config, self.state)["status"], "blocked")
            probe.assert_not_called()

    def test_only_explicit_restart_renews_budget_after_verified_stop(self):
        self.managed_fixture()
        episode = service._recovery_attempt(self.state, None, 1000000)
        with patch.object(sys, "platform", "darwin"), patch.object(service, "_verify_files"), \
                patch.object(service, "_candidate_state"), patch.object(service, "_start", return_value={"status": "ready"}), \
                patch.object(service, "_stop") as stop:
            for action in ("start", "restart", "stop"):
                service.operate(self.config, self.state, action)
                self.assertEqual(service._read_recovery(self.state)["episode"], episode)
            stop.side_effect = Fault("service_stop_unverified", "fixture")
            self.assert_fault("service_stop_unverified", lambda: service.operate(self.config, self.state, "restart", reset_recovery=True))
            self.assertEqual(service._read_recovery(self.state)["episode"], episode)
            stop.side_effect = None
            service.operate(self.config, self.state, "restart", reset_recovery=True)
            self.assertIsNone(service._read_recovery(self.state)["episode"])
            save_json(self.state / "service/service.json", self.record | {"recovery": {"format": 999}})
            self.assert_fault("runtime_recovery_invalid", lambda: service.operate(self.config, self.state, "restart", reset_recovery=True))

    def test_managed_probe_binds_child_parent_and_overall_deadline(self):
        with patch.object(service, "probe_service", return_value={"status": "ready", "pid": 123}) as probe, \
                patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 0, "456\n", "")):
            self.assertEqual(service._managed_probe(self.state, self.record, 456)["supervisor_pid"], 456)
            self.assertEqual(probe.call_args.kwargs["timeout_seconds"], 5)
            self.assert_fault("service_identity_mismatch", lambda: service._managed_probe(self.state, self.record, 999))
            with patch.object(service.time, "monotonic", side_effect=[100, 105]):
                self.assert_fault("service_unready", lambda: service._managed_probe(self.state, self.record, 456))

    def test_first_upgrade_preserves_old_state_until_readiness_and_restores_on_failure(self):
        self.activation_mocks()
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service._json(path) | {"state_contract": 9})
        def start(state, record):
            self.assertIsNone(service._read_recovery(state))
            if record["current"] == self.b:
                raise Fault("service_unready", "fixture")
            return {"status": "ready"}
        with patch.object(service, "_start", side_effect=start):
            self.assert_fault("activation_failed_restored", lambda: service._activate(self.state, self.record, self.b))
        self.assertIsNone(service._read_recovery(self.state))
        self.assertTrue(service._definition(self.state, self.record)["AbandonProcessGroup"])
        with patch.object(service, "_start", return_value={"status": "ready"}):
            service._activate(self.state, self.record, self.b)
        self.assertEqual(service._read_recovery(self.state), {"format": 1, "episode": None})
        self.assertFalse(service._definition(self.state, service._record(self.state))["AbandonProcessGroup"])

    def test_first_upgrade_commit_crash_leaves_rollback_or_committed_selection(self):
        self.activation_mocks()
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service._json(path) | {"state_contract": 9})
        for committed in (False, True):
            self.save(self.record)
            def crash_at_commit(path, value):
                if path == self.state / "service/service.json" and value["current"] == self.b and value["pending"] is None:
                    self.assertEqual(value["recovery"], {"format": 1, "episode": None})
                    if committed:
                        save_json(path, value)
                    raise OSError("Injected controller loss at atomic commit")
                save_json(path, value)
            with self.subTest(committed=committed), patch.object(service, "save_json", side_effect=crash_at_commit):
                with self.assertRaises(OSError):
                    service._activate(self.state, self.record, self.b)
            observed = service._record(self.state)
            if committed:
                self.assertIsNone(observed["pending"])
                self.assertEqual(observed["current"], self.b)
                self.assertEqual(service._read_recovery(self.state), {"format": 1, "episode": None})
            else:
                self.assertEqual(observed["pending"]["from"], self.a)
                self.assertIsNone(service._read_recovery(self.state))
                import plistlib
                owned_plist = self.root / "owned.plist"
                for owned, content in ((owned_plist, plistlib.dumps(service._definition(self.state, observed))),
                                       (self.state / "service/launch.py", service.LAUNCHER.encode()),
                                       (self.state / "service/service.log", b"")):
                    owned.write_bytes(content)
                    owned.chmod(0o600)
                def restore(state, selection):
                    service._verify_files(state, selection)
                    return {"status": "ready"}
                with patch.object(service, "_plist_path", return_value=owned_plist), patch.object(service, "_start", side_effect=restore):
                    service._activate(self.state, observed, self.a)
                self.assertEqual(service._record(self.state)["current"], self.a)
                self.assertIsNone(service._read_recovery(self.state))

    def test_activation_preserves_recovery_advanced_during_candidate_preflight(self):
        verify, _, start = self.activation_mocks()
        self.managed_fixture()
        episode = service._recovery_attempt(self.state, None, 1000000)
        later = dict(episode, attempts=2)
        verify.side_effect = lambda *_args, **_kwargs: service._save_recovery(self.state, later)
        start.side_effect = Fault("service_unready", "fixture")
        self.assert_fault("recovery_required", lambda: service._activate(self.state, self.record, self.b))
        self.assertEqual(service._read_recovery(self.state)["episode"], later)

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
        from test_continuity import task_contract, terminal_task_fixture
        from openmachine.tasks import records
        request = task_contract()
        created = tasks.create(self.state, request.pop("task_id"), **request)
        path = self.state / "tasks/structured-one/task.json"
        path.write_bytes(records.canonical(terminal_task_fixture(created["record"])))
        tasks.rebuild_index(self.state)
        raw = path.read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["task_records"], proof["structured_task_records"]), (9, 1, 1))
        actual = service._candidate_state(Path(sys.executable), self.config, self.state, 9)
        self.assertEqual(actual, proof)
        for change in ({"structured_task_records": 0}, {"task_records": True},
                       {"task_store_present": 1}, {"structured_task_read_formats": [True]},
                       {"structured_task_read_formats": [1, 1]}, {"task_index_coverage": "incomplete"},
                       {"task_index_generation": proof["task_index_generation"] - 1}):
            with self.subTest(change=change), patch.object(service, "_run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(proof | change), "")):
                self.assert_fault("incompatible_state", lambda: service._candidate_state(Path(sys.executable), self.config, self.state, 9))
        self.assertEqual(path.read_bytes(), raw)
        path.unlink()
        self.assert_fault("corrupt_tasks", lambda: service.check_state(self.config, self.state))

    def test_candidate_ignoring_canonical_tasks_is_rejected_before_healthy_service_stop(self):
        from test_continuity import task_contract
        request = task_contract()
        tasks.create(self.state, request.pop("task_id"), **request)
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service._json(path) | {"state_contract": 6})
        ignored = subprocess.CompletedProcess([], 0, json.dumps({"protocol": 1, "state_contract": 6}), "")
        original = (self.state / "tasks/structured-one/task.json").read_bytes()
        with patch.object(service, "_verify_candidate"), patch.object(service, "_run", return_value=ignored), \
                patch.object(service, "_stop") as stop, patch.object(service, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service._activate(self.state, self.record, self.b))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(service._record(self.state), self.record)
        self.assertEqual((self.state / "tasks/structured-one/task.json").read_bytes(), original)

    def test_retained_ssh_budget_requires_actual_reader_before_service_stop(self):
        from integrations.ssh import recovery
        recovery.replace(self.state, "c" * 64, None, {"episode_id": "d" * 32,
            "started_at_ms": 1, "deadline_ms": 3600001, "attempts": 60,
            "next_attempt_ms": 3600001, "reason_code": "ssh_transport_unavailable"})
        before = (self.state / "ssh-recovery.json").read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["ssh_recovery_targets"]), (9, 1))
        self.assertEqual(service._candidate_state(Path(sys.executable), self.config, self.state, 9), proof)
        for delta in ({"state_contract": 6}, {"ssh_recovery_present": False},
                      {"ssh_recovery_targets": 0}, {"ssh_recovery_targets": True},
                      {"ssh_recovery_sha256": "e" * 64}, {"ssh_recovery_format": 2}):
            with self.subTest(delta=delta), patch.object(service, "_verify_candidate"), patch.object(service, "_run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")), \
                    patch.object(service, "_stop") as stop:
                self.assert_fault("incompatible_state", lambda: service._activate(self.state, self.record, self.b))
                stop.assert_not_called()
        self.assertEqual((self.state / "ssh-recovery.json").read_bytes(), before)

    def test_task_creation_and_index_maintenance_share_lifecycle_admission(self):
        from test_continuity import task_contract
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
            with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], code, json.dumps(value), "")):
                self.assert_fault("incompatible_state", lambda: service._candidate_state(Path(sys.executable), self.config, self.state))

    def test_pending_and_wrong_runtime_block_new_writes(self):
        self.record["pending"] = {"from": self.a, "to": self.b, "previous": None}
        self.save(self.record)
        self.assert_fault("activation_pending", lambda: service.guard_writes(self.state))
        self.record["pending"] = None
        self.save(self.record)
        self.assert_fault("service_identity_mismatch", lambda: service.guard_writes(self.state))
        with patch.object(sys, "prefix", str(service._runtime(self.state, self.a))):
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
            with self.subTest(operation=name), patch.object(owner, method) as dispatch:
                self.record["pending"] = {"from": self.a, "to": self.b, "previous": None}
                self.save(self.record)
                self.assert_fault("activation_pending", lambda: operation(*arguments))
                self.record["pending"] = None
                self.save(self.record)
                self.assert_fault("service_identity_mismatch", lambda: operation(*arguments))
                with lease(self.state / "locks/lifecycle.lock"):
                    self.assert_fault("resource_busy", lambda: operation(*arguments))
                    self.assertEqual(app.find_tasks()["status"], "uninitialized")
                    self.assertEqual(app.get_capabilities()["lifecycle"]["state_contract"], 9)
                dispatch.assert_not_called()

                def admitted(*args, **kwargs):
                    with self.assertRaises(Fault) as caught:
                        with lease(self.state / "locks/lifecycle.lock"):
                            self.fail("Admission lease was released before dispatch")
                    self.assertEqual(caught.exception.code, "resource_busy")
                    return {"dispatched": True}

                dispatch.side_effect = admitted
                with patch.object(sys, "prefix", str(service._runtime(self.state, self.a))), \
                        patch.object(application, "lease", wraps=lease) as admission:
                    self.assertEqual(operation(*arguments), {"dispatched": True})
                    admission.assert_called_once_with(self.state / "locks/lifecycle.lock", shared=True)
                prefix = (config,) if name.startswith("github_") else (self.state,) if name == "save_task" else ()
                self.assertEqual(dispatch.call_count, 1)
                self.assertEqual(signature.bind(*dispatch.call_args.args, **dispatch.call_args.kwargs).arguments,
                                 signature.bind(*prefix, *arguments).arguments)
                with lease(self.state / "locks/lifecycle.lock"):
                    pass

    def test_successful_activation_retains_rollback_target(self):
        verify, stop, start = self.activation_mocks()
        result = service._activate(self.state, self.record, self.b)
        self.assertEqual((result["current"], result["previous"]), (self.b, self.a))
        self.assertIsNone(service._record(self.state)["pending"])
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(start.call_count, 1)
        verify.assert_called_once()
        self.assertTrue(service._runtime(self.state, self.a).is_dir())

    def test_bad_candidate_does_not_stop_or_select_over_healthy_version(self):
        verify, stop, start = self.activation_mocks()
        verify.side_effect = Fault("candidate_unready", "fixture")
        self.assert_fault("candidate_unready", lambda: service._activate(self.state, self.record, self.b))
        self.assertEqual(service._record(self.state), self.record)
        stop.assert_not_called()
        start.assert_not_called()

    def test_failed_activation_restores_original_service_and_returns_failure(self):
        _, stop, start = self.activation_mocks()
        start.side_effect = [Fault("service_read_unavailable", "fixture"), {"status": "ready"}]
        self.assert_fault("activation_failed_restored", lambda: service._activate(self.state, self.record, self.b))
        self.assertEqual(service._record(self.state), self.record)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual([call.args[1]["current"] for call in start.call_args_list], [self.b, self.a])

    def test_failed_restoration_retains_exact_recovery_intent(self):
        _, _, start = self.activation_mocks()
        start.side_effect = Fault("service_unready", "fixture")
        self.assert_fault("recovery_required", lambda: service._activate(self.state, self.record, self.b))
        observed = service._record(self.state)
        self.assertEqual(observed["current"], self.a)
        self.assertEqual(observed["pending"], {"from": self.a, "to": self.b, "previous": None})
        self.assert_fault("activation_pending", lambda: service.guard_writes(self.state))

    def test_interrupted_selection_recovers_original_not_unverified_candidate(self):
        self.activation_mocks()
        pending = {**self.record, "current": self.b,
                   "pending": {"from": self.a, "to": self.b, "previous": None}}
        self.save(pending)
        result = service._activate(self.state, pending, self.a)
        self.assertEqual(result["current"], self.a)
        self.assertIsNone(result["previous"])
        self.assertIsNone(service._record(self.state)["pending"])

    def test_activation_lock_conflict_preserves_selection(self):
        _, stop, start = self.activation_mocks()
        with lease(self.state / "locks/lifecycle.lock", shared=True):
            self.assert_fault("resource_busy", lambda: service._activate(self.state, self.record, self.b))
        self.assertEqual(service._record(self.state), self.record)
        stop.assert_not_called()
        start.assert_not_called()

    def test_user_service_definition_preserves_workers_and_owns_no_tunnel(self):
        definition = service._definition(self.state, self.record)
        self.assertTrue(definition["AbandonProcessGroup"])
        self.assertTrue(definition["RunAtLoad"])
        self.assertEqual(definition["EnvironmentVariables"], {"PATH": os.defpath})
        self.assertNotIn("tunnel", json.dumps(definition).lower())
        self.assertEqual(definition["ProgramArguments"][0], self.record["launch_python"])
        self.assertNotEqual(service._label(self.state), service._label(self.root / "other-state"))

    def test_missing_and_unverifiable_native_states_are_distinct(self):
        with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 3, "", "Could not find service fixture")):
            self.assertFalse(service._native(self.state)["loaded"])
        with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 1, "", "Unknown GUI domain")):
            self.assert_fault("user_service_unavailable", lambda: service._native(self.state))
        with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 0, "path = /other/job.plist\npid = 123\n", "")):
            self.assert_fault("service_identity_mismatch", lambda: service._native(self.state))

    def test_invalid_lock_is_rejected_before_package_commands(self):
        wheel = self.root / "openmachine-0.1.0a7-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("openmachine-0.1.0a7.dist-info/METADATA", "Name: openmachine\nVersion: 0.1.0a7\n")
        requirements = self.root / "requirements.lock"
        requirements.write_text("mcp>=1\n")
        with patch.object(service, "_run") as run:
            self.assert_fault("invalid_installation_artifact", lambda: service._prepare(self.state, wheel, requirements, self.config))
            run.assert_not_called()
        self.assertEqual(service._record(self.state), self.record)

    def test_corrupt_selection_is_not_replaced(self):
        value = {**self.record, "current": "../other"}
        self.save(value)
        self.assert_fault("service_state_invalid", lambda: service._record(self.state))
        self.assertEqual(json.loads((self.state / "service/service.json").read_text()), value)

    def test_missing_selection_with_launcher_is_not_an_empty_installation(self):
        (self.state / "service/service.json").unlink()
        (self.state / "service/launch.py").write_text(service.LAUNCHER)
        self.assert_fault("service_state_invalid", lambda: service.status(self.config, self.state))

    def test_incomplete_installation_metadata_rejects_missing_version(self):
        path = self.state / "service/versions" / self.a / "installation.json"
        value = service._json(path)
        del value["version"]
        save_json(path, value)
        self.assert_fault("incompatible_runtime", lambda: service._installed(self.state, self.a))

    def test_restart_rejects_incompatible_state_before_stopping_service(self):
        with patch.object(sys, "platform", "darwin"), patch.object(service, "_verify_files"), \
                patch.object(service, "_candidate_state", side_effect=Fault("incompatible_state", "fixture")), \
                patch.object(service, "_stop") as stop, patch.object(service, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service.operate(self.config, self.state, "restart"))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(service._record(self.state), self.record)

    def test_directional_metadata_and_candidate_contract_are_explicit(self):
        path = self.state / "service/versions" / self.b / "installation.json"
        original = service._json(path)
        for contract in range(1, 10):
            save_json(path, original | {"state_contract": contract})
            self.assertEqual(service._installed(self.state, self.b)["state_contract"], contract)
            reply = {"protocol": 1, "state_contract": contract}
            with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")):
                self.assertEqual(service._candidate_state(Path(sys.executable), self.config, self.state, contract), reply)
                self.assert_fault("incompatible_state", lambda: service._candidate_state(
                    Path(sys.executable), self.config, self.state, 1 if contract != 1 else 3))
        for contract in (0, 10, True, "3"):
            save_json(path, original | {"state_contract": contract})
            self.assert_fault("incompatible_runtime", lambda: service._installed(self.state, self.b))

    def test_explicit_configuration_survives_update_and_exact_rollback(self):
        self.activation_mocks()
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# explicitly selected candidate configuration\n")
        service._activate(self.state, self.record, self.b, candidate)
        self.assertEqual(self.config.read_bytes(), candidate.read_bytes())
        selected = service._record(self.state)
        self.assertEqual(set(selected["configurations"]), {self.a, self.b})
        service._activate(self.state, selected, self.a)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service._record(self.state)["current"], self.a)

    def test_failed_activation_restores_exact_configuration_bytes(self):
        _, stop, start = self.activation_mocks()
        start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# candidate configuration\n")
        self.assert_fault("activation_failed_restored", lambda: service._activate(
            self.state, self.record, self.b, candidate))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service._record(self.state), self.record)
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
        self.assert_fault("recovery_required", lambda: service._activate(
            self.state, self.record, self.b, candidate))
        self.assertEqual(self.config.read_bytes(), manual)
        self.assertIsNotNone(service._record(self.state)["pending"])
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
        self.assert_fault("incompatible_remote_state", lambda: service._activate(
            self.state, self.record, self.b, candidate))
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service._record(self.state), self.record)

    def test_failed_update_restores_current_bytes_not_a_stale_snapshot(self):
        _, _, start = self.activation_mocks()
        service._activate(self.state, self.record, self.b)
        current = service._record(self.state)
        edited = self.config.read_bytes() + b"# valid edit since the previous activation\n"
        self.config.write_bytes(edited)
        start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
        self.assert_fault("activation_failed_restored", lambda: service._activate(
            self.state, current, self.a, self.config))
        self.assertEqual(self.config.read_bytes(), edited)
        restored = service._record(self.state)
        saved = service._configuration_for(self.state, restored, self.b)
        self.assertEqual(saved.read_bytes(), edited)

    def test_unrelated_retained_configuration_is_not_an_allowed_racing_edit(self):
        _, _, start = self.activation_mocks()
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# first explicit configuration\n")
        service._activate(self.state, self.record, self.b, candidate)
        current = service._record(self.state)
        third = "c" * 64
        directory = private_directory(self.state / "service/versions" / third)
        save_json(directory / "installation.json", {
            "identity": third, "version": "0.1.0a7", "protocol": 1, "state_contract": 1})
        candidate.write_bytes(original + b"# next explicit configuration\n")
        def independently_restore_older_bytes(*_):
            self.config.write_bytes(original)
            raise Fault("service_unready", "fixture")
        start.side_effect = independently_restore_older_bytes
        self.assert_fault("recovery_required", lambda: service._activate(
            self.state, current, third, candidate))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIsNotNone(service._record(self.state)["pending"])

    def test_corrupt_configuration_snapshot_cannot_be_selected(self):
        self.activation_mocks()
        service._activate(self.state, self.record, self.b)
        selected = service._record(self.state)
        digest = selected["configurations"][self.a]
        path = service._saved_configuration(self.state, digest)
        path.write_text("corrupt")
        self.assert_fault("configuration_unavailable", lambda: service._activate(self.state, selected, self.a))
        self.assertEqual(service._record(self.state), selected)


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
        with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof), "")) as call:
            self.assertEqual(service._remote_workspace_state(python, self.config, "remote"), proof)
            self.assertEqual(call.call_args.args[0], [str(python), "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                                       "doctor", "--machine", "remote"])
        for delta in ({"workspace_change_format": True}, {"workspace_change_store_present": False},
                      {"target_identity": "f" * 64}, {"legacy_workspace_compatible": None}):
            with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_remote_state", lambda: service._remote_workspace_state(python, self.config, "remote"))
        # Missing unrelated source is not unreadable target history or a reason to disrupt a healthy service.
        missing = proof | {"unavailable_workspaces": ["unrelated"]}
        with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 1, json.dumps(missing), "")):
            self.assertEqual(service._remote_workspace_state(python, self.config, "remote"), missing)

    def test_retained_remote_workspace_state_blocks_old_selection_before_stop(self):
        pin, archive = "d" * 64, "e" * 64
        with self.config.open("a") as file:
            file.write('\n[machines.remote]\ntransport="ssh"\nssh_alias="fixture"\nssh_identity="' + pin +
                       '"\nssh_worker="/target/' + archive + '.pyz"\n')
        installation = self.state / "service/versions" / self.a / "installation.json"
        save_json(installation, service._json(installation) | {"state_contract": 4})
        _, stop, start = self.activation_mocks()
        for present, compatible in ((True, True), (False, False)):
            proof = {"workspace_change_store_present": present, "legacy_workspace_compatible": compatible}
            with patch.object(service, "_remote_workspace_state", return_value=proof) as read:
                self.assert_fault("incompatible_remote_state", lambda: service._activate(self.state, self.record, self.b))
                self.assertEqual(read.call_args.args, (service._runtime(self.state, self.a) / "bin/python", self.config, "remote"))
            self.assertEqual(service._record(self.state), self.record)
            stop.assert_not_called()
            start.assert_not_called()

    def test_actual_local_change_reader_blocks_old_candidate_that_ignores_store(self):
        from openmachine import workspace
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\nexecution_enabled=true\n'
                               '[workspaces.project]\nmachine="local"\nroot=' + json.dumps(str(self.root)) + '\n')
        config = service.load(self.config)
        file = self.root / "source.py"
        file.write_text("answer = 41")
        read = workspace.read_text_file(config, "local", "project", "source.py")
        changed = workspace.edit_text_file(config, self.state, "local", "project", "source.py",
            [{"old_text": "41", "new_text": "42"}], read["context"]["workspace_identity"], "", read["sha256"], "d" * 32)
        self.assertEqual(changed["status"], "applied")
        record = self.state / "workspace-changes" / ("d" * 32) / "record.json"
        retained = record.read_bytes()
        ignored = subprocess.CompletedProcess([], 0, json.dumps({"protocol": 1, "state_contract": 1}), "")
        with patch.object(service, "_verify_candidate"), patch.object(service, "_run", return_value=ignored), \
             patch.object(service, "_stop") as stop, patch.object(service, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service._activate(self.state, self.record, self.b))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(record.read_bytes(), retained)
        self.assertEqual(service._record(self.state), self.record)


    def test_change_format_coverage_is_directional_and_requires_actual_readers(self):
        from openmachine import workspace
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\nworkspace_write_enabled=true\n'
                               '[workspaces.project]\nmachine="local"\nroot='+json.dumps(str(self.root))+'\n')
        config = service.load(self.config)
        file = self.root / "source.py"
        file.write_text("a\n")
        read = workspace.read_text_file(config,"local","project","source.py")
        first = workspace.edit_text_file(config,self.state,"local","project","source.py",
            [{"old_text":"a","new_text":"b"}],read["context"]["workspace_identity"],"",read["sha256"],"1"*32)
        self.assertEqual(first["format"],1)
        legacy = {"protocol":1,"state_contract":4,"workspace_change_format":1,
                  "workspace_change_store_present":True,"workspace_change_records":1}
        with patch.object(service,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(legacy),"")):
            self.assertEqual(service._candidate_state(Path(sys.executable),self.config,self.state),legacy)
        second = workspace.apply_patch(config,self.state,"local","project",
            "*** Begin Patch\n*** Add File: new\n+x\n*** End Patch\n",read["context"]["workspace_identity"],"",
            [{"path":"new","expected_sha256":""}],change_id="2"*32)
        self.assertEqual(second["format"],2)
        legacy["workspace_change_records"] = 2
        with patch.object(service,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(legacy),"")):
            self.assert_fault("incompatible_state",lambda:service._candidate_state(Path(sys.executable),self.config,self.state))
        current = service.check_state(self.config,self.state)
        self.assertEqual(current["workspace_change_formats_present"],[1,2])
        records = list((self.state / "workspace-changes").glob("*/record.json"))
        before = [path.read_bytes() for path in records]
        with patch.object(service,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(current),"")):
            service._candidate_state(Path(sys.executable),self.config,self.state)
        self.assertEqual([path.read_bytes() for path in records],before)

if __name__ == "__main__":
    unittest.main()
