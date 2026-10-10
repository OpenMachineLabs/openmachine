"""Tests for service behavior at the component layer."""

from contextlib import contextmanager
import subprocess
import sys
import unittest
from unittest.mock import call, patch
import zipfile
from openmachine import service, tasks
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import maintenance
from openmachine.service import checks as service_checks
from openmachine.service import processes as service_processes
from openmachine.service import install as service_install
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from openmachine.service import transition as service_transition
from openmachine.tasks import database as task_database
from openmachine.tasks import migration as task_migration
from test.support.service_cases import ServiceCaseFixture
from test.support.tasks import write_legacy_checkpoint


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_commit_update_reuses_activation_and_leaves_selection_on_build_failure(self):
        with patch.object(sys, "platform", "darwin"), \
                patch.object(service_runtime, "_verify_files"), \
                patch.object(service_install, "_prepare_commit", return_value=self.b) as prepare, \
                patch.object(service_transition, "_activate", return_value={"status": "ready"}) as activate:
            self.assertEqual(service_transition.operate(self.config, self.state, "update", commit="c" * 40), {"status": "ready"})
            self.assertEqual(prepare.call_args.args[:2], (self.state, "c" * 40))
            self.assertEqual(prepare.call_args.args[2].read_bytes(), self.config.read_bytes())
            self.assertEqual(activate.call_args.args[2], self.b)
            activate.reset_mock()
            prepare.side_effect = Fault("commit_build_failed", "Fixture build failure.")
            self.assert_fault("commit_build_failed", lambda: service_transition.operate(
                self.config, self.state, "update", commit="c" * 40))
            activate.assert_not_called()
            self.assertEqual(service_state._record(self.state), self.record)
            for inputs in ({"wheel": self.root / "wheel.whl"}, {"requirements": self.root / "requirements.lock"}):
                self.assert_fault("invalid_service_request", lambda: service_transition.operate(
                    self.config, self.state, "update", commit="c" * 40, **inputs))
            activate.assert_not_called()

    def ledger_activation_fixture(self):
        self.enterContext(patch.object(service_runtime, "socket_alive", return_value=False))
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 18})
        return self.activation_mocks()

    def test_task_cutover_follows_gate_and_controller_stop_before_start(self):
        _, stop, start = self.ledger_activation_fixture()
        events = []
        stop.side_effect = lambda state: events.append("stop")
        start.side_effect = lambda state, record: events.append("start") or {"status": "ready"}
        def migrate(state):
            self.assertEqual(service_state._record(state)["pending"], {"from": self.a, "to": self.b, "previous": None})
            events.append("migrate")
        with patch.object(tasks, "migrate", side_effect=migrate), \
                patch.object(service_checks, "activation_safe", side_effect=lambda state, **kwargs: events.append("gate")):
            result = service_transition._activate(self.state, self.record, self.b, require_safe=True)
        self.assertEqual(events, ["gate", "stop", "migrate", "start"])
        self.assertEqual(result["current"], self.b)
        self.assertIsNone(service_state._record(self.state)["pending"])

    def test_cleanup_guard_is_retained_through_failed_cutover_and_restoration(self):
        _, stop, start = self.activation_mocks()
        events = []

        @contextmanager
        def original_guard():
            events.append("guard_acquired")
            try:
                yield
            finally:
                events.append("guard_released")

        def admit(state, *, config, candidate_config, guards):
            self.assertEqual(candidate_config.machines["local"].transport, "local")
            guards.enter_context(original_guard())

        def stopped(state):
            self.assertNotIn("guard_released", events)
            events.append("stop")

        def started(state, record):
            self.assertNotIn("guard_released", events)
            events.append("new_start" if record["current"] == self.b else "old_start")
            if record["current"] == self.b:
                raise Fault("service_unready", "Simulated candidate failure")
            return {"status": "ready"}

        stop.side_effect = stopped
        start.side_effect = started
        with patch.object(service_checks, "activation_safe", side_effect=admit), \
                patch.object(service_runtime, "_sync_definition"):
            self.assert_fault("activation_failed_restored", lambda:
                              service_transition._activate(self.state, self.record, self.b, require_safe=True))
        self.assertEqual(events, ["guard_acquired", "stop", "new_start",
                                  "stop", "old_start", "guard_released"])
        self.assertEqual(service_state._record(self.state), self.record)

    def test_candidate_removing_original_local_machine_defers_before_stop(self):
        _, stop, start = self.activation_mocks()
        identity = "d" * 32
        private_directory(self.state / "executions" / identity)
        candidate = self.root / "candidate.toml"
        candidate.write_text('schema_version=1\n[machines.replacement]\ntransport="local"\n')
        unsettled = {"status": "outcome_unknown", "request": {"machine": "local"},
                     "cleanup": {"status": "unverified"}}

        @contextmanager
        def original_guard(config, state, identities):
            self.assertEqual(identities, [identity])
            yield {identity: unsettled}

        with patch.object(service_checks.execution, "read_record", return_value=unsettled), \
                patch.object(service_checks.execution, "activation_guard", side_effect=original_guard), \
                patch.object(service_checks.tasks, "activation_blockers", return_value=[]):
            self.assert_fault("activation_deferred", lambda: service_transition._activate(
                self.state, self.record, self.b, candidate_config=candidate, require_safe=True))
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)

    def test_task_migration_before_cutover_failure_restores_original_selection(self):
        _, stop, start = self.ledger_activation_fixture()
        with patch.object(tasks, "migrate", side_effect=Fault("task_storage_full", "fixture")), \
                patch.object(tasks, "storage_state", return_value={"kind": "legacy", "fenced": False}), \
                patch.object(service_runtime, "_sync_definition"):
            self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._record(self.state), self.record)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual([item.args[1]["current"] for item in start.call_args_list], [self.a])

    def test_unfenced_import_retry_uses_real_activation_gate_and_migration(self):
        for identity in ("task-one", "task-one-c1"):
            write_legacy_checkpoint(self.state, identity, "Retain the original checkpoint.\n")
        root = self.state / "tasks"
        original = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        with patch.object(task_migration, "_verify_import", side_effect=Fault("task_migration_incomplete", "Fixture failed import.")):
            self.assert_fault("task_migration_incomplete", lambda: tasks.migrate(self.state))
        prepared = tasks.storage_state(self.state)
        inode = (self.state / "locks/tasks.lock").stat().st_ino
        _, stop, start = self.ledger_activation_fixture()

        result = service_transition._activate(self.state, self.record, self.b, require_safe=True)

        self.assertEqual(result["current"], self.b)
        self.assertIsNone(service_state._record(self.state)["pending"])
        self.assertEqual(tasks.storage_state(self.state)["identity"], prepared["identity"])
        self.assertEqual((self.state / "locks/tasks.lock").stat().st_ino, inode)
        archive = next(self.state.glob("tasks-legacy-*"))
        self.assertEqual({path.relative_to(archive): path.read_bytes() for path in archive.rglob("*") if path.is_file()}, original)
        self.assertEqual(tasks.check_state(self.state)["task_records"], 2)
        stop.assert_called_once()
        start.assert_called_once()

    def test_fenced_task_migration_and_start_failures_never_restore_old_writer(self):
        verify, stop, start = self.ledger_activation_fixture()
        for active in (False, True):
            with self.subTest(active=active):
                self.save(self.record)
                verify.reset_mock()
                stop.reset_mock()
                start.reset_mock()
                start.side_effect = Fault("service_unready", "fixture")
                storage = {"kind": "sqlite", "fenced": True, "active": active}
                with patch.object(tasks, "migrate", side_effect=None if active else OSError("cutover acknowledgement lost")), \
                        patch.object(tasks, "storage_state", return_value=storage):
                    self.assert_fault("task_activation_pending", lambda: service_transition._activate(self.state, self.record, self.b))
                observed = service_state._record(self.state)
                self.assertEqual((observed["current"], observed["pending"]),
                                 (self.b, {"from": self.a, "to": self.b, "previous": None}))
                self.assert_fault("activation_pending", lambda: service.guard_writes(self.state))
                stop.assert_called_once()
                verify.assert_called_once()
                self.assertEqual(start.call_count, int(active))

    def test_explicit_task_cutover_resume_preserves_deliberate_stop(self):
        record = self.policy_fixture()
        _, stop, start = self.ledger_activation_fixture()
        digest = service_state._snapshot_configuration(self.state, self.config)
        selected = record | {"current": self.b, "configurations": {self.a: digest, self.b: digest},
                             "pending": {"from": self.a, "to": self.b, "previous": None}}
        self.save(selected)
        with patch.object(tasks, "storage_state", return_value={"kind": "sqlite", "active": True, "fenced": True}), \
                patch.object(tasks, "migrate"), patch.object(service_checks, "activation_safe"), \
                patch.object(service_runtime, "_sync_definition"):
            result = service_transition._resume_activation(self.state, selected)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(service_state._record(self.state)["policy"], record["policy"])
        stop.assert_called_once()
        start.assert_not_called()

    def test_fresh_initialization_failure_resumes_original_selection_and_lock(self):
        _, stop, start = self.ledger_activation_fixture()
        with patch.object(task_database, "commit", side_effect=OSError("private schema commit interrupted")):
            self.assert_fault("task_activation_pending", lambda: service_transition._activate(
                self.state, self.record, self.b))
        selected = service_state._record(self.state)
        self.assertEqual((selected["current"], selected["pending"]),
                         (self.b, {"from": self.a, "to": self.b, "previous": None}))
        lock = self.state / "locks/tasks.lock"
        original = lock.stat()
        self.assertFalse((self.state / "tasks.db").exists())
        storage = tasks.storage_state(self.state)
        self.assertEqual((storage["kind"], storage["active"], storage["fenced"]), ("absent", False, True))
        self.assertTrue(service_transition._task_cutover(self.state, selected))
        self.assertFalse(service_transition._task_cutover(self.state, selected | {"pending": None}, committed=True))
        stop.assert_called_once()
        start.assert_not_called()

        with patch.object(service_checks, "activation_safe"):
            result = service_transition._resume_activation(self.state, selected)
        self.assertEqual((result["current"], result["previous"]), (self.b, self.a))
        self.assertIsNone(service_state._record(self.state)["pending"])
        retained = lock.stat()
        self.assertEqual((retained.st_dev, retained.st_ino), (original.st_dev, original.st_ino))
        self.assertEqual(lock.read_bytes(), b"")
        self.assertTrue(tasks.storage_state(self.state)["active"])
        self.assertFalse((self.state / "tasks").exists())
        self.assertEqual(stop.call_count, 2)
        self.assertEqual([item.args[1]["current"] for item in start.call_args_list], [self.b])

    def test_first_upgrade_preserves_old_state_until_readiness_and_restores_on_failure(self):
        self.activation_mocks()
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 9})
        def start(state, record):
            self.assertIsNone(service_state._read_recovery(state))
            if record["current"] == self.b:
                raise Fault("service_unready", "fixture")
            return {"status": "ready"}
        with patch.object(service_runtime, "_start", side_effect=start), patch.object(service_runtime, "_sync_definition") as sync:
            self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, self.record, self.b))
            sync.assert_called_once()
        self.assertIsNone(service_state._read_recovery(self.state))
        self.assertTrue(service_runtime._definition(self.state, self.record)["AbandonProcessGroup"])
        with patch.object(service_runtime, "_start", return_value={"status": "ready"}):
            service_transition._activate(self.state, self.record, self.b)
        self.assertEqual(service_state._read_recovery(self.state), {"format": 1, "episode": None})
        self.assertFalse(service_runtime._definition(self.state, service_state._record(self.state))["AbandonProcessGroup"])

    def test_first_upgrade_commit_crash_leaves_rollback_or_committed_selection(self):
        self.activation_mocks()
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 9})
        for committed in (False, True):
            self.save(self.record)
            def crash_at_commit(path, value):
                if path == self.state / "service/service.json" and value["current"] == self.b and value["pending"] is None:
                    self.assertEqual(value["recovery"], {"format": 1, "episode": None})
                    if committed:
                        save_json(path, value)
                    raise OSError("Injected controller loss at atomic commit")
                save_json(path, value)
            with self.subTest(committed=committed), patch.object(service_transition, "save_json", side_effect=crash_at_commit):
                with self.assertRaises(OSError):
                    service_transition._activate(self.state, self.record, self.b)
            observed = service_state._record(self.state)
            if committed:
                self.assertIsNone(observed["pending"])
                self.assertEqual(observed["current"], self.b)
                self.assertEqual(service_state._read_recovery(self.state), {"format": 1, "episode": None})
            else:
                self.assertEqual(observed["pending"]["from"], self.a)
                self.assertIsNone(service_state._read_recovery(self.state))
                import plistlib
                owned_plist = self.root / "owned.plist"
                for owned, content in ((owned_plist, plistlib.dumps(service_runtime._definition(self.state, observed))),
                                       (self.state / "service/launch.py", service_state.LAUNCHER.encode()),
                                       (self.state / "service/service.log", b"")):
                    owned.write_bytes(content)
                    owned.chmod(0o600)
                def restore(state, selection):
                    service_runtime._verify_files(state, selection)
                    return {"status": "ready"}
                with patch.object(service_runtime, "_plist_path", return_value=owned_plist), patch.object(service_runtime, "_start", side_effect=restore):
                    service_transition._activate(self.state, observed, self.a)
                self.assertEqual(service_state._record(self.state)["current"], self.a)
                self.assertIsNone(service_state._read_recovery(self.state))

    def test_activation_preserves_recovery_advanced_during_candidate_preflight(self):
        verify, _, start = self.activation_mocks()
        self.managed_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        later = dict(episode, attempts=2)
        verify.side_effect = lambda *_args, **_kwargs: service_state._save_recovery(self.state, later)
        start.side_effect = Fault("service_unready", "fixture")
        self.assert_fault("recovery_required", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._read_recovery(self.state)["episode"], later)

    def test_successful_activation_retains_rollback_target(self):
        verify, stop, start = self.activation_mocks()
        result = service_transition._activate(self.state, self.record, self.b)
        self.assertEqual((result["current"], result["previous"]), (self.b, self.a))
        self.assertIsNone(service_state._record(self.state)["pending"])
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(start.call_count, 1)
        verify.assert_called_once()
        self.assertTrue(service_state._runtime(self.state, self.a).is_dir())

    def test_bad_candidate_does_not_stop_or_select_over_healthy_version(self):
        verify, stop, start = self.activation_mocks()
        verify.side_effect = Fault("candidate_unready", "fixture")
        self.assert_fault("candidate_unready", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._record(self.state), self.record)
        stop.assert_not_called()
        start.assert_not_called()

    def test_failed_activation_restores_original_service_and_returns_failure(self):
        _, stop, start = self.activation_mocks()
        start.side_effect = [Fault("service_read_unavailable", "fixture"), {"status": "ready"}]
        self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._record(self.state), self.record)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual([call.args[1]["current"] for call in start.call_args_list], [self.b, self.a])

    def test_failed_restoration_retains_exact_recovery_intent(self):
        _, _, start = self.activation_mocks()
        start.side_effect = Fault("service_unready", "fixture")
        self.assert_fault("recovery_required", lambda: service_transition._activate(self.state, self.record, self.b))
        observed = service_state._record(self.state)
        self.assertEqual(observed["current"], self.a)
        self.assertEqual(observed["pending"], {"from": self.a, "to": self.b, "previous": None})
        self.assert_fault("activation_pending", lambda: service.guard_writes(self.state))

    def test_interrupted_selection_recovers_original_not_unverified_candidate(self):
        self.activation_mocks()
        pending = {**self.record, "current": self.b,
                   "pending": {"from": self.a, "to": self.b, "previous": None}}
        self.save(pending)
        result = service_transition._activate(self.state, pending, self.a)
        self.assertEqual(result["current"], self.a)
        self.assertIsNone(result["previous"])
        self.assertIsNone(service_state._record(self.state)["pending"])

    def test_activation_lock_conflict_preserves_selection(self):
        _, stop, start = self.activation_mocks()
        with lease(self.state / "locks/lifecycle.lock", shared=True):
            self.assert_fault("resource_busy", lambda: service_transition._activate(self.state, self.record, self.b))
        self.assertEqual(service_state._record(self.state), self.record)
        stop.assert_not_called()
        start.assert_not_called()

    def test_restart_rejects_incompatible_state_before_stopping_service(self):
        with patch.object(sys, "platform", "darwin"), patch.object(service_runtime, "_verify_files"), \
                patch.object(service_checks, "_candidate_state", side_effect=Fault("incompatible_state", "fixture")), \
                patch.object(service_runtime, "_stop") as stop, patch.object(service_runtime, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service.operate(self.config, self.state, "restart"))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)

    def test_explicit_configuration_survives_update_and_exact_rollback(self):
        self.activation_mocks()
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# explicitly selected candidate configuration\n")
        service_transition._activate(self.state, self.record, self.b, candidate)
        self.assertEqual(self.config.read_bytes(), candidate.read_bytes())
        selected = service_state._record(self.state)
        self.assertEqual(set(selected["configurations"]), {self.a, self.b})
        service_transition._activate(self.state, selected, self.a)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service_state._record(self.state)["current"], self.a)

    def test_same_wheel_configuration_update_is_reversible_and_idempotent(self):
        _, stop, start = self.activation_mocks()
        (self.state / "service/versions").chmod(0o700)
        wheel = self.root / "openmachine-0.1.0a7-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("openmachine-0.1.0a7.dist-info/METADATA", "Name: openmachine\nVersion: 0.1.0a7\n")
        requirements = self.root / "requirements.lock"
        requirements.write_text("example==1.0\n")
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# explicitly selected configuration\n")
        selected_bytes = candidate.read_bytes()
        def observed(python, config, *_args, **_kwargs):
            return {"version": "0.1.0a7", "prefix": str(python.parent.parent), "protocol": 1, "state_contract": 1}
        with patch.object(sys, "platform", "darwin"), patch.object(service_runtime, "_verify_files"), \
                patch.object(service_runtime, "socket_alive", return_value=False), \
                patch.object(service_runtime, "status", return_value={"status": "ready"}), \
                patch.object(service_processes, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch.object(service_checks, "_candidate_state", side_effect=observed):
            first = service_install._prepare(self.state, wheel, requirements, self.config)
            self.save(self.record | {"current": first})
            result = service_transition.operate(self.config, self.state, "update", wheel=wheel,
                                                requirements=requirements, candidate_config=candidate)
            second = result["current"]
            self.assertNotEqual(first, second)
            self.assertEqual(result["previous"], first)
            self.assertEqual(self.config.read_bytes(), selected_bytes)
            self.assertEqual(stop.call_count, 1)
            run.reset_mock()
            # Equal bytes at another path are the same candidate, not another restart.
            service_transition.operate(self.config, self.state, "update", wheel=wheel, requirements=requirements)
            run.assert_not_called()
            self.assertEqual(stop.call_count, 1)
            service_transition.operate(self.config, self.state, "rollback")
            self.assertEqual(self.config.read_bytes(), original)
            before = service_state._record(self.state)
            self.assertEqual((before["current"], before["previous"]), (first, second))
            start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
            self.assert_fault("activation_failed_restored", lambda: service_transition.operate(
                self.config, self.state, "update", wheel=wheel, requirements=requirements, candidate_config=candidate))
            self.assertEqual(self.config.read_bytes(), original)
            self.assertEqual(service_state._record(self.state), before)
            run.assert_not_called()
            # Mutating the source during preflight cannot change the retained candidate.
            start.side_effect = None
            def change_source(*args, **kwargs):
                candidate.write_bytes(original + b"# later unrelated source edit\n")
                return observed(*args, **kwargs)
            with patch.object(service_checks, "_candidate_state", side_effect=change_source):
                service_transition.operate(self.config, self.state, "update", wheel=wheel,
                                           requirements=requirements, candidate_config=candidate)
            self.assertEqual(self.config.read_bytes(), selected_bytes)
            self.assertEqual(service_state._record(self.state)["current"], second)

    def test_failed_activation_restores_exact_configuration_bytes(self):
        _, stop, start = self.activation_mocks()
        start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# candidate configuration\n")
        self.assert_fault("activation_failed_restored", lambda: service_transition._activate(
            self.state, self.record, self.b, candidate))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service_state._record(self.state), self.record)
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
        self.assert_fault("recovery_required", lambda: service_transition._activate(
            self.state, self.record, self.b, candidate))
        self.assertEqual(self.config.read_bytes(), manual)
        self.assertIsNotNone(service_state._record(self.state)["pending"])
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
        self.assert_fault("incompatible_remote_state", lambda: service_transition._activate(
            self.state, self.record, self.b, candidate))
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(service_state._record(self.state), self.record)

    def test_failed_update_restores_current_bytes_not_a_stale_snapshot(self):
        _, _, start = self.activation_mocks()
        service_transition._activate(self.state, self.record, self.b)
        current = service_state._record(self.state)
        edited = self.config.read_bytes() + b"# valid edit since the previous activation\n"
        self.config.write_bytes(edited)
        start.side_effect = [Fault("service_unready", "fixture"), {"status": "ready"}]
        self.assert_fault("activation_failed_restored", lambda: service_transition._activate(
            self.state, current, self.a, self.config))
        self.assertEqual(self.config.read_bytes(), edited)
        restored = service_state._record(self.state)
        saved = service_state._configuration_for(self.state, restored, self.b)
        self.assertEqual(saved.read_bytes(), edited)

    def test_unrelated_retained_configuration_is_not_an_allowed_racing_edit(self):
        _, _, start = self.activation_mocks()
        original = self.config.read_bytes()
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(original + b"# first explicit configuration\n")
        service_transition._activate(self.state, self.record, self.b, candidate)
        current = service_state._record(self.state)
        third = "c" * 64
        directory = private_directory(self.state / "service/versions" / third)
        save_json(directory / "installation.json", {
            "identity": third, "version": "0.1.0a7", "protocol": 1, "state_contract": 1})
        candidate.write_bytes(original + b"# next explicit configuration\n")
        def independently_restore_older_bytes(*_):
            self.config.write_bytes(original)
            raise Fault("service_unready", "fixture")
        start.side_effect = independently_restore_older_bytes
        self.assert_fault("recovery_required", lambda: service_transition._activate(
            self.state, current, third, candidate))
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIsNotNone(service_state._record(self.state)["pending"])

    def test_corrupt_configuration_snapshot_cannot_be_selected(self):
        self.activation_mocks()
        service_transition._activate(self.state, self.record, self.b)
        selected = service_state._record(self.state)
        digest = selected["configurations"][self.a]
        path = service_state._saved_configuration(self.state, digest)
        path.write_text("corrupt")
        self.assert_fault("configuration_unavailable", lambda: service_transition._activate(self.state, selected, self.a))
        self.assertEqual(service_state._record(self.state), selected)

    def test_retained_remote_workspace_state_blocks_old_selection_before_stop(self):
        pin, archive = "d" * 64, "e" * 64
        with self.config.open("a") as file:
            file.write('\n[machines.remote]\ntransport="ssh"\nssh_alias="fixture"\nssh_identity="' + pin +
                       '"\nssh_worker="/target/' + archive + '.pyz"\n')
        installation = self.state / "service/versions" / self.a / "installation.json"
        save_json(installation, service_state._json(installation) | {"state_contract": 4})
        _, stop, start = self.activation_mocks()
        for present, compatible in ((True, True), (False, False)):
            proof = {"workspace_change_store_present": present, "legacy_workspace_compatible": compatible}
            with patch.object(service_checks, "_remote_workspace_state", return_value=proof) as read:
                self.assert_fault("incompatible_remote_state", lambda: service_transition._activate(self.state, self.record, self.b))
                self.assertEqual(read.call_args.args, (service_state._runtime(self.state, self.a) / "bin/python", self.config, "remote"))
            self.assertEqual(service_state._record(self.state), self.record)
            stop.assert_not_called()
            start.assert_not_called()

    def test_stopped_update_and_restoration_preserve_recovery_without_starting(self):
        record = self.policy_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        verify, stop, start = self.activation_mocks()
        with patch.object(service_runtime, "_sync_definition"):
            result = service_transition._activate(self.state, record, self.b)
        self.assertEqual(result["status"], "stopped")
        start.assert_not_called()
        self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)
        current = service_state._record(self.state)
        with patch.object(service_runtime, "_sync_definition", side_effect=[OSError("fixture"), None]):
            self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, current, self.a))
        self.assertEqual(service_state._record(self.state)["current"], self.b)
        self.assertEqual(service_state._read_recovery(self.state)["episode"], episode)
        start.assert_not_called()

    def test_old_reader_restoration_keeps_only_the_remaining_native_handoff(self):
        for identity, contract in ((self.a, 13), (self.b, 14)):
            path = self.state / "service/versions" / identity / "installation.json"
            save_json(path, service_state._json(path) | {"state_contract": contract})
        _, _, start = self.activation_mocks()
        def actual_reader_boundary(state, selected):
            if selected["current"] == self.b:
                raise Fault("service_unready", "Rejected candidate fixture")
            # Old supervisors validate every installation named by pending.
            # Keep the admission marker, but never ask that reader to load 14.
            pending = service_state._record(state)["pending"]
            self.assertIsNotNone(pending)
            with patch.object(service_state, "STATE_CONTRACT", 13):
                for identity in {selected["current"], pending["from"], pending["to"]}:
                    service_state._installed(state, identity)
            return {"status": "ready"}
        start.side_effect = actual_reader_boundary
        with patch.object(service_runtime, "_sync_definition") as sync:
            for interrupted in (False, True):
                with self.subTest(interrupted=interrupted):
                    record = dict(self.record)
                    if interrupted:
                        record.update(current=self.b, pending={"from": self.a, "to": self.b, "previous": None})
                    self.save(record)
                    if interrupted:
                        result = service_transition._activate(self.state, record, self.a)
                        self.assertEqual(result["status"], "ready")
                    else:
                        self.assert_fault("activation_failed_restored", lambda: service_transition._activate(self.state, record, self.b))
                    self.assertEqual(service_state._record(self.state)["current"], self.a)
                    self.assertIsNone(service_state._record(self.state)["pending"])
                    self.assertEqual(sync.call_args.args[1]["current"], self.a)
                    self.assertTrue((self.state / "service/versions" / self.b).is_dir())

    def test_policy_and_binding_preserve_recovery_advanced_before_admission(self):
        self.enterContext(patch.object(service_transition.sys, "platform", "darwin"))
        from contextlib import contextmanager
        self.policy_fixture()
        episode = service_runtime._recovery_attempt(self.state, None, 1000000)
        later = episode | {"attempts": 2}
        @contextmanager
        def admission(path, **options):
            if path.name == "lifecycle.lock":
                service_state._save_recovery(self.state, later)
            with lease(path, **options) as owned:
                yield owned
        with patch.object(maintenance, "lease", side_effect=admission), \
                patch.object(service_runtime, "_verify_files"), patch.object(service_checks, "_candidate_state"), \
                patch.object(service_state, "_bind_management", side_effect=lambda state, record, identity: record), \
                patch.object(service_runtime, "_sync_definition"), patch.object(service_runtime, "_stop"), \
                patch.object(maintenance, "observe", return_value={}):
            for action in ("stop", "bind"):
                service_state._save_recovery(self.state, episode)
                maintenance.control(self.config, self.state, action)
                self.assertEqual(service_state._read_recovery(self.state)["episode"], later)
        with patch.object(service_transition, "lease", side_effect=admission), \
                patch.object(service_runtime, "_verify_files"), patch.object(service_checks, "_candidate_state"), \
                patch.object(service_runtime, "_start", return_value={"status": "ready"}):
            service_state._save_recovery(self.state, episode)
            service_transition.operate(self.config, self.state, "start")
            self.assertFalse(service_state.stopped(service_state._record(self.state)))
            self.assertEqual(service_state._read_recovery(self.state)["episode"], later)


if __name__ == "__main__":
    unittest.main()
