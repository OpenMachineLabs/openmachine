"""Tests for workspace behavior at the resilience layer."""

import hashlib
import json
import sys
import time
import unittest
import uuid
from openmachine import execution
from openmachine.config import Workspace
from openmachine.foundation.state import lease
from openmachine.execution import api as execution_api
from test.support.workspace import WorkspaceFixture



class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
    def test_non_git_subdirectory_and_idle_pty_hold_the_same_edit_lease(self):
        nested = self.work / "nested"
        nested.mkdir()
        identity = uuid.uuid4().hex
        result = self.app.start_execution("local", identity, str(nested),
            [sys.executable, "-I", "-q", "-i"], tty=True, timeout_seconds=15)
        try:
            edited = self.apply()
            self.assertEqual((edited["status"], edited["error"]), ("rejected", "resource_busy"))
            record = execution.read_record(self.state / "executions" / identity)
            self.assertEqual(record["context"]["workspace_lock_root"], str(self.work))
            self.fault("legacy_workspace_busy", lambda: execution_api.check_workspace_leases(self.config, self.state, legacy_candidate=True))
        finally:
            self.app.cancel_execution("local", identity)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = self.app.read_execution("local", identity)
                if result["status"] in {"cancelled", "succeeded", "failed", "outcome_unknown"}:
                    break
                time.sleep(0.03)
            self.assertEqual(result["cleanup"]["status"], "verified")

    def test_legacy_non_git_cwd_lock_blocks_transition_without_replacing_it(self):
        nested = self.work / "nested"
        nested.mkdir()
        args = self.arguments()
        identity = uuid.uuid4().hex
        # Use a real current execution record as the legacy-layout fixture, after it exits.
        result = self.app.start_execution("local", identity, str(nested), [sys.executable, "-c", "pass"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.app.read_execution("local", identity)
            if result["status"] == "succeeded":
                break
            time.sleep(0.03)
        self.assertEqual(result["status"], "succeeded")
        directory = self.state / "executions" / identity
        path = directory / "record.json"
        record = json.loads(path.read_text())
        record["context"].pop("workspace_lock_root")
        path.write_text(json.dumps(record))
        old_key = hashlib.sha256(str(nested).encode()).hexdigest()
        old_lock = self.state / "locks" / f"workspace-{old_key}.lock"
        with lease(old_lock):
            inode = old_lock.stat().st_ino
            edit = self.apply(args)
            self.assertEqual((edit["status"], edit["error"]), ("rejected", "legacy_workspace_busy"))
            self.assertEqual(old_lock.stat().st_ino, inode)
            other = self.root / "independent"
            other.mkdir()
            (other / "source.py").write_text("answer = 41")
            self.config.workspaces["independent"] = Workspace(machine="local", root=str(other))
            read = self.app.read_workspace_file("local", "independent", "source.py")
            separate = self.apply(args, workspace_name="independent",
                expected_workspace_identity=read["context"]["workspace_identity"], expected_sha256=read["sha256"])
            self.assertEqual(separate["status"], "applied")
        self.assertEqual(self.apply(args)["status"], "applied")


if __name__ == "__main__":
    unittest.main()
