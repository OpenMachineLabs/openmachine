"""Tests for service behavior at the component layer."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from openmachine import execution, tasks
from openmachine.config import load
from openmachine.app.task_receipts import observation as task_observation
from openmachine.execution import api as execution_api
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import maintenance
from openmachine.service import checks as service_checks
from openmachine.service import processes as service_processes
from openmachine.service import install as service_install
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from openmachine.service import transition as service_transition
from openmachine.tasks import records as task_records
from openmachine.workspace import worktrees
from openmachine.workspace.git import context
from test.support.service_cases import ServiceCaseFixture
from test.support.tasks import task_contract, write_legacy_task


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def historical_execution_fixture(self, *, creation=False, boot_id=None):
        work = self.root / "work"
        work.mkdir()
        self.config.write_text(self.config.read_text() +
            f'\n[workspaces.project]\nmachine="local"\nroot="{work}"\n'
            f'\n[workspaces.container]\nmachine="local"\nroot="{self.root}"\n')
        request = {"machine": "local", "cwd": str(work), "argv": ["true"], "timeout_seconds": 10,
            "expected_head_sha": "", "workspace_access": "exclusive", "resource_keys": ["fixture-resource"],
            "task_id": "", "termination_grace_seconds": 0.5, "tty": False, "terminal_size": None}
        observed = {"worktree": str(work), "workspace_lock_root": str(work)}
        roots = [str(work)]
        if creation:
            subprocess.run(["git", "init", "--quiet", str(work)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(work), "-c", "user.name=Fixture", "-c",
                "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "-c",
                "core.hooksPath=/dev/null", "commit", "--allow-empty", "--quiet", "-m", "Initial"], check=True)
            observed = context(work)
            intent = {"workspace": "project", "destination": str(self.root / "created"), "base_ref": "HEAD",
                "new_branch": "fixture/created", "expected_workspace_identity": observed["workspace_identity"], "worktree": ""}
            plan = worktrees.prepare_creation(load(self.config), "local", **intent)
            request.update(argv=plan["argv"], resource_keys=plan["resource_keys"], worktree_creation=intent)
            observed.update(workspace_lock_root=plan["destination"], creation_plan=plan)
            roots = [plan["destination"], str(self.root)]
        record = {"format_version": 6 if creation else 5, "version": "historical-fixture", "request": request,
            "context": observed, "status": "outcome_unknown", "exit_code": 0, "created_at": 1000.0,
            "finished_at": 1003.0, "log_truncated": False, "output_incomplete": False,
            "io_mode": "pipe", "terminal_size": None, "control_ready": False,
            "control": {"protocol": 1, "seq": 0, "last": None},
            "command": {"spawned": True, "exited": True, "started_at": 1001.0, "exited_at": 1002.0, "exit_signal": None},
            "stop": {"reason": None, "requested_at": None, "grace_seconds": 0.5, "force_used": False},
            "cleanup": {"containment": "posix_session", "status": "unverified", "error": "cleanup_unverified"}}
        if boot_id is not None:
            record["cleanup"].update(session_id=1234, boot_id=boot_id)
        directory = private_directory(private_directory(self.state / "executions") / ("e" * 32))
        save_json(directory / "record.json", record)
        paths = [self.state / "locks" / (directory.name + ".lock"), directory / "control.lock", directory / "worker.lock"]
        paths += [self.state / "locks" / ("workspace-" + hashlib.sha256(root.encode()).hexdigest() + ".lock") for root in roots]
        paths += [self.state / "locks" / ("resource-" + hashlib.sha256(key.encode()).hexdigest() + ".lock") for key in request["resource_keys"]]
        for path in paths:
            with lease(path):
                pass
        self.assertEqual(execution.read_record(directory), record)
        return directory, record, paths

    def historical_task_fixture(self, directory, historical):
        record = task_records.new_record(task_records.normalize_create(task_contract()), 1004000)
        operations = []
        for identity in ("1" * 32, "2" * 32):
            operation = {"id": identity, "stage_id": "verify", "plan_revision": 1, "epoch": 0,
                "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
                "locator": {"kind": "execution", "machine": "local", "target_identity": None,
                            "execution_id": directory.name, "control_seq": None},
                "admitted_at_ms": 1004000, "state": "unknown", "observed_at_ms": 1004001, "result": None}
            operation["result"], _ = task_observation(operation, historical | {"execution_id": directory.name})
            operations.append(operation)
        record.update(operations=operations, revision=2, control_revision=2, updated_at_ms=1004001, handoff=None)
        record["control"].update(state="paused", stop_intent="cancel")
        record["plan"]["stages"][0].update(state="blocked", evidence=[item["id"] for item in operations])
        write_legacy_task(self.state, record)
        return operations

    def run_maintenance(self, record):
        with lease(self.state / "service/operation.lock") as lock:
            maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        return maintenance._read(self.state, record["operation_id"])

    def test_historical_creation_waits_for_bound_kernel_boot_without_repairing_history(self):
        directory, historical, _ = self.historical_execution_fixture(creation=True)
        original = (directory / "record.json").read_bytes()
        operations = self.historical_task_fixture(directory, historical)
        self.assertEqual(operations[0]["result"]["receipt_sha256"],
                         "a48d410a94c7617d20e7e3191125e0909f75c3fa2bf341691140f646fc715955")
        task_path = self.state / "tasks/structured-one/task.json"
        task_bytes = task_path.read_bytes()
        installed = self.state / "service/versions" / self.b / "installation.json"
        save_json(installed, service_state._json(installed) | {"state_contract": service_state.STATE_CONTRACT})
        record = self.operation_fixture()
        _, stop, start = self.activation_mocks()
        with patch.object(maintenance, "_bound"), patch.object(service_runtime, "socket_alive", return_value=False), \
                patch.object(execution_api, "boot_identity", return_value="11111111-1111-1111-1111-111111111111") as boot:
            for _ in range(2):
                with lease(self.state / "service/operation.lock") as lock:
                    maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
                deferred = maintenance._read(self.state, record["operation_id"])
                self.assertEqual((deferred["phase"], deferred["reason"]), ("deferred", "activation_deferred"))
                self.assertIsInstance(deferred["outcome"], dict)
                self.assertEqual(deferred["outcome"]["execution_boots"], {directory.name: {
                    "receipt_sha256": hashlib.sha256(original).hexdigest(), "boot_id": boot.return_value}})
                stop.assert_not_called()
                start.assert_not_called()
            boot.return_value = "22222222-2222-2222-2222-222222222222"
            with lease(self.state / "service/operation.lock") as lock:
                maintenance.run_operation(self.state, record["operation_id"], lock.fileno())
        completed = maintenance._read(self.state, record["operation_id"])
        self.assertEqual(completed["phase"], "completed")
        self.assertEqual(completed["request"], record["request"])
        self.assertEqual(completed["prepared"], record["prepared"])
        self.assertEqual(completed["outcome"]["execution_boots"], deferred["outcome"]["execution_boots"])
        self.assertEqual((directory / "record.json").read_bytes(), original)
        self.assertEqual(execution.read_record(directory), historical)
        self.assertFalse((directory / "reconciliation.json").exists())
        archived = list(self.state.glob("tasks-legacy-*/structured-one/task.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), task_bytes)
        self.assertEqual(tasks.storage_state(self.state)["kind"], "sqlite")
        self.assertEqual(list(tasks.activation_blockers(self.state)), operations)
        for operation in operations:
            self.assertEqual(task_observation(operation, historical | {"execution_id": directory.name})[0], operation["result"])

    def test_native_boot_identity_and_shared_historical_resources_need_no_new_witness(self):
        directory, historical, paths = self.historical_execution_fixture(
            creation=True, boot_id="11111111-1111-1111-1111-111111111111")
        second = private_directory(directory.parent / ("f" * 32))
        independent = historical | {"format_version": 5,
            "request": {key: value for key, value in historical["request"].items() if key != "worktree_creation"},
            "context": {"worktree": str(self.root), "workspace_lock_root": str(self.root)}}
        independent["request"].update(cwd=str(self.root), argv=["true"])
        save_json(second / "record.json", independent)
        for path in (self.state / "locks" / (second.name + ".lock"), second / "worker.lock", second / "control.lock"):
            with lease(path):
                pass
            paths.append(path)
        record = self.operation_fixture()
        _, stop, start = self.activation_mocks()
        def ready(*args):
            for path in paths:
                self.assert_fault("resource_busy", lambda: lease(path, shared=True).__enter__())
            with lease(self.state / "locks/unrelated-resource.lock"):
                pass
            return {"status": "ready"}
        start.side_effect = ready
        with patch.object(maintenance, "_bound"), patch.object(service_runtime, "socket_alive", return_value=False), \
                patch.object(execution_api, "boot_identity", return_value="11111111-1111-1111-1111-111111111111") as boot:
            self.assertEqual(self.run_maintenance(record)["phase"], "deferred")
            stop.assert_not_called()
            boot.return_value = "22222222-2222-2222-2222-222222222222"
            completed = self.run_maintenance(record)
        self.assertEqual(completed["phase"], "completed")
        self.assertNotIn("execution_boots", completed["outcome"])
        for path in paths:
            with lease(path):
                pass
        self.assertEqual(execution.read_record(directory), historical)
        self.assertEqual(execution.read_record(second), independent)

    def test_historical_activation_rejects_busy_original_owners_and_creation_resources(self):
        directory, historical, paths = self.historical_execution_fixture(creation=True,
            boot_id="11111111-1111-1111-1111-111111111111")
        record = self.operation_fixture()
        _, stop, start = self.activation_mocks()
        with patch.object(maintenance, "_bound"), patch.object(execution_api, "boot_identity",
                return_value="22222222-2222-2222-2222-222222222222"):
            for path in paths:
                with self.subTest(path=path.name), lease(path):
                    result = self.run_maintenance(record)
                    self.assertEqual((result["phase"], result["reason"]), ("deferred", "activation_deferred"))
        stop.assert_not_called()
        start.assert_not_called()
        self.assertEqual(execution.read_record(directory), historical)

    def test_historical_activation_rejects_unmatched_task_evidence(self):
        directory, historical, _ = self.historical_execution_fixture(
            boot_id="11111111-1111-1111-1111-111111111111")
        operation = self.historical_task_fixture(directory, historical)[0]
        record = self.operation_fixture()
        _, stop, start = self.activation_mocks()
        cases = [operation | {"result": operation["result"] | {"receipt_sha256": value}} for value in (None, "0" * 64)]
        cases += [operation | {"locator": operation["locator"] | update} for update in (
            {"machine": "other"}, {"execution_id": "0" * 32}, {"target_identity": "0" * 64}, {"control_seq": 1})]
        cases += [operation | {"state": "admitted", "result": None}]
        with patch.object(maintenance, "_bound"), patch.object(execution_api, "boot_identity",
                return_value="22222222-2222-2222-2222-222222222222"):
            for operation in cases:
                with self.subTest(operation=operation), patch.object(tasks, "activation_blockers", return_value=iter([operation])):
                    self.assertEqual(self.run_maintenance(record)["phase"], "deferred")
        stop.assert_not_called()
        start.assert_not_called()

    def test_boot_witness_does_not_admit_changed_corrupt_or_unresolved_receipts(self):
        directory, historical, _ = self.historical_execution_fixture()
        record = self.operation_fixture()
        _, stop, start = self.activation_mocks()
        with patch.object(maintenance, "_bound"), patch.object(execution_api, "boot_identity",
                return_value="11111111-1111-1111-1111-111111111111") as boot:
            witness = self.run_maintenance(record)["outcome"]["execution_boots"]
            boot.return_value = "22222222-2222-2222-2222-222222222222"
            cases = [{"created_at": 999.0}, {"exit_code": None}, {"error": "worker_unverified"}, {"status": "running"},
                     {"cleanup": historical["cleanup"] | {"boot_id": boot.return_value}},
                     {"context": {}}, {"request": historical["request"] | {"resource_keys": None}}]
            for status in ("pending", "partial", "outcome_unknown"):
                cases.append({"control": {"protocol": 1, "seq": 1, "last": {"seq": 1, "action": "write_stdin",
                    "request_sha256": "a" * 64, "status": status, "bytes_written": 1 if status == "partial" else None,
                    "accepted_at": 1002.0, "completed_at": None if status == "pending" else 1003.0, "error": None}}})
            for update in cases:
                with self.subTest(update=update):
                    save_json(directory / "record.json", historical | update)
                    before = (directory / "record.json").read_bytes()
                    current = maintenance._read(self.state, record["operation_id"])
                    maintenance._save(self.state, current, "deferred")
                    result = self.run_maintenance(record)
                    self.assertNotEqual(result["phase"], "completed")
                    self.assertEqual(result["outcome"]["execution_boots"], witness)
                    self.assertEqual((directory / "record.json").read_bytes(), before)
            save_json(directory / "record.json", historical)
            maintenance._save(self.state, maintenance._read(self.state, record["operation_id"]), "deferred")
            boot.side_effect = Fault("cleanup_identity_unavailable", "Fixture kernel identity query failed.")
            self.assertEqual(self.run_maintenance(record)["phase"], "deferred")
        stop.assert_not_called()
        start.assert_not_called()

    def test_boot_evidence_survives_restore_and_is_reused_by_a_new_update(self):
        directory, _, paths = self.historical_execution_fixture()
        original = (directory / "record.json").read_bytes()
        record = self.operation_fixture()
        _, _, start = self.activation_mocks()
        calls = 0
        def ready(*args):
            nonlocal calls
            for path in paths:
                self.assert_fault("resource_busy", lambda: lease(path).__enter__())
            calls += 1
            if calls == 1:
                raise Fault("service_unready", "Fixture activation failed.")
            return {"status": "ready"}
        start.side_effect = ready
        with patch.object(maintenance, "_bound"), patch.object(service_runtime, "socket_alive", return_value=False), \
                patch.object(execution_api, "boot_identity", return_value="11111111-1111-1111-1111-111111111111") as boot:
            deferred = self.run_maintenance(record)
            boot.return_value = "22222222-2222-2222-2222-222222222222"
            restored = self.run_maintenance(record)
            self.assertEqual(restored["phase"], "restored")
            retained = maintenance._operation_path(self.state, record["operation_id"]).read_bytes()
            following = record | {"operation_id": "d" * 32,
                "request": record["request"] | {"selection_sha256": maintenance._selection(self.state)}}
            maintenance._save(self.state, following, "prepared")
            completed = self.run_maintenance(following)
        self.assertEqual(completed["phase"], "completed")
        self.assertEqual(completed["outcome"]["execution_boots"], deferred["outcome"]["execution_boots"])
        self.assertEqual(maintenance._operation_path(self.state, record["operation_id"]).read_bytes(), retained)
        self.assertEqual((directory / "record.json").read_bytes(), original)
        self.assertEqual(calls, 3)

    def test_lost_boot_evidence_acknowledgement_is_read_and_continued_by_original_id(self):
        self.historical_execution_fixture()
        record = self.operation_fixture()
        _, stop, start = self.activation_mocks()
        save = maintenance._save
        def lose_acknowledgement(state, original, phase, **changes):
            saved = save(state, original, phase, **changes)
            if phase == "deferred":
                raise OSError("Fixture lost the publication acknowledgement.")
            return saved
        with patch.object(maintenance, "_bound"), patch.object(service_runtime, "socket_alive", return_value=False), \
                patch.object(execution_api, "boot_identity", return_value="11111111-1111-1111-1111-111111111111") as boot:
            with patch.object(maintenance, "_save", side_effect=lose_acknowledgement), self.assertRaises(OSError):
                self.run_maintenance(record)
            retained = maintenance._read(self.state, record["operation_id"])
            self.assertEqual(retained["phase"], "deferred")
            self.assertEqual(self.run_maintenance(record)["outcome"], retained["outcome"])
            stop.assert_not_called()
            start.assert_not_called()
            boot.return_value = "22222222-2222-2222-2222-222222222222"
            self.assertEqual(self.run_maintenance(record)["phase"], "completed")

    def test_malformed_or_conflicting_maintenance_boot_evidence_is_rejected(self):
        directory, _, _ = self.historical_execution_fixture()
        record = self.operation_fixture()
        path = maintenance._operation_path(self.state, record["operation_id"])
        witness = {"receipt_sha256": hashlib.sha256((directory / "record.json").read_bytes()).hexdigest(),
                   "boot_id": "11111111-1111-1111-1111-111111111111"}
        for changed in (None, [], {directory.name: witness | {"boot_id": "not-a-boot"}},
                        {directory.name: witness | {"receipt_sha256": ""}}):
            with self.subTest(changed=changed):
                save_json(path, record | {"outcome": {"execution_boots": changed}})
                self.assert_fault("maintenance_state_invalid", lambda: maintenance._read(self.state, record["operation_id"]))
        save_json(path, record | {"outcome": {"execution_boots": {directory.name: witness}}})
        other = record | {"operation_id": "d" * 32, "outcome": {"execution_boots": {
            directory.name: witness | {"boot_id": "22222222-2222-2222-2222-222222222222"}}}}
        maintenance._save(self.state, other, "restored")
        self.assert_fault("maintenance_state_invalid", lambda: maintenance._execution_boots(self.state))

    def test_retired_update_keeps_boot_evidence_and_does_not_rebind_its_payload(self):
        self.historical_execution_fixture()
        record = self.operation_fixture()
        self.activation_mocks()
        with patch.object(maintenance, "_bound"), patch.object(service_runtime, "socket_alive", return_value=False), \
                patch.object(execution_api, "boot_identity", return_value="11111111-1111-1111-1111-111111111111") as boot:
            deferred = self.run_maintenance(record)
            with patch.object(maintenance, "observe", return_value={"status": "ready", "current": self.a}):
                retired = maintenance.retire_operation(self.config, self.state, record["operation_id"])["operation"]
            self.assertEqual(retired["request"], record["request"])
            self.assertEqual(retired["prepared"], record["prepared"])
            retained = maintenance._operation_path(self.state, record["operation_id"]).read_bytes()
            following = record | {"operation_id": "d" * 32,
                "request": record["request"] | {"payload_sha256": "f" * 64, "source": str(self.root / "new-payload")},
                "prepared": record["prepared"] | {"payload_sha256": "f" * 64}}
            maintenance._save(self.state, following, "prepared")
            boot.return_value = "22222222-2222-2222-2222-222222222222"
            completed = self.run_maintenance(following)
        self.assertEqual(completed["phase"], "completed")
        self.assertEqual(completed["request"], following["request"])
        self.assertEqual(completed["outcome"]["execution_boots"], deferred["outcome"]["execution_boots"])
        self.assertEqual(maintenance._operation_path(self.state, record["operation_id"]).read_bytes(), retained)

    def test_boot_evidence_continues_original_activation_after_task_cutover_failure(self):
        directory, historical, _ = self.historical_execution_fixture()
        operations = self.historical_task_fixture(directory, historical)
        record = self.operation_fixture()
        installed = self.state / "service/versions" / self.b / "installation.json"
        save_json(installed, service_state._json(installed) | {"state_contract": service_state.STATE_CONTRACT,
                  "management_contract": 1, "payload_sha256": record["request"]["payload_sha256"]})
        _, _, start = self.activation_mocks()
        start.side_effect = [Fault("service_unready", "Fixture lost readiness after cutover."), {"status": "ready"}]
        with patch.object(maintenance, "_bound"), patch.object(service_runtime, "socket_alive", return_value=False), \
                patch.object(service_state, "_bind_management", side_effect=lambda state, selected, identity: selected), \
                patch.object(execution_api, "boot_identity", return_value="11111111-1111-1111-1111-111111111111") as boot:
            deferred = self.run_maintenance(record)
            boot.return_value = "22222222-2222-2222-2222-222222222222"
            pending = self.run_maintenance(record)
            self.assertEqual(pending["phase"], "recovery_required")
            self.assertEqual(pending["outcome"]["execution_boots"], deferred["outcome"]["execution_boots"])
            self.assertEqual(maintenance.read_operation(self.config, self.state, record["operation_id"])["status"], "continuation_required")
            completed = self.run_maintenance(record)
        self.assertEqual(completed["phase"], "completed")
        self.assertEqual(completed["request"], record["request"])
        self.assertEqual(completed["prepared"], record["prepared"])
        self.assertEqual(completed["outcome"]["execution_boots"], deferred["outcome"]["execution_boots"])
        self.assertEqual(list(tasks.activation_blockers(self.state)), operations)
        self.assertEqual(execution.read_record(directory), historical)

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
            def effect(state, selected, identity, snapshot, *, before_change, require_safe, execution_boots):
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
