"""Core management: install, update, original-operation readback and status."""

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from openmachine.config import TEXT
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from openmachine.service import maintenance, transition
from test.support.service_cases import FIRST, SECOND, ServiceCaseFixture

ENVELOPE = {"management_contract": 1, "capabilities": ["status", "configure", "configure:verify_existing", "install",
                                                       "update", "update:selection_consent", "operation", "continue"]}


class MaintenanceTests(ServiceCaseFixture, unittest.TestCase):
    def test_status_before_and_after_install_names_the_bound_entry(self):
        status = self.manage("status")
        self.assertEqual({key: status[key] for key in status if key != "observed_at_ms"}, ENVELOPE | {
            "status": "not_installed", "current": None, "selection_sha256": None, "product_version": None,
            "product_commit": None, "entry": None})
        operation = self.install()
        self.assertEqual((operation["phase"], operation["outcome"]), ("completed", {"current": self.digest}))
        self.assertEqual(self.spawned, [(FIRST, sys.executable)])
        status = self.manage("status")
        payload_python = self.state / "service/payloads" / self.digest / "python/bin/python3"
        self.assertEqual({key: status[key] for key in status if key not in {"observed_at_ms", "selection_sha256"}}, ENVELOPE | {
            "status": "installed", "current": self.digest, "product_version": "0.2.8", "product_commit": "a" * 40,
            "entry": {"python": str(payload_python.resolve()), "access_python": str(payload_python),
                      "path": str(self.state / "service/manage.py"), "config": str(self.config), "state": str(self.state),
                      "identity": self.digest}})
        self.assertRegex(status["selection_sha256"], r"^[0-9a-f]{64}$")

    def test_update_moves_access_python_and_the_pinned_entry_follows_the_selection(self):
        self.install()
        before = self.manage("status")
        entry = (self.state / "service/manage.py").read_bytes()
        second = self.next_payload()
        accepted = self.manage("update", identity=SECOND, source=self.source, sha256=second,
                               expected_selection_sha256=before["selection_sha256"])
        self.assertEqual(accepted["operation"]["request"]["selection_sha256"], before["selection_sha256"])
        self.assertEqual(self.complete(SECOND)["outcome"], {"current": second})
        after = self.manage("status")
        self.assertEqual((after["current"], after["entry"]["access_python"]),
                         (second, str(self.state / "service/payloads" / second / "python/bin/python3")))
        self.assertEqual({key: after["entry"][key] for key in ("python", "identity", "path")},
                         {key: before["entry"][key] for key in ("python", "identity", "path")})
        self.assertNotEqual(after["selection_sha256"], before["selection_sha256"])
        self.assertEqual((self.state / "service/manage.py").read_bytes(), entry)
        # The entry written at install executes the selected installation's management.
        result = subprocess.run([sys.executable, "-I", after["entry"]["path"], "--contract", "1", "status"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.splitlines(), ["-I", "-m", "openmachine.cli", "--config", str(self.config),
                                                      "--state-dir", str(self.state), "manage", "--contract", "1", "status"])

    def test_update_against_a_stale_selection_is_rejected_without_a_record(self):
        self.install()
        rejected = self.manage("update", identity=SECOND, source=self.source, sha256=self.digest,
                               expected_selection_sha256="0" * 64)
        self.assertEqual({key: rejected[key] for key in ("status", "reason", "admitted", "operation_id")},
                         {"status": "rejected", "reason": "selection_changed", "admitted": False, "operation_id": SECOND})
        self.assertFalse(maintenance._operation_path(self.state, SECOND).exists())

    def test_install_needs_clean_state_and_happens_once(self):
        (self.state / "unrelated").mkdir()
        self.assertEqual(self.manage("install", identity=FIRST, source=self.source, sha256=self.digest)["reason"],
                         "service_adoption_required")
        (self.state / "unrelated").rmdir()
        self.install()
        again = self.manage("install", identity=SECOND, source=self.source, sha256=self.digest)
        self.assertEqual((again["status"], again["reason"]), ("unknown", "service_already_installed"))
        self.assertEqual(self.manage("configure")["reason"], "service_already_installed")

    def test_an_operation_id_reads_back_its_original_record(self):
        accepted = self.manage("install", identity=FIRST, source=self.source, sha256=self.digest)
        replay = self.manage("install", identity=FIRST, source=self.source, sha256="0" * 64)
        self.assertEqual((replay["status"], replay["operation"]), ("continuation_required", accepted["operation"]))
        with lease(self.state / "service/operation.lock"):
            self.assertEqual(self.manage("operation", identity=FIRST)["status"], "busy")
            self.assertEqual(self.manage("status")["status"], "busy")
            self.assertEqual(self.manage("continue", identity=FIRST)["status"], "busy")
        self.assertEqual(self.manage("continue", identity=FIRST)["status"], "accepted")
        self.assertEqual(self.spawned[-1], (FIRST, sys.executable))
        self.assertEqual(self.manage("update", identity=SECOND, source=self.source, sha256=self.digest,
                                     expected_selection_sha256="0" * 64)["status"], "busy")
        # An interrupted effect is never replayed.
        maintenance._save(self.state, self.record(FIRST), "retaining")
        self.assertEqual(self.manage("operation", identity=FIRST)["status"], "recovery_required")
        self.assertEqual(self.manage("continue", identity=FIRST)["reason"], "maintenance_not_continuable")

    def test_a_retained_operation_continues_in_the_incoming_core(self):
        self.manage("install", identity=FIRST, source=self.source, sha256=self.digest)
        with lease(self.state / "service/operation.lock") as lock, \
                patch.object(maintenance.os, "execve", side_effect=Exception("handoff")) as execve, self.assertRaises(Exception):
            maintenance.run_operation(self.state, FIRST, lock.fileno())
        python = self.state / "service/payloads" / self.digest / "python/bin/python3"
        self.assertEqual(execve.call_args.args[:2], (str(python), [str(python), "-I", "-B", "-m", "openmachine.service.maintenance",
                                                                    str(self.state), FIRST, str(execve.call_args.args[1][-1])]))
        self.assertEqual(self.manage("operation", identity=FIRST)["status"], "continuation_required")
        self.manage("continue", identity=FIRST)
        self.assertEqual(self.spawned[-1], (FIRST, str(python)))

    def test_failures_are_rejected_before_activation_and_unverified_after_it(self):
        self.manage("install", identity=FIRST, source=self.source, sha256=self.digest)
        failed = self.complete(FIRST, prepare=Fault("installation_failed", "fixture"))
        self.assertEqual((failed["phase"], failed["reason"]), ("rejected", "installation_failed"))
        self.assertEqual(self.manage("status")["status"], "not_installed")
        self.manage("install", identity=SECOND, source=self.source, sha256=self.digest)
        with patch.object(transition, "register", side_effect=OSError("fixture")):
            lost = self.complete(SECOND)
        self.assertEqual((lost["phase"], lost["reason"]), ("recovery_required", "maintenance_effect_unverified"))

    def test_a_selection_changed_after_admission_is_not_rebased(self):
        self.install()
        status = self.manage("status")
        second = self.next_payload()
        self.manage("update", identity=SECOND, source=self.source, sha256=second,
                    expected_selection_sha256=status["selection_sha256"])
        def prepare(state, retained, sha256):
            prepared = self.prepare(state, retained, sha256)
            transition.select(state, sha256)  # Another writer moved the selection meanwhile.
            return prepared
        changed = self.complete(SECOND, prepare=prepare)
        self.assertEqual((changed["phase"], changed["reason"]), ("rejected", "maintenance_input_changed"))

    def test_configure_status_and_request_boundaries(self):
        os.unlink(self.config)
        self.assertEqual(self.manage("configure", verify_existing=False)["status"], "configured")
        self.assertEqual(self.config.read_text(), TEXT)
        self.assertEqual(self.manage("configure", verify_existing=True)["verified"], str(self.config))
        self.assertEqual(self.manage("configure", verify_existing=False)["status"], "unknown")
        self.assertEqual(maintenance.dispatch(self.config, self.state, "status", contract=2)["status"], "incompatible")
        relative = maintenance.dispatch(self.config.relative_to(self.root), self.state, "status")
        self.assertEqual((relative["status"], relative["reason"]), ("unknown", "invalid_service_path"))
        with patch.object(maintenance.sys, "platform", "linux"):
            self.assertEqual(self.manage("status")["reason"], "unsupported_service_host")
        self.install()
        other = self.root / "other.toml"
        other.write_text(TEXT)
        self.assertEqual(maintenance.dispatch(other, self.state, "status")["reason"], "service_configuration_mismatch")


if __name__ == "__main__":
    unittest.main()
