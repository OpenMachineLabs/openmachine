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
        config = Settings.model_validate({"schema_version": 1,
            "machines": {"local": {"transport": "local", "execution_enabled": True}},
            "github": {"repositories": ["example/project"]}})
        app = create_application(config, self.state)
        issue = ("update", "example/project", "Title", "Body", 1, "closed", "observed-time")
        pull = ("update", "example/project", "Title", "Body", 1, "topic", "main", False,
                "closed", "observed-time", "f" * 40)
        cases = [
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
                    self.assertEqual(app.get_capabilities()["lifecycle"]["state_contract"], 4)
                dispatch.assert_not_called()

                def admitted(*args):
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
                dispatch.assert_called_once_with(*prefix, *arguments)
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
        for contract in (1, 2, 3, 4):
            save_json(path, original | {"state_contract": contract})
            self.assertEqual(service._installed(self.state, self.b)["state_contract"], contract)
            reply = {"protocol": 1, "state_contract": contract}
            with patch.object(service, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")):
                self.assertEqual(service._candidate_state(Path(sys.executable), self.config, self.state, contract), reply)
                self.assert_fault("incompatible_state", lambda: service._candidate_state(
                    Path(sys.executable), self.config, self.state, 1 if contract != 1 else 3))
        for contract in (0, 5, True, "3"):
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

if __name__ == "__main__":
    unittest.main()
