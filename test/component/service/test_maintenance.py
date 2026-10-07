"""Tests for service behavior at the component layer."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import maintenance
from openmachine.service import checks as service_checks
from openmachine.service import install as service_install
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from openmachine.service import transition as service_transition
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
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

    def test_foreign_first_run_state_and_old_persistent_controls_are_rejected(self):
        private_directory(self.state / "executions")
        self.assert_fault("service_adoption_required", lambda: maintenance._uninstalled(self.state))
        with patch.object(service_runtime, "_verify_files"), patch.object(service_runtime, "_stop") as stop:
            self.assert_fault("incompatible_management", lambda: maintenance.control(self.config, self.state, "stop"))
            stop.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)

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


if __name__ == "__main__":
    unittest.main()
