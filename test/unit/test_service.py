"""Focused lifecycle failures; mocked launchctl is not native-service acceptance."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from openmachine import execution, service, tasks
from openmachine.sanitize import Fault


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-service-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = execution.private_directory(self.root / "state")
        execution.private_directory(self.state / "locks")
        execution.private_directory(self.state / "service")
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        self.a, self.b = "a" * 64, "b" * 64
        for identity in (self.a, self.b):
            directory = execution.private_directory(self.state / "service/versions" / identity)
            execution._save(directory / "installation.json", {
                "identity": identity, "version": "0.1.0a7", "protocol": 1, "state_contract": 1})
        self.record = {"format": 1, "config": str(self.config), "probe": {"workspace": "project", "path": "AGENTS.md"},
                       "current": self.a, "previous": None, "pending": None,
                       "launch_python": str(Path(sys._base_executable).resolve()), "path_environment": os.defpath}
        self.save(self.record)

    def save(self, record):
        execution._save(self.state / "service/service.json", record)

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

    def test_state_check_uses_actual_task_reader_and_preserves_bytes(self):
        tasks.save(self.state, "original", 0, "Original task", "Preserve original execution evidence.\n", [])
        paths = [self.state / "tasks/index.json", self.state / "tasks/original/task.md"]
        before = [path.read_bytes() for path in paths]
        result = service.check_state(self.config, self.state)
        self.assertEqual(result["task_records"], 1)
        self.assertEqual([path.read_bytes() for path in paths], before)
        value = json.loads(paths[0].read_text())
        value["format_version"] = 999
        execution._save(paths[0], value)
        self.assert_fault("unsupported_task_format", lambda: service.check_state(self.config, self.state))
        self.assertEqual(json.loads(paths[0].read_text())["format_version"], 999)

    def test_incompatible_receipt_never_becomes_empty_history(self):
        directory = execution.private_directory(self.state / "executions" / ("c" * 32))
        execution._save(directory / "record.json", {"status": "running", "format_version": 999})
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
        with execution._lease(self.state / "locks/lifecycle.lock", shared=True):
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
        execution._save(path, value)
        self.assert_fault("incompatible_runtime", lambda: service._installed(self.state, self.a))

    def test_restart_rejects_incompatible_state_before_stopping_service(self):
        with patch.object(sys, "platform", "darwin"), patch.object(service, "_verify_files"), \
                patch.object(service, "_candidate_state", side_effect=Fault("incompatible_state", "fixture")), \
                patch.object(service, "_stop") as stop, patch.object(service, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service.operate(self.config, self.state, "restart"))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(service._record(self.state), self.record)


if __name__ == "__main__":
    unittest.main()
