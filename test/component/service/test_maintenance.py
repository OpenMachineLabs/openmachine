"""Core management: install, update, original-operation readback and status."""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from openmachine.config import TEXT
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, save_json
from openmachine.service import maintenance, transition
from openmachine.service import state as service_state
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

    def test_install_and_update_refuse_an_invalid_configuration(self):
        self.config.write_text("schema_version = 1\n" + TEXT)
        self.assertEqual(self.manage("install", identity=FIRST, source=self.source, sha256=self.digest)["reason"], "invalid_config")
        self.assertFalse(maintenance._operation_path(self.state, FIRST).exists())

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
        # Another reader's probe is no owner: concurrent status and readback do not report each other as busy.
        with lease(self.state / "service/operation.lock", shared=True):
            self.assertEqual(self.manage("operation", identity=FIRST)["status"], "continuation_required")
            self.assertEqual(self.manage("status")["status"], "not_installed")
        self.assertEqual(self.manage("continue", identity=FIRST)["status"], "accepted")
        self.assertEqual(self.spawned[-1], (FIRST, sys.executable))
        self.assertEqual(self.manage("update", identity=SECOND, source=self.source, sha256=self.digest,
                                     expected_selection_sha256="0" * 64)["status"], "busy")
        # An effect that may have happened is never replayed.
        maintenance._save(self.state, self.record(FIRST), "recovery_required", reason="fixture")
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
        self.assertEqual((lost["phase"], lost["reason"]), ("activating", "maintenance_effect_unverified"))
        self.assertEqual(self.manage("operation", identity=SECOND)["status"], "continuation_required")
        # An activation is read back by its own process, never ended.
        self.assertEqual(self.manage("continue", identity=SECOND)["status"], "accepted")
        self.assertEqual((self.record(SECOND)["phase"], self.spawned[-1][0]), ("activating", SECOND))

    def test_continue_settles_an_interrupted_activation_from_the_selection(self):
        self.install()
        second = self.next_payload()
        selection = self.manage("status")["selection_sha256"]
        self.manage("update", identity=SECOND, source=self.source, sha256=second, expected_selection_sha256=selection)
        # The process died after recording activation, before the selection moved: activation runs once.
        with patch.object(transition, "select", side_effect=SystemExit), self.assertRaises(SystemExit):
            self.complete(SECOND)
        self.assertEqual(self.manage("operation", identity=SECOND)["status"], "continuation_required")
        with lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, SECOND, lock.fileno())
        self.assertEqual((self.record(SECOND)["phase"], self.manage("status")["current"]), ("completed", second))
        # It died after the selection moved: continuing completes the record and never selects again.
        record = maintenance._save(self.state, self.record(SECOND), "activating", outcome=None)
        with patch.object(transition, "select") as select, lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, SECOND, lock.fileno())
        select.assert_not_called()
        self.assertEqual(self.record(SECOND)["phase"], "completed")
        # Another writer moved the selection elsewhere: the operation is not rebased.
        maintenance._save(self.state, record, "activating", outcome=None)
        with patch.object(maintenance, "_activated", return_value=False), lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, SECOND, lock.fileno())
        self.assertEqual((self.record(SECOND)["phase"], self.record(SECOND)["reason"]), ("recovery_required", "maintenance_input_changed"))

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

    def test_the_pinned_entry_refuses_a_changed_binding(self):
        self.install()
        record = service_state._json(self.state / "service/service.json")
        for change in ({"config": str(self.root / "other.toml")}, {"management": record["management"] | {"identity": "f" * 64}}):
            with self.subTest(change=change):
                save_json(self.state / "service/service.json", record | change)
                result = subprocess.run([sys.executable, "-I", str(self.state / "service/manage.py"), "status"],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual((result.returncode, json.loads(result.stdout)["reason"]),
                                 (1, "management_identity_unavailable"))

    def test_operation_ids_and_the_store_are_bounded(self):
        for identity in ("../other", "A" * 32, "1" * 31):
            with self.subTest(identity=identity):
                self.assertEqual(self.manage("operation", identity=identity)["reason"], "invalid_operation_id")
        self.manage("install", identity=FIRST, source=self.source, sha256=self.digest)
        maintenance._save(self.state, self.record(FIRST), "rejected", reason="fixture")
        with patch.object(maintenance, "MAX_OPERATIONS", 1):
            self.assertEqual(self.manage("install", identity=SECOND, source=self.source, sha256=self.digest)["reason"],
                             "maintenance_store_full")
        self.assertFalse(maintenance._operation_path(self.state, SECOND).exists())

    def test_invalid_selection_and_installation_records_are_never_replaced(self):
        self.install()
        selection, installation = self.state / "service/service.json", self.state / "service/versions" / self.digest / "installation.json"
        good = {path: service_state._json(path) for path in (selection, installation)}
        cases = ((selection, {"current": "../x"}, "service_state_invalid"), (selection, {"extra": 1}, "service_state_invalid"),
                 (installation, {"commit": "x"}, "incompatible_runtime"), (installation, {"extra": 1}, "incompatible_runtime"),
                 (installation, {"management_contract": 2}, "incompatible_runtime"))
        for path, change, code in cases:
            with self.subTest(path=path.name, change=change):
                save_json(path, good[path] | change)
                before = path.read_bytes()
                self.assertEqual(self.manage("status")["reason"], code)
                self.assertEqual(path.read_bytes(), before)
                save_json(path, good[path])
        self.assertEqual(self.manage("status")["status"], "installed")

    def test_configure_status_and_request_boundaries(self):
        os.unlink(self.config)
        self.assertEqual(self.manage("configure", verify_existing=False)["status"], "configured")
        self.assertEqual(self.config.read_text(), TEXT)
        self.assertEqual(self.manage("configure", verify_existing=True)["verified"], str(self.config))
        self.assertEqual(self.manage("configure", verify_existing=False)["status"], "unknown")
        self.assertEqual(maintenance.dispatch(self.config, self.state, "status", contract=2)["status"], "incompatible")
        relative = maintenance.dispatch(self.config.relative_to(self.root), self.state, "status")
        self.assertEqual((relative["status"], relative["reason"]), ("unknown", "invalid_service_path"))
        for platform, uid in (("darwin", 0), ("win32", 501)):
            with self.subTest(platform=platform), patch.object(maintenance.sys, "platform", platform), \
                    patch.object(maintenance.os, "getuid", return_value=uid):
                self.assertEqual(self.manage("status")["reason"], "unsupported_service_host")
        with patch.object(maintenance.sys, "platform", "linux"), patch.object(maintenance.os, "getuid", return_value=0):
            self.assertEqual(maintenance.dispatch(self.config, self.state, "status")["status"], "not_installed")
        self.install()
        other = self.root / "other.toml"
        other.write_text(TEXT)
        self.assertEqual(maintenance.dispatch(other, self.state, "status")["reason"], "service_configuration_mismatch")



class RetentionTests(ServiceCaseFixture, unittest.TestCase):
    """An unattended machine updates for years: finished work is pruned, interrupted work can end."""

    def update(self, identity, notices, complete=True):
        (self.source / "THIRD_PARTY_NOTICES.md").write_text(notices)
        digest = self.seal()
        accepted = self.manage("update", identity=identity, source=self.source, sha256=digest,
                               expected_selection_sha256=self.manage("status")["selection_sha256"])
        self.assertEqual(accepted["status"], "accepted", accepted)
        if complete:
            self.assertEqual(self.complete(identity)["outcome"], {"current": digest})
        return digest

    def kept(self, name):
        return {path.name for path in (self.state / "service" / name).iterdir()}

    def test_updates_keep_the_current_and_previous_installations_and_the_entrys_payload(self):
        first = self.install()["outcome"]["current"]
        second = self.update(SECOND, "second\n")
        third = self.update("3" * 32, "third\n")
        self.assertEqual((self.kept("versions"), self.kept("payloads")), ({second, third}, {first, second, third}))
        self.assertEqual(self.manage("status")["current"], third)
        fourth = self.update("4" * 32, "fourth\n")
        self.assertEqual((self.kept("versions"), self.kept("payloads")), ({third, fourth}, {first, third, fourth}))
        # The entry written at install still runs from the first payload and executes the selection.
        result = subprocess.run([sys.executable, "-I", str(self.state / "service/manage.py"), "status"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.splitlines()[0], "-I")

    def test_only_the_newest_finished_records_and_the_previous_selections_are_kept(self):
        self.install()
        self.update(SECOND, "second\n")
        rejected = self.update("3" * 32, "third\n", complete=False)
        self.complete("3" * 32, prepare=Fault("installation_failed", "fixture"))
        with patch.object(maintenance, "KEPT_OPERATIONS", 1):
            self.update("4" * 32, "fourth\n")
        # The newest record, and SECOND's, which made the previous selection; the rejected one is gone too.
        self.assertEqual(self.kept("operations"), {"4" * 32 + ".json", SECOND + ".json"})
        self.assertNotIn(rejected, self.kept("versions") | self.kept("payloads"))

    def test_rejected_updates_are_pruned_and_never_fill_the_store(self):
        first = self.install()["outcome"]["current"]
        for number in range(2, maintenance.MAX_OPERATIONS + 1):
            self.update(f"{number:032x}", "failing\n", complete=False)
            failed = self.complete(f"{number:032x}", prepare=Fault("installation_failed", "fixture"))
            self.assertEqual(failed["phase"], "rejected")
        self.assertEqual(self.manage("status")["current"], first)
        latest = self.update("f" * 32, "after\n")
        # The install's record made the previous selection; what the rejected updates retained went with them.
        self.assertEqual(len(self.kept("operations")), maintenance.KEPT_OPERATIONS + 1)
        self.assertEqual((self.kept("versions"), self.kept("payloads")), ({first, latest}, {first, latest}))

    def test_the_previous_selection_outlives_more_ended_updates_than_kept_records(self):
        first = self.install()["outcome"]["current"]
        second = self.update(SECOND, "second\n")
        for number in range(3, maintenance.KEPT_OPERATIONS + 3):
            self.update(f"{number:032x}", "interrupted\n", complete=False)
            maintenance._save(self.state, self.record(f"{number:032x}"), "retaining")
            self.assertEqual(self.manage("continue", identity=f"{number:032x}")["status"], "rejected")
        third = self.update("f" * 32, "third\n")
        self.assertEqual((self.kept("versions"), self.kept("payloads")), ({second, third}, {first, second, third}))

    def test_continue_ends_an_update_interrupted_before_activation(self):
        first = self.install()["outcome"]["current"]
        for number, interrupt in enumerate(("killed while retaining", "killed while preparing", "I/O failure while preparing"), 2):
            identity = str(number) * 32
            with self.subTest(interrupt=interrupt):
                digest = self.update(identity, f"{interrupt}\n", complete=False)
                if interrupt == "killed while retaining":
                    maintenance._save(self.state, self.record(identity), "retaining")
                    (self.state / "service/payloads" / digest).mkdir()  # A partial copy.
                elif interrupt == "killed while preparing":
                    with lease(self.state / "service/operation.lock") as lock, \
                            patch.object(maintenance.os, "execve", side_effect=SystemExit), self.assertRaises(SystemExit):
                        maintenance.run_operation(self.state, identity, lock.fileno())
                    maintenance._save(self.state, self.record(identity), "preparing")
                    (self.state / "service/versions" / digest).mkdir()  # A partial installation.
                else:
                    failed = self.complete(identity, prepare=OSError("fixture"))
                    self.assertEqual((failed["phase"], failed["reason"]), ("preparing", "maintenance_effect_unverified"))
                with lease(self.state / "service/operation.lock"):
                    self.assertEqual(self.manage("continue", identity=identity)["status"], "busy")
                self.assertEqual(self.manage("operation", identity=identity)["status"], "continuation_required")
                ended = self.manage("continue", identity=identity)
                self.assertEqual((ended["status"], ended["operation"]["reason"]), ("rejected", "maintenance_interrupted"))
                self.assertEqual((self.kept("versions"), self.kept("payloads")), ({first}, {first}))
                self.assertEqual(self.manage("status")["current"], first)
        self.assertNotEqual(self.update("9" * 32, "after\n"), first)


if __name__ == "__main__":
    unittest.main()
