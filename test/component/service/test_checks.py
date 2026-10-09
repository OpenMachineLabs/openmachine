"""Tests for service behavior at the component layer."""

import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import call, patch
from openmachine import service, tasks
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import checks as service_checks
from openmachine.service import install as service_install
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from openmachine.service import transition as service_transition
from openmachine.tasks import database as task_database, migration as task_migration, records as task_records
from openmachine.tasks.index import structured_metadata
from test.support.service_cases import ServiceCaseFixture
from test.support.tasks import write_legacy_checkpoint, write_legacy_task


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_previous_reader_remains_compatible_without_execution_reconciliation(self):
        proof = service.check_state(self.config, self.state)
        self.assertEqual(proof["execution_reconciliations"], 0)
        previous = {key: value for key, value in proof.items() if not key.startswith("execution_reconcil")}
        previous["state_contract"] = 19
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(previous), "")):
            self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state), previous)

    def legacy_task_fixture(self, request=None):
        from openmachine.tasks import records
        from test.support.tasks import task_contract
        value = records.new_record(records.normalize_create(request or task_contract()), time.time_ns() // 1000000)
        return {"record": write_legacy_task(self.state, value)}

    def test_sqlite_candidate_requires_same_authority_and_supported_actual_engine(self):
        tasks.migrate(self.state)
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["task_storage"]["kind"]), (21, "sqlite"))
        storage = proof["task_storage"]
        for delta in ({"state_contract": 17}, {"task_storage": None},
                      {"task_storage": storage | {"identity": "f" * 32}},
                      {"task_storage": storage | {"schema": 999}},
                      {"task_storage": storage | {"fenced": False}},
                      {"task_storage": storage | {"active": False}},
                      {"sqlite_runtime": proof["sqlite_runtime"] | {"supported": False}}):
            with self.subTest(delta=delta), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(
                    Path(sys.executable), self.config, self.state))
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof), "")):
            self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state), proof)
        self.assertEqual(tasks.storage_state(self.state), storage)

    def test_retired_events_archive_is_passive_and_blocks_reactivation(self):
        value, digest = self.retired_events_fixture()
        path, lock = self.state / "events.json", self.state / "locks/events.lock"
        before, inode, lock_inode = path.read_bytes(), path.stat().st_ino, lock.stat().st_ino
        proof = service.check_state(self.config, self.state)
        self.assertTrue(proof["events_retired"])
        self.assertEqual((proof["events_subscriptions"], proof["events_sha256"]), (1, digest))
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state), proof)
        self.assertNotIn("whsec_", json.dumps(proof))
        self.assertNotIn("callback.example", json.dumps(proof))
        legacy = {key: item for key, item in proof.items() if key != "events_retired"}
        for candidate in (legacy, proof | {"events_retired": False}, proof | {"events_retired": 1},
                          proof | {"events_sha256": "0" * 64}, proof | {"events_present": False}):
            with self.subTest(candidate=candidate), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(candidate), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(
                    Path(sys.executable), self.config, self.state))
        self.assertEqual((path.read_bytes(), path.stat().st_ino, lock.stat().st_ino), (before, inode, lock_inode))

    def test_retired_events_archive_rejects_corrupt_unsafe_or_missing_history(self):
        self.retired_events_fixture()
        path, lock = self.state / "events.json", self.state / "locks/events.lock"
        original = path.read_bytes()
        for raw in (b'{"format":1,"format":1}', b'{}', b'[]', b'\xff', b'x' * 524289,
                    b'{"format":true,"subscriptions":{},"connections":{}}',
                    b'{"format":1,"subscriptions":[],"connections":{}}'):
            with self.subTest(raw_size=len(raw)):
                path.write_bytes(raw)
                self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
                self.assertEqual(path.read_bytes(), raw)
        path.write_bytes(original)
        path.chmod(0o644)
        self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
        path.chmod(0o600)
        target = self.state / "archive-backup"
        path.rename(target)
        path.symlink_to(target)
        self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
        path.unlink()
        self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
        target.rename(path)
        lock.unlink()
        self.assert_fault("events_corrupt", lambda: service_checks._events_state(self.state))
        self.assertFalse(lock.exists())
        self.assertEqual(path.read_bytes(), original)

    def test_retired_archive_busy_lock_is_not_replaced_or_repaired(self):
        self.retired_events_fixture()
        path = self.state / "events.json"
        before = path.read_bytes()
        with lease(self.state / "locks/events.lock"):
            self.assert_fault("resource_busy", lambda: service_checks._events_state(self.state))
        self.assertEqual(path.read_bytes(), before)

    def test_managed_probe_binds_child_parent_and_overall_deadline(self):
        with patch.object(service_checks, "probe_service", return_value={"status": "ready", "pid": 123}) as probe, \
                patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, "456\n", "")):
            self.assertEqual(service_checks._managed_probe(self.state, self.record, 456)["supervisor_pid"], 456)
            self.assertEqual(probe.call_args.kwargs["timeout_seconds"], 5)
            self.assert_fault("service_identity_mismatch", lambda: service_checks._managed_probe(self.state, self.record, 999))
            with patch.object(service_runtime.time, "monotonic", side_effect=[100, 105]):
                self.assert_fault("service_unready", lambda: service_checks._managed_probe(self.state, self.record, 456))

    def test_state_check_uses_actual_task_reader_and_preserves_bytes(self):
        write_legacy_checkpoint(self.state, "original", "Preserve original execution evidence.\n", "Original task", [])
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

    def test_actual_task_reader_covers_terminal_history_and_rejects_partial_candidate_proof(self):
        from test.support.tasks import task_contract, terminal_task_fixture
        request = task_contract()
        created = self.legacy_task_fixture(request)
        path = self.state / "tasks/structured-one/task.json"
        write_legacy_task(self.state, terminal_task_fixture(created["record"]))
        raw = path.read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["task_records"], proof["structured_task_records"]), (21, 1, 1))
        actual = service_checks._candidate_state(Path(sys.executable), self.config, self.state, 21)
        self.assertEqual(actual, proof)
        for change in ({"state_contract": 14}, {"structured_task_formats_present": [1, 2]},
                       {"structured_task_read_formats": [1, 2]}, {"structured_task_records": 0}, {"task_records": True},
                       {"task_store_present": 1}, {"structured_task_read_formats": [True]},
                       {"structured_task_read_formats": [1, 1]}, {"task_index_coverage": "incomplete"},
                       {"task_index_generation": proof["task_index_generation"] - 1}):
            with self.subTest(change=change), patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(proof | change), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(Path(sys.executable), self.config, self.state))
        self.assertEqual(path.read_bytes(), raw)
        path.unlink()
        self.assert_fault("corrupt_tasks", lambda: service.check_state(self.config, self.state))

    def test_candidate_resamples_a_concurrent_task_generation_once(self):
        from test.support.tasks import task_contract
        request = task_contract()
        tasks.create(self.state, request.pop("task_id"), **request)
        before = service.check_state(self.config, self.state)
        generations = []

        def candidate(*args, **kwargs):
            if not generations:
                tasks.save(self.state, "new-task", 0, "New task", "A concurrent legitimate publication.\n", [])
            value = before if not generations else service.check_state(self.config, self.state)
            generations.append(value["task_index_generation"])
            return subprocess.CompletedProcess([], 0, json.dumps(value), "")

        selection = service_state._record(self.state)
        with patch.object(service_checks, "_run", side_effect=candidate) as run, \
                patch.object(service_checks.time, "sleep") as sleep:
            observed = service_checks._candidate_state(Path(sys.executable), self.config, self.state)
        self.assertEqual(observed, service.check_state(self.config, self.state))
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()
        self.assertNotEqual(*generations)
        self.assertEqual(service_state._record(self.state), selection)

    def test_candidate_generation_churn_is_bounded_and_fails_closed(self):
        from test.support.tasks import task_contract
        request = task_contract()
        tasks.create(self.state, request.pop("task_id"), **request)
        attempts = []

        def moving_candidate(*args, **kwargs):
            old = service.check_state(self.config, self.state)
            attempts.append(old["task_index_generation"])
            tasks.save(self.state, "moving-" + str(len(attempts)), 0,
                       "Moving task", "Concurrent task publication.\n", [])
            return subprocess.CompletedProcess([], 0, json.dumps(old), "")

        selection = service_state._record(self.state)
        with patch.object(service_checks, "_run", side_effect=moving_candidate) as run, \
                patch.object(service_checks.time, "sleep") as sleep:
            self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(
                Path(sys.executable), self.config, self.state))
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(set(attempts)), 3)
        self.assertEqual(service_state._record(self.state), selection)

    def test_scoped_task_history_requires_contract_16_and_complete_actual_reader(self):
        from test.support.tasks import task_contract
        from openmachine.tasks import records
        repository = "fixture/project"
        binding = {"connection_id": "a" * 32, "account_id": 1, "authorization_revision": 1}
        request = task_contract()
        request["contract"].update(repositories=[repository], github_connections={repository: binding})
        created = self.legacy_task_fixture(request)
        value = created["record"]
        self.assertEqual(value["format"], 4)
        value.update(revision=value["revision"] + 1, updated_at_ms=value["updated_at_ms"] + 1)
        value["control_revision"] = value["revision"]
        operation_id = "b" * 32
        scope = {"resource": "object", "number": 17, "parent_number": None, "fields": ["labels"]}
        value["operations"] = [{"id": operation_id, "stage_id": "verify", "plan_revision": 1, "epoch": 0,
            "request_sha256": "b" * 64, "source_sha256": value["plan"]["stages"][0]["source_sha256"],
            "locator": {"kind": "github", "repository": repository, "object_type": "pull_request",
                "number": 17, "parent_number": None, "anchor": "om-task:structured-one:" + operation_id,
                "connection": binding}, "admitted_at_ms": value["updated_at_ms"], "state": "unknown",
            "observed_at_ms": value["updated_at_ms"], "result": {"status": "unknown", "receipt_sha256": None,
                "summary": "Synthetic retained dispatch uncertainty.", "log_offset": None,
                "failure_code": "github_response_unknown", "effect": "effect_unknown"}}]
        value["effect_scopes"] = [{"id": "c" * 32, "operation_id": operation_id,
            "request_sha256": "b" * 64, "scope": scope, "principal_ref": "fixture:owner",
            "at_ms": value["updated_at_ms"]}]
        path = self.state / "tasks/structured-one/task.json"
        write_legacy_task(self.state, records.Record.model_validate(value).model_dump())
        paths = [path, self.state / "tasks/index.json"]
        before = [item.read_bytes() for item in paths]
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["structured_task_formats_present"]), (21, [4]))
        self.assertIn(4, proof["structured_task_read_formats"])
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state, 21), proof)
        for delta in ({"state_contract": 15}, {"structured_task_read_formats": [1, 2, 3]},
                      {"structured_task_formats_present": []}, {"structured_task_formats_present": [3]},
                      {"structured_task_formats_present": [4.0]}, {"structured_task_formats_present": [4, 4]}):
            with self.subTest(delta=delta), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(
                    Path(sys.executable), self.config, self.state))
        self.assertEqual([item.read_bytes() for item in paths], before)

    def test_format_3_history_keeps_directional_contract_15_compatibility(self):
        from test.support.tasks import task_contract
        from openmachine.tasks import records
        request = task_contract()
        created = self.legacy_task_fixture(request)
        value = created["record"]
        value["format"] = 3
        value.pop("control_revision")
        value.pop("effect_scopes")
        path = self.state / "tasks/structured-one/task.json"
        write_legacy_task(self.state, records.Record.model_validate(value).model_dump())
        before = path.read_bytes()
        proof = service.check_state(self.config, self.state)
        older = proof | {"state_contract": 15, "structured_task_read_formats": [1, 2, 3]}
        self.assertEqual(proof["structured_task_formats_present"], [3])
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(older), "")):
            self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state, 15), older)
        self.assertEqual(path.read_bytes(), before)

    def test_candidate_ignoring_canonical_tasks_is_rejected_before_healthy_service_stop(self):
        from test.support.tasks import task_contract
        request = task_contract()
        self.legacy_task_fixture(request)
        path = self.state / "service/versions" / self.b / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 6})
        ignored = subprocess.CompletedProcess([], 0, json.dumps({"protocol": 1, "state_contract": 6}), "")
        original = (self.state / "tasks/structured-one/task.json").read_bytes()
        with patch.object(service_install, "_verify_candidate"), patch.object(service_checks, "_run", return_value=ignored), \
                patch.object(service_runtime, "_stop") as stop, patch.object(service_runtime, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service_transition._activate(self.state, self.record, self.b))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)
        self.assertEqual((self.state / "tasks/structured-one/task.json").read_bytes(), original)

    def test_retained_ssh_budget_requires_actual_reader_before_service_stop(self):
        from integrations.ssh import recovery
        recovery.replace(self.state, "c" * 64, None, {"episode_id": "d" * 32,
            "started_at_ms": 1, "deadline_ms": 3600001, "attempts": 60,
            "next_attempt_ms": 3600001, "reason_code": "ssh_transport_unavailable"})
        before = (self.state / "ssh-recovery.json").read_bytes()
        proof = service.check_state(self.config, self.state)
        self.assertEqual((proof["state_contract"], proof["ssh_recovery_targets"]), (21, 1))
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state, 21), proof)
        for delta in ({"state_contract": 6}, {"ssh_recovery_present": False},
                      {"ssh_recovery_targets": 0}, {"ssh_recovery_targets": True},
                      {"ssh_recovery_sha256": "e" * 64}, {"ssh_recovery_format": 2}):
            with self.subTest(delta=delta), patch.object(service_install, "_verify_candidate"), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")), \
                    patch.object(service_runtime, "_stop") as stop:
                self.assert_fault("incompatible_state", lambda: service_transition._activate(self.state, self.record, self.b))
                stop.assert_not_called()
        self.assertEqual((self.state / "ssh-recovery.json").read_bytes(), before)

    def test_incompatible_receipt_never_becomes_empty_history(self):
        directory = private_directory(self.state / "executions" / ("c" * 32))
        save_json(directory / "record.json", {"status": "running", "format_version": 999})
        self.assert_fault("unsupported_state_version", lambda: service.check_state(self.config, self.state))
        self.assertTrue((directory / "record.json").exists())

    def test_candidate_state_rejects_failure_or_protocol_drift(self):
        for value, code in (({"error": "unsupported_task_format"}, 1),
                            ({"protocol": 2, "state_contract": 1}, 0)):
            with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], code, json.dumps(value), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(Path(sys.executable), self.config, self.state))

    def test_directional_metadata_and_candidate_contract_are_explicit(self):
        path = self.state / "service/versions" / self.b / "installation.json"
        original = service_state._json(path)
        for contract in range(1, service_state.STATE_CONTRACT + 1):
            save_json(path, original | {"state_contract": contract})
            self.assertEqual(service_state._installed(self.state, self.b)["state_contract"], contract)
            reply = {"protocol": 1, "state_contract": contract}
            if contract >= 18:
                reply.update(task_storage=tasks.storage_state(self.state), sqlite_runtime=tasks.sqlite_runtime())
            with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")):
                self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state, contract), reply)
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(
                    Path(sys.executable), self.config, self.state, 1 if contract != 1 else 3))
        for contract in (0, service_state.STATE_CONTRACT + 1, True, "3"):
            save_json(path, original | {"state_contract": contract})
            self.assert_fault("incompatible_runtime", lambda: service_state._installed(self.state, self.b))

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
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof), "")) as call:
            self.assertEqual(service_checks._remote_workspace_state(python, self.config, "remote"), proof)
            self.assertEqual(call.call_args.args[0], [str(python), "-I", "-m", "openmachine.cli", "--config", str(self.config),
                                                       "doctor", "--machine", "remote"])
        for delta in ({"workspace_change_format": True}, {"workspace_change_store_present": False},
                      {"target_identity": "f" * 64}, {"legacy_workspace_compatible": None}):
            with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_remote_state", lambda: service_checks._remote_workspace_state(python, self.config, "remote"))
        # Missing unrelated source is not unreadable target history or a reason to disrupt a healthy service.
        missing = proof | {"unavailable_workspaces": ["unrelated"]}
        with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 1, json.dumps(missing), "")):
            self.assertEqual(service_checks._remote_workspace_state(python, self.config, "remote"), missing)

    def test_actual_local_change_reader_blocks_old_candidate_that_ignores_store(self):
        from openmachine import workspace
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\nexecution_enabled=true\n'
                               '[workspaces.project]\nmachine="local"\nroot=' + json.dumps(str(self.root)) + '\n')
        config = service_checks.load(self.config)
        file = self.root / "source.py"
        file.write_text("answer = 41")
        read = workspace.read_text_file(config, "local", "project", "source.py")
        changed = workspace.edit_text_file(config, self.state, "local", "project", "source.py",
            [{"old_text": "41", "new_text": "42"}], read["context"]["workspace_identity"], "", read["sha256"], "d" * 32)
        self.assertEqual(changed["status"], "applied")
        record = self.state / "workspace-changes" / ("d" * 32) / "record.json"
        retained = record.read_bytes()
        ignored = subprocess.CompletedProcess([], 0, json.dumps({"protocol": 1, "state_contract": 1}), "")
        with patch.object(service_install, "_verify_candidate"), patch.object(service_checks, "_run", return_value=ignored), \
             patch.object(service_runtime, "_stop") as stop, patch.object(service_runtime, "_start") as start:
            self.assert_fault("incompatible_state", lambda: service_transition._activate(self.state, self.record, self.b))
            stop.assert_not_called()
            start.assert_not_called()
        self.assertEqual(record.read_bytes(), retained)
        self.assertEqual(service_state._record(self.state), self.record)

    def test_change_format_coverage_is_directional_and_requires_actual_readers(self):
        from openmachine import workspace
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\nworkspace_write_enabled=true\n'
                               '[workspaces.project]\nmachine="local"\nroot='+json.dumps(str(self.root))+'\n')
        config = service_checks.load(self.config)
        file = self.root / "source.py"
        file.write_text("a\n")
        read = workspace.read_text_file(config,"local","project","source.py")
        first = workspace.edit_text_file(config,self.state,"local","project","source.py",
            [{"old_text":"a","new_text":"b"}],read["context"]["workspace_identity"],"",read["sha256"],"1"*32)
        self.assertEqual(first["format"],1)
        legacy = {"protocol":1,"state_contract":4,"workspace_change_format":1,
                  "workspace_change_store_present":True,"workspace_change_records":1}
        with patch.object(service_checks,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(legacy),"")):
            self.assertEqual(service_checks._candidate_state(Path(sys.executable),self.config,self.state),legacy)
        second = workspace.apply_patch(config,self.state,"local","project",
            "*** Begin Patch\n*** Add File: new\n+x\n*** End Patch\n",read["context"]["workspace_identity"],"",
            [{"path":"new","expected_sha256":""}],change_id="2"*32)
        self.assertEqual(second["format"],2)
        legacy["workspace_change_records"] = 2
        with patch.object(service_checks,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(legacy),"")):
            self.assert_fault("incompatible_state",lambda:service_checks._candidate_state(Path(sys.executable),self.config,self.state))
        current = service.check_state(self.config,self.state)
        self.assertEqual(current["workspace_change_formats_present"],[1,2])
        records = list((self.state / "workspace-changes").glob("*/record.json"))
        before = [path.read_bytes() for path in records]
        with patch.object(service_checks,"_run",return_value=subprocess.CompletedProcess([],0,json.dumps(current),"")):
            service_checks._candidate_state(Path(sys.executable),self.config,self.state)
        self.assertEqual([path.read_bytes() for path in records],before)

    def test_unfenced_activation_reads_new_legacy_operations_and_rejects_dual_authority(self):
        record = self.legacy_task_fixture()["record"]
        with patch.object(task_migration, "_verify_import", side_effect=Fault("task_migration_incomplete", "Fixture failed import.")):
            self.assert_fault("task_migration_incomplete", lambda: tasks.migrate(self.state))
        service_checks.activation_safe(self.state)
        now = time.time_ns() // 1000000
        operation = {"id": "a" * 32, "stage_id": "verify", "plan_revision": 1, "epoch": 0,
            "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
            "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
                "execution_id": "a" * 32, "control_seq": None},
            "admitted_at_ms": now, "state": "unknown", "observed_at_ms": now,
            "result": {"status": "unknown", "receipt_sha256": None, "summary": "Original unresolved work.", "log_offset": None}}
        record["operations"] = [operation]
        record.update(revision=2, control_revision=2, updated_at_ms=now)
        record["control"].update(state="paused", stop_intent="pause")
        record["plan"]["stages"][0].update(state="blocked", evidence=[operation["id"]])
        record["handoff"] = None
        path = self.state / "tasks/structured-one/task.json"
        with lease(self.state / "locks/tasks.lock"):
            path.write_bytes(task_records.canonical(record))
            index_path = self.state / "tasks/index.json"
            index = json.loads(index_path.read_text())
            index["tasks"][record["task_id"]] = structured_metadata(record, path.read_bytes())
            index["generation"] += 1
            save_json(index_path, index)
        original = path.read_bytes()
        self.assertEqual(list(tasks.activation_blockers(self.state)), [operation])
        self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        self.assertEqual(path.read_bytes(), original)
        path.write_bytes(b"{}")
        self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        path.write_bytes(original)
        with task_database.connect(self.state, maintenance=True, write=True) as connection:
            connection.execute("UPDATE store SET active=1")
        self.assert_fault("corrupt_tasks", lambda: list(tasks.activation_blockers(self.state)))
        self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))

    def test_product_admission_defers_actual_unresolved_task_and_execution_readers(self):
        from test.support.tasks import task_contract
        from openmachine.tasks import records
        request = task_contract()
        created = self.legacy_task_fixture(request)
        service_checks.activation_safe(self.state)
        # Compatibility still uses the actual canonical reader. An incomplete
        # canonical write cannot become an empty or safe task store.
        path = self.state / "tasks/structured-one/task.json"
        original = path.read_bytes()
        path.write_bytes(b'{}')
        self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        path.write_bytes(original)
        with patch.object(service_checks.tasks, "activation_blockers", return_value=[{"state": "unknown", "result": None}]):
            self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        historical = [
            {"state": "unknown", "locator": {"kind": "github"},
             "result": {"status": "unknown", "receipt_sha256": "a" * 64,
                        "summary": "Original provider outcome is unresolved; no request was repeated."}},
            {"state": "unknown", "locator": {"kind": "change"},
             "result": {"status": "unknown", "receipt_sha256": None, "summary": "Domain failure: invalid_edits."}},
        ]
        with patch.object(service_checks.tasks, "activation_blockers", return_value=historical):
            service_checks.activation_safe(self.state)
        for operation in (
                {"state": "unknown", "locator": {"kind": "change"},
                 "result": {"status": "unknown", "receipt_sha256": None, "summary": "Original change outcome is unresolved."}},
                {"state": "unknown", "locator": {"kind": "execution"},
                 "result": {"status": "unknown", "receipt_sha256": None, "summary": "Original execution outcome is unresolved."}}):
            with self.subTest(operation=operation), patch.object(service_checks.tasks, "activation_blockers", return_value=[operation]):
                self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        private_directory(self.state / "executions/original")
        with patch.object(service_checks.execution, "read_record", return_value={"status": "running"}):
            self.assert_fault("activation_deferred", lambda: service_checks.activation_safe(self.state))
        self.assertEqual(path.read_bytes(), records.canonical(created["record"]))


if __name__ == "__main__":
    unittest.main()
