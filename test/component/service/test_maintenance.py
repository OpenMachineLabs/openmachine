"""Tests for service behavior at the component layer."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from openmachine import tasks
from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import maintenance
from openmachine.service import checks as service_checks
from openmachine.service import processes as service_processes
from openmachine.service import install as service_install
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from openmachine.service import transition as service_transition
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_confirmed_update_preserves_independent_grants_and_unrelated_settings(self):
        self.policy_fixture()
        self.machine_fixture()
        self.config.write_text(self.config.read_text() + '\n[github]\nread_repositories=["example/project"]\n'
            + '\n[workspaces.existing]\nmachine="local"\nroot=' + json.dumps(str(self.root)) + '\n')
        original, selection = self.config.read_bytes(), service_state._record(self.state)
        candidate = self.root / "candidate.toml"
        with patch.object(maintenance, "_spawn") as spawn:
            for index, (commands, writes) in enumerate(((False, False), (False, True), (True, False), (True, True)), 1):
                with self.subTest(commands=commands, writes=writes):
                    candidate.write_text(original.decode().replace('transport="local"',
                        f'transport="local"\nexecution_enabled={str(commands).lower()}\nworkspace_write_enabled={str(writes).lower()}')
                        + '\n[workspaces.project]\nmachine="local"\nroot=' + json.dumps(str(self.root)) + '\n')
                    accepted = maintenance.submit(self.config, self.state, "update", f"{index:032x}", self.root / "payload", "c" * 64,
                        candidate_config=candidate, expected_config_sha256=maintenance._digest(self.config),
                        expected_candidate_sha256=maintenance._digest(candidate))
                    record = accepted["operation"]
                    saved = load(Path(record["request"]["candidate_config"]))
                    baseline = load(self.config)
                    self.assertEqual((saved.machines["local"].execution_enabled,
                                      saved.machines["local"].workspace_write_enabled), (commands, writes))
                    self.assertEqual((saved.github, saved.skills, saved.workspaces["existing"]),
                                     (baseline.github, baseline.skills, baseline.workspaces["existing"]))
                    self.assertEqual(saved.workspaces["project"].root, str(self.root))
                    if commands:
                        saved.machine("local", write=True)
                    else:
                        self.assert_fault("execution_disabled", lambda: saved.machine("local", write=True))
                    if writes:
                        saved.workspace_machine("local")
                    else:
                        self.assert_fault("workspace_write_disabled", lambda: saved.workspace_machine("local"))
                    self.assertEqual(self.config.read_bytes(), original)
                    self.assertEqual(service_state._record(self.state), selection)
                    # No fixture worker was dispatched; settle its admission for the next combination.
                    maintenance._save(self.state, record, "rejected", reason="fixture_not_dispatched")
            self.assertEqual(spawn.call_count, 4)

    def test_confirmed_update_rejects_stale_and_invalid_digests_before_admission(self):
        self.machine_fixture()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(self.config.read_bytes() + b"\n# reviewed candidate\n")
        old, new = maintenance._digest(self.config), maintenance._digest(candidate)
        original = self.config.read_bytes()
        for current, proposed, reason in (("0" * 64, new, "configuration_changed"),
                (old, "0" * 64, "candidate_configuration_changed"),
                ("A" * 64, new, "invalid_configuration_consent"),
                (old, "bad", "invalid_configuration_consent"),
                (None, new, "invalid_configuration_consent"),
                (old, True, "invalid_configuration_consent")):
            with self.subTest(reason=reason, current=current, proposed=proposed), \
                    patch.object(maintenance.sys, "platform", "darwin"), patch.object(maintenance, "_spawn") as spawn:
                result = maintenance.dispatch(self.config, self.state, "update", identity="1" * 32,
                    source=self.root / "payload", sha256="c" * 64, candidate_config=candidate,
                    expected_config_sha256=current, expected_candidate_sha256=proposed)
                self.assertEqual((result["status"], result["admitted"], result["reason"]), ("rejected", False, reason))
                self.assertIn("update:configuration_consent", result["capabilities"])
                self.assertEqual(result["operation_id"], "1" * 32)
                self.assertFalse((self.state / "service/operations").exists())
                self.assertEqual(self.config.read_bytes(), original)
                spawn.assert_not_called()

    def test_confirmed_installation_selection_rejects_same_version_switch_before_admission(self):
        self.machine_fixture()
        installed = service_state._json(self.state / "service/versions" / self.a / "installation.json")
        save_json(self.state / "service/versions" / self.b / "installation.json", installed | {"identity": self.b})
        selection = maintenance._selection(self.state)
        digest = maintenance._digest(self.config)
        inputs = dict(identity="1" * 32, source=self.root / "payload", sha256="c" * 64,
                      expected_config_sha256=digest, expected_candidate_sha256=digest,
                      expected_selection_sha256=selection)
        self.save(self.record | {"current": self.b})
        with patch.object(sys, "platform", "darwin"), patch.object(maintenance, "_spawn") as spawn:
            refused = maintenance.dispatch(self.config, self.state, "update", **inputs)
            self.assertEqual((refused["status"], refused["admitted"], refused["reason"]),
                             ("rejected", False, "selection_changed"))
            self.assertFalse((self.state / "service/operations").exists())
            spawn.assert_not_called()
            self.save(self.record)
            accepted = maintenance.dispatch(self.config, self.state, "update", **inputs)
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["operation"]["request"]["selection_sha256"], selection)
            self.save(self.record | {"current": self.b})
            recovered = maintenance.dispatch(self.config, self.state, "update", **inputs)
            self.assertEqual(recovered["operation"]["operation_id"], inputs["identity"])
            spawn.assert_called_once()
        self.assertNotEqual(self.a, self.b)

    def test_selection_drift_after_preflight_is_rejected_under_admission_lock(self):
        self.machine_fixture()
        digest = maintenance._digest(self.config)
        original = maintenance._selection(self.state)
        def switch(_):
            self.save(self.record | {"current": self.b})
        with patch.object(sys, "platform", "darwin"), \
                patch.object(maintenance, "_available", side_effect=switch), \
                patch.object(maintenance, "_spawn") as spawn:
            result = maintenance.dispatch(self.config, self.state, "update",
                identity="2" * 32, source=self.root / "payload", sha256="c" * 64,
                expected_config_sha256=digest, expected_candidate_sha256=digest,
                expected_selection_sha256=original)
        self.assertEqual((result["status"], result["admitted"], result["reason"]),
                         ("rejected", False, "selection_changed"))
        self.assertFalse((self.state / "service/operations").exists())
        spawn.assert_not_called()

    def test_confirmed_input_loss_or_invalidity_is_rejected_before_admission(self):
        self.machine_fixture()
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        for target, reason in ((self.config, "configuration_changed"), (candidate, "candidate_configuration_changed")):
            for missing in (False, True):
                for during_admission in (False, True):
                    with self.subTest(target=target.name, missing=missing, during_admission=during_admission):
                        self.config.write_bytes(original)
                        candidate.write_bytes(original + b"\n# reviewed candidate\n")
                        old, new = maintenance._digest(self.config), maintenance._digest(candidate)
                        def change():
                            if missing:
                                target.unlink()
                            else:
                                target.write_bytes(b"[invalid TOML")
                        if not during_admission:
                            change()
                        with patch.object(maintenance.sys, "platform", "darwin"), \
                                patch.object(maintenance, "_available", side_effect=lambda state: change() if during_admission else None), \
                                patch.object(maintenance, "_spawn") as spawn:
                            result = maintenance.dispatch(self.config, self.state, "update", identity="1" * 32,
                                source=self.root / "payload", sha256="c" * 64, candidate_config=candidate,
                                expected_config_sha256=old, expected_candidate_sha256=new)
                        self.assertEqual((result["status"], result["admitted"], result["reason"]), ("rejected", False, reason))
                        self.assertFalse((self.state / "service/operations").exists())
                        if target != self.config:
                            self.assertEqual(self.config.read_bytes(), original)
                        spawn.assert_not_called()

    def test_candidate_input_read_failures_do_not_mask_retention_store_failures(self):
        self.machine_fixture()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(self.config.read_bytes())
        digest = maintenance._digest(candidate)
        failures = (
            (FileNotFoundError(2, "missing input", str(candidate)), True),
            (OSError(5, "input read failed", str(candidate)), True),
            (OSError(28, "retention store full", str(self.state / "service/configurations")), False),
            (maintenance.Fault("configuration_unavailable", "Retained snapshot is invalid."), False),
        )
        for error, rejected in failures:
            with self.subTest(error=type(error).__name__, rejected=rejected), \
                    patch.object(maintenance.sys, "platform", "darwin"), \
                    patch.object(service_state, "_snapshot_configuration", side_effect=error), \
                    patch.object(maintenance, "_spawn") as spawn:
                result = maintenance.dispatch(self.config, self.state, "update", identity="1" * 32,
                    source=self.root / "payload", sha256="c" * 64, candidate_config=candidate,
                    expected_config_sha256=digest, expected_candidate_sha256=digest)
                if rejected:
                    self.assertEqual((result["status"], result["admitted"], result["reason"]),
                                     ("rejected", False, "candidate_configuration_changed"))
                else:
                    self.assertEqual(result["status"], "unknown")
                    self.assertNotIn("admitted", result)
                self.assertFalse((self.state / "service/operations").exists())
                self.assertEqual(maintenance._digest(self.config), digest)
                spawn.assert_not_called()

    def test_confirmed_update_rechecks_active_config_inside_admission_lock(self):
        self.machine_fixture()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(self.config.read_bytes())
        digest = maintenance._digest(self.config)
        changed = self.config.read_bytes() + b"\n# concurrent edit\n"
        with patch.object(maintenance, "_available", side_effect=lambda state: self.config.write_bytes(changed)), \
                patch.object(maintenance, "_spawn") as spawn:
            self.assert_fault("configuration_changed", lambda: maintenance.submit(self.config, self.state, "update", "1" * 32,
                self.root / "payload", "c" * 64, candidate_config=candidate,
                expected_config_sha256=digest, expected_candidate_sha256=digest))
            spawn.assert_not_called()
        self.assertEqual(self.config.read_bytes(), changed)
        self.assertFalse((self.state / "service/operations").exists())

    def test_confirmed_update_rejects_candidate_changed_during_retention(self):
        self.machine_fixture()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(self.config.read_bytes())
        digest = maintenance._digest(candidate)
        snapshot = service_state._snapshot_configuration
        def changed(state, path):
            path.write_bytes(path.read_bytes() + b"\n# changed after confirmation\n")
            return snapshot(state, path)
        with patch.object(service_state, "_snapshot_configuration", side_effect=changed), patch.object(maintenance, "_spawn") as spawn:
            self.assert_fault("candidate_configuration_changed", lambda: maintenance.submit(self.config, self.state, "update", "1" * 32,
                self.root / "payload", "c" * 64, candidate_config=candidate,
                expected_config_sha256=digest, expected_candidate_sha256=digest))
            spawn.assert_not_called()
        self.assertEqual(maintenance._digest(self.config), digest)
        self.assertFalse((self.state / "service/operations").exists())

    def test_confirmed_update_freezes_candidate_and_reads_original_after_lost_reply(self):
        self.machine_fixture()
        candidate = self.root / "candidate.toml"
        reviewed = self.config.read_bytes() + b"\n# reviewed candidate\n"
        candidate.write_bytes(reviewed)
        with patch.object(maintenance, "_spawn", side_effect=OSError("not launched")) as spawn:
            record = maintenance.submit(self.config, self.state, "update", "1" * 32, self.root / "payload", "c" * 64,
                candidate_config=candidate, expected_config_sha256=maintenance._digest(self.config),
                expected_candidate_sha256=maintenance._digest(candidate))["operation"]
            frozen = Path(record["request"]["candidate_config"])
            self.assertEqual(frozen.read_bytes(), reviewed)
            self.assertTrue(frozen.is_relative_to(self.state / "service/configurations"))
            candidate.write_bytes(reviewed + b"\n# caller edited its draft later\n")
            self.assertEqual(maintenance._inputs_unchanged(self.state, record), frozen)
            again = maintenance.submit(self.config, self.state, "update", "1" * 32, Path("absent"), "invalid",
                expected_config_sha256="invalid", expected_candidate_sha256="invalid")
            self.assertEqual((again["status"], again["operation"]), ("continuation_required", record))
            self.assertEqual(record["reason"], "maintenance_launch_unavailable")
            self.assertEqual(maintenance.read_operation(self.config, self.state, "1" * 32)["operation"], record)
            spawn.assert_called_once()

    def test_confirmed_update_keeps_uncertain_publication_unknown(self):
        self.machine_fixture()
        digest = maintenance._digest(self.config)
        save = maintenance._save
        def lost(*args, **kwargs):
            save(*args, **kwargs)
            raise OSError("lost acknowledgement after publication")
        with patch.object(maintenance.sys, "platform", "darwin"), patch.object(maintenance, "_save", side_effect=lost), \
                patch.object(maintenance, "_spawn") as spawn:
            result = maintenance.dispatch(self.config, self.state, "update", identity="1" * 32,
                source=self.root / "payload", sha256="c" * 64,
                expected_config_sha256=digest, expected_candidate_sha256=digest)
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("admitted", result)
            spawn.assert_not_called()
        original = maintenance.read_operation(self.config, self.state, "1" * 32)
        self.assertEqual(original["operation"]["request"]["config_sha256"], digest)
        self.assertEqual(maintenance._digest(self.config), digest)

    def test_confirmed_grant_revocation_activates_without_clearing_stop(self):
        self.policy_fixture()
        self.machine_fixture()
        self.config.write_text(self.config.read_text().replace('transport="local"',
            'transport="local"\nexecution_enabled=true\nworkspace_write_enabled=true'))
        original_policy = service_state._record(self.state)["policy"]
        candidate = self.root / "candidate.toml"
        candidate.write_text(self.config.read_text().replace('enabled=true', 'enabled=false'))
        expected = candidate.read_bytes()
        with patch.object(maintenance, "_spawn"):
            record = maintenance.submit(self.config, self.state, "update", "1" * 32, self.root / "payload", "c" * 64,
                candidate_config=candidate, expected_config_sha256=maintenance._digest(self.config),
                expected_candidate_sha256=maintenance._digest(candidate))["operation"]
        prepared = {"status": "prepared", "identity": self.b, "version": "0.1.0a7", "payload_sha256": "c" * 64,
                    "runtime": str(self.state / "service/versions" / self.b), "launch_python": sys.executable,
                    "path_environment": os.defpath, "selection": "unchanged", "service": "not_started"}
        maintenance._save(self.state, record, "prepared", prepared=prepared)
        _, _, start = self.activation_mocks()
        with patch.object(service_runtime, "_sync_definition"), lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        original = maintenance.read_operation(self.config, self.state, record["operation_id"])
        self.assertEqual((original["status"], original["operation"]["outcome"]["status"]), ("completed", "stopped"))
        self.assertEqual(self.config.read_bytes(), expected)
        self.assertEqual(service_state._record(self.state)["policy"], original_policy)
        config = load(self.config)
        self.assert_fault("execution_disabled", lambda: config.machine("local", write=True))
        self.assert_fault("workspace_write_disabled", lambda: config.workspace_machine("local"))
        start.assert_not_called()

    def test_status_separates_selected_and_running_commits(self):
        for identity, commit in ((self.a, "c" * 40), (self.b, "d" * 40)):
            path = service_state._runtime(self.state, identity) / "installation.json"
            save_json(path, service_state._json(path) | {"commit": commit})
        with patch.object(sys, "platform", "darwin"), \
                patch.object(service_runtime, "_verify_files"), \
                patch.object(service_runtime, "_native", return_value={"loaded": False, "pid": None}), \
                patch.object(service_runtime, "_startup", return_value={"enabled": False}), \
                patch.object(service_state, "running_commit", return_value="e" * 40):
            for identity, commit in ((self.a, "c" * 40), (self.b, "d" * 40)):
                self.save(self.record | {"current": identity})
                result = maintenance.dispatch(self.config, self.state, "status")
                self.assertEqual(result["status"], "stopped")
                self.assertEqual((result["version"], result["product_version"]), ("0.1.0a7", "0.1.0a7"))
                self.assertEqual((result["commit"], result["product_commit"]), (commit, commit))
                self.assertEqual(result["implementation_commit"], "e" * 40)
                self.assertEqual(result["selection_sha256"], maintenance._selection(self.state))

    def test_existing_remote_workspace_cannot_silently_rebind_ssh_identity(self):
        from openmachine.config import Settings
        original = Settings.model_validate({"schema_version": 1,
            "machines": {"remote": {"transport": "ssh", "ssh_alias": "owned-ssh",
                                    "ssh_worker": "/home/fixture/" + "c" * 64 + ".pyz",
                                    "ssh_identity": "a" * 64}},
            "workspaces": {"remote-project": {"machine": "remote", "root": "/home/fixture/project"}}})
        candidate = original.model_copy(deep=True)
        candidate.machines["remote"].ssh_identity = "b" * 64
        self.assert_fault("workspace_reference_unavailable", lambda: service_checks.workspace_bindings_safe(
            self.state, original, candidate, "local-probe"))

    def test_activation_refuses_protected_workspace_before_stopping_core(self):
        self.policy_fixture()
        self.machine_fixture()
        _, stop, _ = self.activation_mocks()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(self.config.read_bytes())
        with patch.object(service_checks, "workspace_bindings_safe",
                          side_effect=Fault("workspace_in_use", "Original workspace retained.")) as guard:
            self.assert_fault("workspace_in_use", lambda: service_transition._activate(
                self.state, self.record, self.b, candidate, require_safe=True))
        guard.assert_called_once()
        stop.assert_not_called()
        self.assertEqual(service_state._record(self.state)["current"], self.a)

    def test_workspace_binding_guard_allows_unreferenced_local_lifecycle(self):
        from openmachine.config import load
        self.machine_fixture()
        first, second = self.root / "original", self.root / "replacement"
        first.mkdir()
        second.mkdir()
        self.config.write_text(self.config.read_text() + "\n[workspaces.existing]\nmachine=\"local\"\nroot="
                               + json.dumps(str(first)) + "\n")
        original = load(self.config)
        for action in ("rename", "replace", "remove"):
            candidate = original.model_copy(deep=True)
            if action == "rename":
                candidate.workspaces["renamed"] = candidate.workspaces.pop("existing")
            elif action == "replace":
                candidate.workspaces["existing"].root = str(second)
            else:
                del candidate.workspaces["existing"]
            with self.subTest(action=action):
                service_checks.workspace_bindings_safe(self.state, original, candidate, "project")
        renamed = original.model_copy(deep=True)
        renamed.workspaces["renamed"] = renamed.workspaces.pop("existing")
        self.assert_fault("workspace_in_use", lambda: service_checks.workspace_bindings_safe(
            self.state, original, renamed, "existing"))
        with patch.object(tasks, "references_workspace", return_value=True):
            self.assert_fault("workspace_in_use", lambda: service_checks.workspace_bindings_safe(
                self.state, original, renamed, "project"))

    def ledger_operation_fixture(self):
        record = self.operation_fixture()
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 18,
                  "management_contract": 1, "payload_sha256": record["request"]["payload_sha256"]})
        storage = {"kind": "legacy", "fenced": False, "active": False}
        def migrate(state):
            storage.update(kind="sqlite", fenced=True, active=True)
            return storage
        migration = self.enterContext(patch.object(tasks, "migrate", side_effect=migrate))
        self.enterContext(patch.object(tasks, "storage_state", side_effect=lambda state: dict(storage)))
        self.enterContext(patch.object(maintenance, "_bound"))
        self.enterContext(patch.object(service_checks, "activation_safe"))
        self.enterContext(patch.object(service_runtime, "socket_alive", return_value=False))
        self.enterContext(patch.object(service_state, "_bind_management", side_effect=lambda state, selected, identity: selected))
        _, stop, start = self.activation_mocks()
        return record, storage, migration, stop, start

    def test_sqlite_cutover_continues_only_original_candidate_without_repreparing(self):
        record, storage, migration, stop, start = self.ledger_operation_fixture()
        start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
        with patch.object(service_install, "_prepare_payload") as prepare:
            with lease(self.state / "service/operation.lock") as lock:
                maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
            observed = maintenance.read_operation(self.config, self.state, record["operation_id"])
            self.assertEqual((observed["status"], observed["operation"]["phase"]),
                             ("continuation_required", "recovery_required"))
            with patch.object(maintenance, "_spawn") as spawn, patch.object(maintenance.payload, "verify"):
                accepted = maintenance.continue_operation(self.config, self.state, record["operation_id"])
            self.assertEqual(accepted["operation"]["operation_id"], record["operation_id"])
            spawn.assert_called_once()
            with lease(self.state / "service/operation.lock") as lock:
                maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
            prepare.assert_not_called()
        completed = maintenance._read(self.state, record["operation_id"])
        self.assertEqual((completed["phase"], completed["request"]), ("completed", record["request"]))
        self.assertEqual([item.args[1]["current"] for item in start.call_args_list], [self.b, self.b])
        self.assertEqual((migration.call_count, stop.call_count), (2, 2))
        self.assertTrue(storage["active"])

    def test_sqlite_readiness_commit_loss_is_observed_without_restart(self):
        record, _, migration, stop, start = self.ledger_operation_fixture()
        original_commit = service_transition._commit_ready
        def lost_acknowledgement(state, selected):
            original_commit(state, selected)
            raise OSError("readiness acknowledgement lost")
        with patch.object(service_transition, "_commit_ready", side_effect=lost_acknowledgement), \
                lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        self.assertIsNone(service_state._record(self.state)["pending"])
        self.assertEqual(maintenance.read_operation(self.config, self.state, record["operation_id"])["status"], "continuation_required")
        with patch.object(service_runtime, "status", return_value={"status": "ready", "current": self.b}) as observe, \
                lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        observe.assert_called_once()
        self.assertEqual((migration.call_count, stop.call_count, start.call_count), (1, 1, 1))
        self.assertEqual(maintenance._read(self.state, record["operation_id"])["phase"], "completed")

    def test_sqlite_continuation_rejects_changed_intent_or_unfenced_authority(self):
        record, storage, _, _, start = self.ledger_operation_fixture()
        start.side_effect = Fault("service_unready", "fixture")
        with lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        selected = service_state._record(self.state)
        original = maintenance._operation_path(self.state, record["operation_id"]).read_bytes()
        cases = [(selected | {"probe": selected["probe"] | {"path": "changed.txt"}}, True, False),
                 (selected | {"pending": selected["pending"] | {"from": "e" * 64}}, True, False),
                 (selected, False, False), (selected, True, True)]
        for changed, fenced, restore_required in cases:
            with self.subTest(fenced=fenced, restore_required=restore_required, selection=changed):
                self.save(changed)
                storage.update(fenced=fenced, restore_required=restore_required)
                with patch.object(maintenance, "_spawn") as spawn:
                    self.assert_fault("maintenance_not_continuable", lambda: maintenance.continue_operation(
                        self.config, self.state, record["operation_id"]))
                    spawn.assert_not_called()
                self.assertEqual(maintenance._operation_path(self.state, record["operation_id"]).read_bytes(), original)

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

    def test_resume_preserves_stop_login_and_recovery_without_new_intent(self):
        record = self.policy_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        record = service_state._record(self.state)
        with patch.object(service_runtime, "_verify_files"), patch.object(service_checks, "_candidate_state"), \
                patch.object(maintenance, "observe", return_value={}), \
                patch.object(service_runtime, "_start") as start:
            maintenance.control(self.config, self.state, "resume")
            start.assert_not_called()
            self.assertEqual(service_state._record(self.state), record)
            running = record | {"policy": {"desired": "running", "start_at_login": False}}
            self.save(running)
            maintenance.control(self.config, self.state, "resume")
            start.assert_called_once_with(self.state, running)
            self.assertEqual(service_state._record(self.state), running)
            self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)
            start.reset_mock()
            # Legacy records have no persisted running intent.
            legacy = dict(record, format=1)
            legacy.pop("policy")
            self.save(legacy)
            maintenance.control(self.config, self.state, "resume")
            start.assert_not_called()

    def test_retained_management_survives_selected_legacy_installation(self):
        # A valid old payload anchor stays authoritative even when the selected
        # newer Core was installed through the earlier wheel-only updater.
        anchor = self.state / "service/versions" / self.a / "installation.json"
        save_json(anchor, service_state._json(anchor) | {
            "state_contract": 14, "management_contract": 1, "payload_sha256": "d" * 64})
        python = self.state / "service/payloads" / ("d" * 64) / "python/bin/python3"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        bound = service_state._bind_management(self.state, self.record, self.a)
        self.save(bound)
        retained_entry = (self.state / "service/manage.py").read_bytes()
        selected = self.state / "service/versions" / self.b / "installation.json"
        save_json(selected, service_state._json(selected) | {
            "version": "0.2.13", "state_contract": 16})
        upgraded = bound | {"current": self.b, "previous": self.a}
        self.save(upgraded)
        self.assertEqual(service_state._bind_management(self.state, upgraded, self.b), upgraded)
        self.assertEqual((self.state / "service/manage.py").read_bytes(), retained_entry)
        with patch.object(service_runtime, "_verify_files"), \
                patch.object(service_checks, "_candidate_state"), \
                patch.object(service_runtime, "_stop") as stop, \
                patch.object(service_runtime, "_sync_definition"), \
                patch.object(maintenance, "observe", return_value={"status": "stopped"}), \
                patch.object(service_runtime, "_startup", return_value={"enabled": True}):
            observed = maintenance.control(self.config, self.state, "stop")
        self.assertEqual(observed["status"], "stopped")
        self.assertEqual(service_state._record(self.state)["policy"],
                         {"desired": "stopped", "start_at_login": True})
        self.assertEqual(service_state._record(self.state)["management"], bound["management"])
        self.assertEqual((self.state / "service/manage.py").read_bytes(), retained_entry)
        stop.assert_called_once_with(self.state)

    def test_first_management_binding_still_requires_payload_authority(self):
        selected = self.state / "service/versions" / self.a / "installation.json"
        save_json(selected, service_state._json(selected) | {
            "version": "0.2.13", "state_contract": 16, "management_contract": 1})
        self.assert_fault("incoming_runtime_required",
                          lambda: service_state._bind_management(self.state, self.record, self.a))
        self.assertFalse((self.state / "service/manage.py").exists())
        self.assertEqual(service_state._record(self.state), self.record)

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
        with patch.object(maintenance, "_spawn") as spawn, patch.object(service_processes, "run") as run:
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

    def test_deferred_update_retirement_is_terminal_idempotent_and_preserves_selection(self):
        record = self.operation_fixture("deferred")
        before = service_state._record(self.state)
        with patch.object(maintenance, "_bound"), \
                patch.object(maintenance, "observe", return_value={"status": "ready", "current": self.a}):
            result = maintenance.retire_operation(self.config, self.state, record["operation_id"])
            repeated = maintenance.retire_operation(self.config, self.state, record["operation_id"])
        self.assertEqual((result["status"], result["operation"]["phase"], result["operation"]["reason"]),
                         ("retired", "rejected", "retired_before_activation"))
        self.assertEqual(repeated, result)
        self.assertEqual(service_state._record(self.state), before)
        original = maintenance.read_operation(self.config, self.state, record["operation_id"])
        self.assertEqual(original["status"], "rejected")
        maintenance._available(self.state)

    def test_deferred_update_retirement_waits_only_for_lifecycle_acquisition(self):
        record = self.operation_fixture("deferred")
        lifecycle = self.state / "locks/lifecycle.lock"
        attempts = 0

        class BusyLease:
            def __enter__(self):
                raise Fault("resource_busy", "fixture")

            def __exit__(self, *args):
                return False

        def controlled(path, **kwargs):
            nonlocal attempts
            if path != lifecycle:
                return lease(path, **kwargs)
            attempts += 1
            return BusyLease() if attempts < 3 else lease(path, **kwargs)

        with patch.object(maintenance, "lease", side_effect=controlled), \
                patch.object(maintenance.time, "monotonic", side_effect=[0.0, 0.1, 0.2]), \
                patch.object(maintenance.time, "sleep") as sleep, patch.object(maintenance, "_bound"), \
                patch.object(maintenance, "observe", return_value={"status": "ready", "current": self.a}) as observe:
            result = maintenance.retire_operation(self.config, self.state, record["operation_id"])
        self.assertEqual((result["status"], result["operation"]["phase"]), ("retired", "rejected"))
        self.assertEqual(attempts, 3)
        self.assertEqual(sleep.call_count, 2)
        observe.assert_called_once()

    def test_deferred_update_retirement_contention_budget_has_no_effect(self):
        record = self.operation_fixture("deferred")
        path = maintenance._operation_path(self.state, record["operation_id"])
        before = path.read_bytes()
        lifecycle = self.state / "locks/lifecycle.lock"

        class BusyLease:
            def __enter__(self):
                raise Fault("resource_busy", "fixture")

            def __exit__(self, *args):
                return False

        def controlled(path, **kwargs):
            return BusyLease() if path == lifecycle else lease(path, **kwargs)

        with patch.object(maintenance, "lease", side_effect=controlled), \
                patch.object(maintenance.time, "monotonic", side_effect=[0.0, 5.0]), \
                patch.object(maintenance.time, "sleep") as sleep, patch.object(maintenance, "_bound"), \
                patch.object(maintenance, "observe") as observe:
            self.assert_fault("resource_busy",
                              lambda: maintenance.retire_operation(self.config, self.state, record["operation_id"]))
        sleep.assert_not_called()
        observe.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_deferred_update_retirement_does_not_replay_body_failure(self):
        record = self.operation_fixture("deferred")
        path = maintenance._operation_path(self.state, record["operation_id"])
        before = path.read_bytes()
        lifecycle = self.state / "locks/lifecycle.lock"
        attempts = 0

        class BusyLease:
            def __enter__(self):
                raise Fault("resource_busy", "fixture")

            def __exit__(self, *args):
                return False

        def controlled(path, **kwargs):
            nonlocal attempts
            if path != lifecycle:
                return lease(path, **kwargs)
            attempts += 1
            return BusyLease() if attempts == 1 else lease(path, **kwargs)

        with patch.object(maintenance, "lease", side_effect=controlled), \
                patch.object(maintenance.time, "monotonic", side_effect=[0.0, 0.1]), \
                patch.object(maintenance.time, "sleep"), patch.object(maintenance, "_bound"), \
                patch.object(maintenance, "observe", side_effect=Fault("maintenance_retirement_unverified", "fixture")) as observe:
            self.assert_fault("maintenance_retirement_unverified",
                              lambda: maintenance.retire_operation(self.config, self.state, record["operation_id"]))
        self.assertEqual(attempts, 2)
        observe.assert_called_once()
        self.assertEqual(path.read_bytes(), before)

    def test_deferred_update_retirement_requires_deliberate_verified_stop(self):
        selected = self.policy_fixture()
        cases = [("running", False, None, False), ("stopped", True, None, False),
                 ("stopped", False, 123, False), ("stopped", False, None, True)]
        for desired, socket_alive, pid, allowed in cases:
            with self.subTest(desired=desired, socket_alive=socket_alive, pid=pid):
                self.save(selected | {"policy": selected["policy"] | {"desired": desired}})
                record = self.operation_fixture("deferred")
                path = maintenance._operation_path(self.state, record["operation_id"])
                before = path.read_bytes()
                observed = {"status": "stopped", "current": self.a, "loaded": False,
                            "pid": pid, "supervisor_pid": None}
                with patch.object(maintenance, "_bound"), \
                        patch.object(service_runtime, "status", return_value=observed), \
                        patch.object(service_runtime, "_startup", return_value={"enabled": False}), \
                        patch.object(service_runtime, "socket_alive", return_value=socket_alive):
                    if allowed:
                        result = maintenance.retire_operation(self.config, self.state, record["operation_id"])
                        self.assertEqual((result["status"], result["operation"]["phase"]), ("retired", "rejected"))
                    else:
                        self.assert_fault("maintenance_retirement_unverified",
                            lambda: maintenance.retire_operation(self.config, self.state, record["operation_id"]))
                        self.assertEqual(path.read_bytes(), before)
                self.assertEqual(service_state._record(self.state)["current"], self.a)
                self.assertEqual(service_state._record(self.state)["pending"], None)

    def test_deferred_update_retirement_rejects_changed_active_or_uncertain_state(self):
        record = self.operation_fixture("prepared")
        self.assert_fault("maintenance_not_retirable",
                          lambda: maintenance.retire_operation(self.config, self.state, record["operation_id"]))

        record = self.operation_fixture("deferred")
        with patch.object(maintenance, "_bound"), patch.object(maintenance, "_selection", return_value="0" * 64):
            self.assert_fault("maintenance_input_changed",
                              lambda: maintenance.retire_operation(self.config, self.state, record["operation_id"]))

        record = self.operation_fixture("deferred")
        selected = service_state._record(self.state)
        with patch.object(service_state, "_record", return_value=selected | {"pending": {"fixture": True}}), \
                patch.object(maintenance, "_bound"), \
                patch.object(maintenance, "_selection", return_value=record["request"]["selection_sha256"]):
            self.assert_fault("activation_pending",
                              lambda: maintenance.retire_operation(self.config, self.state, record["operation_id"]))

        record = self.operation_fixture("deferred")
        with patch.object(maintenance, "_bound"), \
                patch.object(maintenance, "observe", return_value={"status": "unready", "current": self.a}):
            self.assert_fault("maintenance_retirement_unverified",
                              lambda: maintenance.retire_operation(self.config, self.state, record["operation_id"]))

        record = self.operation_fixture("deferred")
        path = maintenance._operation_path(self.state, record["operation_id"])
        corrupted = record | {"prepared": record["prepared"] | {"service": "started"}}
        save_json(path, corrupted)
        before = path.read_bytes()
        self.assert_fault("maintenance_state_invalid",
                          lambda: maintenance.retire_operation(self.config, self.state, record["operation_id"]))
        self.assertEqual(path.read_bytes(), before)

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
                patch.object(service_processes, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof), "")), \
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
