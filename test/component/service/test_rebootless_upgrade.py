"""Controller upgrade admission must not reclassify unknown native work."""

from contextlib import ExitStack
import hashlib
import json
import sys
import time
import unittest
from unittest.mock import patch

from openmachine import __version__, execution, tasks
from openmachine.app import task_receipts
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import checks
from openmachine.workspace.git import context
from test.support.runtime_cases import RuntimeCaseFixture


class RebootlessUpgradeTests(RuntimeCaseFixture, unittest.TestCase):
    def cleanup_unknown(self, *, original_id="d" * 32, resources=None, retain_identity=False):
        # Synthetic original facts avoid dependence on macOS process-query races.
        directory = private_directory(self.state / "executions" / original_id)
        private_directory(self.state / "locks")
        now = time.time()
        root = str(self.work)
        resources = sorted(resources or [])
        record = {
            "request": {
                "machine": "my-laptop", "cwd": root,
                "argv": [sys.executable, "-c", "print('settled')"],
                "timeout_seconds": 5, "expected_head_sha": "",
                "workspace_access": "exclusive", "resource_keys": resources,
                "task_id": "", "termination_grace_seconds": 5.0,
                "tty": False, "terminal_size": None,
            },
            "context": context(self.work) | {"workspace_lock_root": root},
            "version": __version__, "format_version": execution.FORMAT,
            "status": "outcome_unknown", "exit_code": 0,
            "created_at": now - 2, "finished_at": now,
            "log_truncated": False, "output_incomplete": False,
            "io_mode": "pipe", "terminal_size": None,
            "control_ready": False, "control": None, "error": None,
            "command": {"spawned": True, "exited": True, "started_at": now - 2,
                        "exited_at": now - 1, "exit_signal": None},
            "stop": {"reason": None, "requested_at": None,
                     "grace_seconds": 5.0, "force_used": False},
            "cleanup": {"containment": "posix_session", "status": "unverified",
                        "error": "cleanup_query_failed"},
        }
        if retain_identity:
            record["cleanup"].update(session_id=9999, boot_id="11111111-2222-3333-4444-555555555555")
        for path in [
            self.state / "locks" / (original_id + ".lock"),
            directory / "control.lock", directory / "worker.lock",
            self.state / "locks" / ("workspace-" + hashlib.sha256(root.encode()).hexdigest() + ".lock"),
            *(self.state / "locks" / ("resource-" + hashlib.sha256(name.encode()).hexdigest() + ".lock")
              for name in resources),
        ]:
            with lease(path):
                pass
        save_json(directory / "record.json", record)
        execution.read_record(directory)
        original = (directory / "record.json").read_bytes()
        receipt = execution.read(self.config, self.state, "my-laptop", original_id)
        operation = {"locator": {"kind": "execution", "control_seq": None}, "result": None}
        result, _ = task_receipts.observation(operation, receipt)
        self.assertEqual(result["status"], "unknown")
        task_operation = {"state": "unknown", "locator": {
            "kind": "execution", "machine": "my-laptop", "target_identity": None,
            "execution_id": original_id, "control_seq": None}, "result": result}
        return directory, original, task_operation

    def admit(self, operations, *, candidate_config=None):
        with ExitStack() as guards, patch.object(tasks, "activation_blockers", return_value=operations):
            checks.activation_safe(self.state, config=self.config,
                                   candidate_config=candidate_config or self.config, guards=guards)

    def test_cleanup_only_unknown_upgrade_requires_no_reboot_and_retains_original_history(self):
        directory, before, task = self.cleanup_unknown()
        with patch.object(tasks, "activation_blockers", return_value=[task]):
            with self.assertRaises(Fault) as error:
                checks.activation_safe(self.state)
            self.assertEqual(error.exception.code, "activation_deferred")
            with ExitStack() as guards:
                checks.activation_safe(self.state, config=self.config,
                                       candidate_config=self.config, guards=guards)
                root = json.loads(before)["context"]["workspace_lock_root"]
                workspace_lock = self.state / "locks" / (
                    "workspace-" + hashlib.sha256(root.encode()).hexdigest() + ".lock")
                with self.assertRaises(Fault) as busy:
                    with lease(workspace_lock, create=False):
                        pass
                self.assertEqual(busy.exception.code, "resource_busy")
        self.assertEqual((directory / "record.json").read_bytes(), before)
        self.assertFalse((directory / "reconciliation.json").exists())
        self.assertEqual(execution.read(self.config, self.state, "my-laptop", "d" * 32)["status"], "outcome_unknown")

    def test_original_locks_must_exist_and_be_idle_even_when_command_exit_is_known(self):
        directory, before, task = self.cleanup_unknown(resources=["upgrade-resource"])
        resource = self.state / "locks" / (
            "resource-" + hashlib.sha256(b"upgrade-resource").hexdigest() + ".lock")
        with lease(resource, create=False):
            with self.assertRaises(Fault) as error:
                self.admit([task])
            self.assertEqual(error.exception.code, "activation_deferred")
        self.assertEqual((directory / "record.json").read_bytes(), before)
        resource.unlink()
        with self.assertRaises(Fault) as error:
            self.admit([task])
        self.assertEqual(error.exception.code, "activation_deferred")
        self.assertFalse(resource.exists())

    def test_candidate_must_retain_local_machine_for_unknown_execution_and_task(self):
        directory, original, task = self.cleanup_unknown()
        candidates = {
            "removed": Settings.model_validate({
                "schema_version": 1, "machines": {"replacement": {"transport": "local"}}}),
            "rebound_to_ssh": Settings.model_validate({
                "schema_version": 1,
                "machines": {"my-laptop": {"transport": "ssh", "ssh_alias": "replacement",
                             "ssh_worker": "/workers/" + "a" * 64 + ".pyz",
                             "ssh_identity": "b" * 64}}}),
        }
        for name, candidate in candidates.items():
            with self.subTest(name=name), self.assertRaises(Fault) as error:
                self.admit([task], candidate_config=candidate)
            self.assertEqual(error.exception.code, "activation_deferred")
        # Disabling future command admission must not invalidate read-only
        # access to an existing local execution.
        readonly_candidate = Settings.model_validate({
            "schema_version": 1, "machines": {"my-laptop": {"transport": "local"}},
            "workspaces": {"project": {"machine": "my-laptop", "root": str(self.work)}}})
        self.admit([task], candidate_config=readonly_candidate)
        self.assertEqual((directory / "record.json").read_bytes(), original)

    def test_wrong_execution_digest_machine_or_control_cannot_waive_task_blocker(self):
        directory, original, task = self.cleanup_unknown()
        altered = [
            {**task, "result": {**task["result"], "receipt_sha256": "f" * 64}},
            {**task, "locator": {**task["locator"], "machine": "another-machine"}},
            {**task, "locator": {**task["locator"], "control_seq": 1}},
            {**task, "state": "admitted"},
        ]
        for operation in altered:
            with self.subTest(operation=operation), self.assertRaises(Fault) as error:
                self.admit([operation])
            self.assertEqual(error.exception.code, "activation_deferred")
        self.assertEqual((directory / "record.json").read_bytes(), original)

    def test_unknown_command_result_remains_a_hard_blocker(self):
        directory, _, task = self.cleanup_unknown()
        record = json.loads((directory / "record.json").read_text())
        record["command"]["exited"] = False
        record["command"]["exited_at"] = None
        record["exit_code"] = None
        save_json(directory / "record.json", record)
        with self.assertRaises(Fault) as error:
            self.admit([task])
        self.assertEqual(error.exception.code, "activation_deferred")
        self.assertEqual(execution.read(self.config, self.state, "my-laptop", "d" * 32)["status"], "outcome_unknown")

    def test_reconciled_original_task_digest_still_matches_without_rewriting_task(self):
        directory, original, task = self.cleanup_unknown(retain_identity=True)
        record = json.loads(original)
        save_json(directory / "reconciliation.json", {
            "format": 1, "execution_id": "d" * 32,
            "receipt_sha256": hashlib.sha256(original).hexdigest(),
            "observed_at": max(time.time(), record["finished_at"]),
            "created_worktree": None,
        })
        removed = Settings.model_validate({"schema_version": 1, "machines": {"other": {"transport": "local"}}})
        with self.assertRaises(Fault) as error:
            self.admit([task], candidate_config=removed)
        self.assertEqual(error.exception.code, "activation_deferred")
        self.admit([task])
        # Once Task no longer retains an unknown observation, a reconciled
        # terminal receipt should not block an unrelated configuration change.
        self.admit([], candidate_config=removed)
        self.assertEqual((directory / "record.json").read_bytes(), original)
        self.assertEqual(execution.read(self.config, self.state, "my-laptop", "d" * 32)["status"], "succeeded")

    def test_multiple_historical_records_share_the_same_original_lease(self):
        first, first_bytes, _ = self.cleanup_unknown(original_id="c" * 32)
        second, second_bytes, _ = self.cleanup_unknown(original_id="d" * 32)
        self.admit([])
        self.assertEqual((first / "record.json").read_bytes(), first_bytes)
        self.assertEqual((second / "record.json").read_bytes(), second_bytes)


if __name__ == "__main__":
    unittest.main()
