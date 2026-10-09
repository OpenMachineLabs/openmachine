"""Retired configuration migration uses real historical shapes and managed guards."""

from contextlib import closing
import hashlib
import json
import sqlite3
import sys
import tomllib
import unittest
from unittest.mock import patch

from openmachine import tasks
from openmachine.config import _retired_chat_recovery, load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import save_json
from openmachine.service import install as service_install
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from openmachine.service import transition as service_transition
from test.support.service_cases import ServiceCaseFixture
from test.support.tasks import replace_task_record, task_contract


class RetiredConfigurationTests(ServiceCaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.original = (
            'schema_version = 1\n'
            '[machines.local]\ntransport = "local"\nexecution_enabled = true\n'
            '[workspaces.project]\nmachine = "local"\nroot = ' + json.dumps(str(self.root)) + '\n'
            '[chat_recovery]\nworkspace = "project"\nconnection = "existing-connection"\n'
            'browser_account = "existing-account"\nbrowser_workspace = "existing-browser"\n'
            'model = "retired-model"\n'
            '[github]\naccount = "fixture-owner"\nread_repositories = ["OpenMachineLabs/openmachine"]\n'
            '[skills]\nroots = ["/fixture/retained-skills"]\n'
        ).encode()
        self.config.write_bytes(self.original)
        for identity, version, contract in ((self.a, "0.2.16", 18), (self.b, "0.2.17", 20)):
            path = self.state / "service/versions" / identity / "installation.json"
            save_json(path, service_state._json(path) | {"version": version, "state_contract": contract})
        self.wheel = self.root / "candidate.whl"
        self.requirements = self.root / "requirements.lock"

    def configured(self):
        # Product commands and native start are fixture-only, while state/config
        # snapshots, Task migration and activation guards stay real.
        verify, stop, start = self.activation_mocks()
        self.enterContext(patch.object(sys, "platform", "darwin"))
        self.enterContext(patch.object(service_runtime, "_verify_files"))
        self.enterContext(patch.object(service_runtime, "_sync_definition"))
        self.enterContext(patch.object(service_runtime, "socket_alive", return_value=False))
        prepare = self.enterContext(patch.object(service_install, "_prepare", return_value=self.b))
        return prepare, verify, stop, start

    def assert_no_temp_candidate(self):
        self.assertEqual(list((self.state / "service").glob(".retired-config-*")), [])

    def retained_task(self):
        request = task_contract()
        task_id = request.pop("task_id")
        tasks.create(self.state, task_id, **request)
        record = tasks.read(self.state, task_id)["record"]
        record["contract"]["recovery_policy"] = "disabled"
        record["chat_registration"] = None
        replace_task_record(self.state, record)
        return task_id

    def task_authority(self, identity):
        with closing(sqlite3.connect(self.state / "tasks.db")) as connection:
            row = connection.execute("SELECT current,sha256 FROM tasks WHERE task_id=?", (identity,)).fetchone()
        return row

    def test_old_core_config_is_strictly_retired_without_reviving_an_interface(self):
        settings, candidate = _retired_chat_recovery(self.original)
        parsed = tomllib.loads(candidate.decode())
        expected = tomllib.loads(self.original.decode())
        expected.pop("chat_recovery")
        self.assertEqual(parsed, expected)
        self.assertEqual(candidate, self.original[:self.original.index(b"[chat_recovery]")] +
                         self.original[self.original.index(b"[github]"):])
        self.assertEqual(settings.machines["local"].execution_enabled, True)
        self.assertEqual(settings.github.account, "fixture-owner")
        self.assertEqual(_retired_chat_recovery(candidate)[1], candidate)
        self.assert_fault("invalid_config", lambda: load(self.config))
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_old_version_updates_once_and_rolls_back_with_original_task_bytes(self):
        identity = self.retained_task()
        before_task = self.task_authority(identity)
        candidate = _retired_chat_recovery(self.original)[1]
        prepare, verify, stop, start = self.configured()

        def examine(_state, _wheel, _locked, path):
            self.assertEqual(path.read_bytes(), candidate)
            self.assertNotEqual(path, self.config)
            self.assertEqual(self.config.read_bytes(), self.original)
            return self.b

        prepare.side_effect = examine
        updated = service_transition.operate(
            self.config, self.state, "update", wheel=self.wheel, requirements=self.requirements)
        self.assertEqual(updated["status"], "ready")
        self.assertEqual((updated["current"], updated["previous"]), (self.b, self.a))
        self.assertEqual(self.config.read_bytes(), candidate)
        self.assertEqual(self.task_authority(identity), before_task)
        self.assertEqual(tasks.check_state(self.state)["structured_task_records"], 1)
        self.assertFalse(tasks.read(self.state, identity)["support"]["runtime_recovery"])
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(start.call_count, 1)
        self.assert_no_temp_candidate()
        self.assertIsNone(service_state._record(self.state)["pending"])
        self.assertEqual(
            service_state._saved_configuration(
                self.state, service_state._record(self.state)["configurations"][self.a]).read_bytes(),
            self.original)

        # Rollback selects the retained 0.2.16 reader and its exact old bytes.
        rolled_back = service_transition.operate(self.config, self.state, "rollback")
        self.assertEqual(rolled_back["current"], self.a)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.task_authority(identity), before_task)
        self.assertIsNone(service_state._record(self.state)["pending"])
        self.assertEqual(stop.call_count, 2)

    def test_preflight_failure_preserves_selected_service_and_exact_configuration(self):
        prepare, verify, stop, start = self.configured()
        verify.side_effect = Fault("candidate_unready", "fixture")
        self.assert_fault("candidate_unready", lambda: service_transition.operate(
            self.config, self.state, "update", wheel=self.wheel, requirements=self.requirements))
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(service_state._record(self.state), self.record)
        self.assert_no_temp_candidate()
        stop.assert_not_called()
        start.assert_not_called()

    def test_activation_failure_restores_old_service_and_configuration(self):
        identity = self.retained_task()
        before_task = self.task_authority(identity)
        _, _, stop, start = self.configured()
        start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
        self.assert_fault("activation_failed_restored", lambda: service_transition.operate(
            self.config, self.state, "update", wheel=self.wheel, requirements=self.requirements))
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.task_authority(identity), before_task)
        restored = service_state._record(self.state)
        self.assertEqual((restored["current"], restored["previous"], restored["pending"]),
                         (self.a, None, None))
        self.assertEqual(restored["config"], self.record["config"])
        self.assertEqual(restored.get("recovery"), {"format": 1, "episode": None})
        self.assert_no_temp_candidate()
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(start.call_count, 2)

    def test_changed_original_configuration_is_never_overwritten_during_preflight(self):
        prepare, _, stop, start = self.configured()
        independent = self.original + b"# independent retained edit\n"

        def change_source(*_):
            self.config.write_bytes(independent)
            return self.b

        prepare.side_effect = change_source
        self.assert_fault("configuration_changed", lambda: service_transition.operate(
            self.config, self.state, "update", wheel=self.wheel, requirements=self.requirements))
        self.assertEqual(self.config.read_bytes(), independent)
        self.assertEqual(service_state._record(self.state), self.record)
        self.assert_no_temp_candidate()
        stop.assert_not_called()
        start.assert_not_called()

    def test_malformed_or_unknown_retired_config_fails_closed_before_package_effect(self):
        originals = [
            self.original.replace(b'model = "retired-model"', b"model = true"),
            self.original.replace(b'connection = "existing-connection"', b'connection = ""'),
            self.original.replace(b'workspace = "project"', b'workspace = "absent"'),
            self.original.replace(b'model = "retired-model"', b'model = "retired-model"\nunknown = "ignored"'),
            self.original + b"\n[unrelated]\nauthorization = true\n",
            self.original.replace(b"[chat_recovery]", b"[chat_recovery.unexpected]"),
            self.original.replace(b"[chat_recovery]", b"[chat_recovery] # retired\n") +
                b"\n[chat_recovery.other]\nkey = 'injected'\n",
        ]
        prepare, verify, stop, start = self.configured()
        for invalid in originals:
            with self.subTest(variant=hashlib.sha256(invalid).hexdigest()[:10]):
                self.config.write_bytes(invalid)
                self.assert_fault("invalid_config", lambda: service_transition.operate(
                    self.config, self.state, "update", wheel=self.wheel, requirements=self.requirements))
                self.assertEqual(self.config.read_bytes(), invalid)
                self.assertEqual(service_state._record(self.state), self.record)
                self.assert_no_temp_candidate()
        prepare.assert_not_called()
        verify.assert_not_called()
        stop.assert_not_called()
        start.assert_not_called()

    def test_product_management_accepts_one_verified_sanitized_original_intent(self):
        from openmachine.service import maintenance

        source = self.root / "trusted-payload.tar"
        original_hash = hashlib.sha256(self.original).hexdigest()
        candidate = _retired_chat_recovery(self.original)[1]
        operation_id = "f" * 32
        with patch.object(maintenance.payload, "verify") as verify, \
                patch.object(maintenance, "_bound") as bound, \
                patch.object(maintenance, "_spawn") as spawn:
            submitted = maintenance.submit(
                self.config, self.state, "update", operation_id, source, "d" * 64,
                expected_config_sha256=original_hash)
        self.assertEqual(submitted["status"], "accepted")
        request = submitted["operation"]["request"]
        self.assertEqual(request["config_sha256"], original_hash)
        self.assertEqual(request["candidate_sha256"], hashlib.sha256(candidate).hexdigest())
        self.assertNotEqual(request["candidate_config"], str(self.config))
        self.assertEqual(service_state._saved_configuration(
            self.state, request["candidate_sha256"]).read_bytes(), candidate)
        self.assertEqual(maintenance._inputs_unchanged(self.state, submitted["operation"]).read_bytes(), candidate)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(service_state._record(self.state), self.record)
        self.assert_no_temp_candidate()
        verify.assert_called_once()
        bound.assert_called_once()
        spawn.assert_called_once()

    def test_product_management_rejects_racing_and_invalid_old_configuration(self):
        from openmachine.service import maintenance

        source = self.root / "trusted-payload.tar"
        changed = self.original + b"# later external edit\n"
        with patch.object(maintenance.payload, "verify"), \
                patch.object(maintenance, "_spawn") as spawn, \
                patch.object(maintenance, "_bound", side_effect=lambda *_: self.config.write_bytes(changed)):
            self.assert_fault("configuration_changed", lambda: maintenance.submit(
                self.config, self.state, "update", "e" * 32, source, "d" * 64))
        self.assertEqual(self.config.read_bytes(), changed)
        self.assertFalse((self.state / "service/operations" / ("e" * 32 + ".json")).exists())
        self.assert_no_temp_candidate()
        spawn.assert_not_called()

        self.config.write_bytes(self.original + b"[unknown]\nunsafe=true\n")
        with patch.object(maintenance.payload, "verify"), \
                patch.object(maintenance, "_spawn") as spawn:
            self.assert_fault("invalid_config", lambda: maintenance.submit(
                self.config, self.state, "update", "a" * 32, source, "d" * 64))
        self.assertEqual(service_state._record(self.state), self.record)
        self.assertFalse((self.state / "service/operations" / ("a" * 32 + ".json")).exists())
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
