"""Tests for service behavior at the component layer."""

import hashlib
import tomllib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import call, patch
from openmachine import __version__
from openmachine.config import Settings
from openmachine.foundation.state import lease
from openmachine.service import maintenance
from openmachine.service import checks as service_checks
from openmachine.service import runtime as service_runtime
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_machine_catalog_and_unsaved_probe_do_not_start_or_configure_core(self):
        result = self.machine_fixture()
        before = self.config.read_bytes()
        original = sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*"))
        with patch.object(maintenance.runtime, "status", side_effect=AssertionError("Must not require a running service")), \
                patch.object(maintenance.ssh_client, "call", return_value=result) as probe:
            catalog = maintenance.machines(self.config, self.state)
            self.assertEqual([item["name"] for item in catalog["machines"]], ["local"])
            probe.assert_not_called()
            observed = maintenance.probe_machine(self.config, "server-a", alias="prepared-a", worker="/workers/" + "e" * 64 + ".pyz")
            config, name, operation = probe.call_args.args
            self.assertEqual((name, operation), ("server-a", "probe"))
            self.assertFalse(config.machines[name].execution_enabled)
            self.assertFalse(config.machines[name].workspace_write_enabled)
            self.assertFalse(observed["identity_verified"])
            self.assertEqual(observed["os"], "Linux")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(sorted(str(path.relative_to(self.state)) for path in self.state.rglob("*")), original)

    def test_machine_save_uses_original_maintenance_and_preserves_other_configuration(self):
        self.policy_fixture()
        result = self.machine_fixture()
        before = self.config.read_bytes()
        digest = hashlib.sha256(before).hexdigest()
        with patch.object(maintenance.ssh_client, "call", return_value=result) as probe, \
                patch.object(maintenance, "_spawn") as spawn:
            accepted = maintenance.change_machine(self.config, self.state, "machine-add", "1" * 32,
                "server-a", digest, alias="prepared-a", worker="/workers/" + "e" * 64 + ".pyz", pinned="d" * 64)
            record = accepted["operation"]
            self.assertEqual(accepted["status"], "accepted")
            candidate = Path(record["request"]["candidate_config"])
            self.assertTrue(candidate.is_relative_to(self.state / "service/configurations"))
            saved = tomllib.loads(candidate.read_text())
            target = saved["machines"].pop("server-a")
            self.assertEqual(saved, tomllib.loads(before.decode()))
            self.assertEqual((target["ssh_identity"], target["execution_enabled"], target["workspace_write_enabled"]), ("d" * 64, False, False))
            self.assertEqual(self.config.read_bytes(), before)
            self.assertEqual(maintenance._inputs_unchanged(self.state, record), candidate)
            # A lost reply is reconciled by original ID even if later input differs.
            again = maintenance.change_machine(self.config, self.state, "machine-add", "1" * 32,
                "different", "bad", alias="different", worker="bad", pinned="bad")
            self.assertEqual(again["operation"], record)
            spawn.assert_called_once()
            probe.assert_not_called()  # SSH checks run only after durable admission.
        prepared = {"status": "prepared", "identity": self.b, "version": __version__,
                    "payload_sha256": "c" * 64, "runtime": str(self.state / "service/versions" / self.b),
                    "launch_python": sys.executable, "path_environment": os.defpath,
                    "selection": "unchanged", "service": "not_started"}
        maintenance._save(self.state, record, "prepared", prepared=prepared)
        verify, _, start = self.activation_mocks()
        with patch.object(service_runtime, "_sync_definition"), lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        original = maintenance.read_operation(self.config, self.state, record["operation_id"])
        self.assertEqual(original["status"], "completed")
        self.assertEqual(original["operation"]["outcome"]["status"], "stopped")
        self.assertEqual(maintenance.load(self.config).machines["server-a"].ssh_identity, "d" * 64)
        self.assertTrue(any(call.kwargs.get("check_remote") for call in verify.call_args_list))
        start.assert_not_called()

    def test_machine_add_rejects_duplicates_changed_config_and_failed_identity_without_saving(self):
        result = self.machine_fixture()
        digest = maintenance._digest(self.config)
        with patch.object(maintenance, "_spawn") as spawn, patch.object(maintenance.ssh_client, "call", return_value=result) as probe:
            self.assert_fault("duplicate_machine", lambda: maintenance.probe_machine(self.config, "local", alias="prepared", worker="/" + "e" * 64 + ".pyz"))
            self.assert_fault("invalid_machine", lambda: maintenance.probe_machine(self.config, "remote", alias="-option", worker="/" + "e" * 64 + ".pyz"))
            self.assert_fault("configuration_changed", lambda: maintenance.change_machine(self.config, self.state, "machine-add", "1" * 32,
                "remote", "0" * 64, alias="prepared", worker="/" + "e" * 64 + ".pyz", pinned="d" * 64))
            self.assert_fault("identity_required", lambda: maintenance.change_machine(self.config, self.state, "machine-add", "1" * 32,
                "remote", digest, alias="prepared", worker="/" + "e" * 64 + ".pyz", pinned="unconfirmed"))
            probe.assert_not_called()
            spawn.assert_not_called()
        self.assertFalse((self.state / "service/operations").exists())
        with patch.object(maintenance.sys, "platform", "darwin"):
            refused = maintenance.dispatch(self.config, self.state, "machine-add", identity="1" * 32,
                name="remote", expected_config_sha256="0" * 64, alias="prepared", worker="/" + "e" * 64 + ".pyz", pinned="d" * 64)
        self.assertEqual((refused["status"], refused["admitted"], refused["operation_id"]), ("rejected", False, "1" * 32))

    def test_machine_removal_preserves_local_and_refuses_retained_references(self):
        self.machine_fixture()
        self.config.write_text(self.config.read_text() + '\n[machines.remote]\ntransport="ssh"\nssh_alias="prepared"\nssh_worker="/' + "e" * 64 + '.pyz"\nssh_identity="' + "d" * 64 + '"\n')
        config = maintenance.load(self.config)
        digest = maintenance._digest(self.config)
        self.assert_fault("local_machine_required", lambda: maintenance.change_machine(self.config, self.state, "machine-remove", "1" * 32, "local", digest))
        unpinned = config.model_copy(update={"machines": {**config.machines,
            "remote": config.machines["remote"].model_copy(update={"ssh_identity": ""})}})
        self.assert_fault("machine_in_use", lambda: service_checks.machine_unused(unpinned, self.state, "remote"))
        with patch.object(service_checks.tasks, "references_target", return_value=True) as references:
            self.assert_fault("machine_in_use", lambda: service_checks.machine_unused(config, self.state, "remote"))
            references.assert_called_once_with(self.state, "remote", "d" * 64)
        # Use the public settings validator rather than manufacturing a workspace owner.
        config = Settings.model_validate(config.model_dump() | {"workspaces": {"work": {"machine": "remote", "root": "/remote/work"}}})
        self.assert_fault("machine_in_use", lambda: service_checks.machine_unused(config, self.state, "remote"))
        unused = [{"workspace_change_records": 0}, {"executions": [], "unreadable": [], "next_cursor": None}]
        for records in ({"executions": [{"execution_id": "retained"}]}, {"executions": [], "unreadable": ["corrupt"]}):
            with patch.object(maintenance.ssh_client, "call", side_effect=[unused[0], records]):
                self.assert_fault("machine_in_use", lambda: service_checks.machine_unused(maintenance.load(self.config), self.state, "remote"))
        with patch.object(maintenance.ssh_client, "call", side_effect=unused):
            service_checks.machine_unused(maintenance.load(self.config), self.state, "remote")
        with patch.object(maintenance, "_spawn") as spawn, patch.object(maintenance.ssh_client, "call", side_effect=AssertionError("SSH runs after admission")):
            accepted = maintenance.change_machine(self.config, self.state, "machine-remove", "1" * 32, "remote", digest)
            candidate = tomllib.loads(Path(accepted["operation"]["request"]["candidate_config"]).read_text())
            self.assertEqual(set(candidate["machines"]), {"local"})
            self.assertEqual(candidate["skills"]["roots"], ["/retained/skills"])
            spawn.assert_called_once()
        self.assertIn("remote", maintenance.load(self.config).machines)


if __name__ == "__main__":
    unittest.main()
