"""Tests for service behavior at the component layer."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, call, patch
from openmachine import service
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, save_json
from openmachine.service import maintenance
from openmachine.service import checks as service_checks
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
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
