"""Tests for ssh behavior at the resilience layer."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
import zipfile
from integrations.ssh import bundle
from openmachine import __version__, execution
from openmachine.execution import logs
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from test.support.ssh_cases import SSHCaseFixture


class SSHTests(SSHCaseFixture, unittest.TestCase):
    def test_bundle_is_reproducible_content_addressed_and_dependency_free(self):
        first = bundle.build_bundle(self.root / "bundle")
        self.assertEqual(first, bundle.build_bundle(self.root / "bundle"))
        artifact = Path(first["path"])
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), first["sha256"])
        with zipfile.ZipFile(artifact) as archive:
            self.assertEqual(set(archive.namelist()), {"__main__.py", "integrations/__init__.py", "integrations/ssh/__init__.py",
                              "integrations/ssh/endpoint.py", "integrations/ssh/bundle.py",
                              "openmachine/__init__.py", "openmachine/execution/__init__.py",
                              "openmachine/execution/__main__.py", "openmachine/execution/api.py",
                              "openmachine/execution/worker.py", "openmachine/execution/records.py", "openmachine/execution/process.py",
                              "openmachine/execution/logs.py", "openmachine/execution/control.py", "openmachine/workspace/__init__.py",
                              "openmachine/workspace/inspect.py", "openmachine/workspace/git.py",
                              "openmachine/workspace/files.py", "openmachine/workspace/change_records.py",
                              "openmachine/workspace/preparation.py", "openmachine/workspace/changes.py", "openmachine/workspace/patch.py", "openmachine/workspace/worktrees.py", "openmachine/foundation/__init__.py",
                              "openmachine/foundation/state.py", "openmachine/foundation/files.py",
                              "openmachine/foundation/sanitize.py"})
        # -S removes third-party site packages; the remote entry must still import.
        result = subprocess.run([sys.executable, "-I", "-S", str(artifact), "rpc"], input="{}",
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "protocol_mismatch")
        artifact.write_bytes(b"changed")
        with self.assertRaises(Fault) as caught:
            bundle.build_bundle(artifact.parent)
        self.assertEqual(caught.exception.code, "worker_conflict")

    def test_archive_worker_runs_without_site_packages_or_controller_task_store(self):
        artifact = Path(bundle.build_bundle(self.root / "workers")["path"])
        state = private_directory(self.root / "state")
        directory = private_directory(state / "executions" / ("c" * 32))
        locks = private_directory(state / "locks")
        record = {"format_version": execution.FORMAT, "version": __version__, "status": "accepted", "exit_code": None,
                  "created_at": 1, "log_truncated": False, "output_incomplete": False,
                  "context": {"worktree": str(self.project)},
                  "request": {"machine": self.identity, "cwd": str(self.project), "expected_head_sha": "",
                              "argv": [sys.executable, "-I", "-S", "-c",
                                       'from pathlib import Path; p=Path("count.txt"); '
                                       'p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print("worker-complete")'],
                              "timeout_seconds": 10, "task_id": "controller-only-task", "termination_grace_seconds": 5,
                              "tty": False, "terminal_size": None},
                  "io_mode": "pipe", "terminal_size": None, "control": None, "control_ready": False,
                  "command": {"spawned": False, "exited": False, "started_at": None, "exited_at": None, "exit_signal": None},
                  "stop": {"reason": None, "requested_at": None, "grace_seconds": 5, "force_used": False},
                  "cleanup": {"containment": "posix_session", "status": "pending", "error": None}}
        with lease(directory / "control.lock"):
            save_json(directory / "record.json", record)
        key = hashlib.sha256(str(self.project).encode()).hexdigest()
        with lease(locks / f"workspace-{key}.lock") as owned, lease(directory / "worker.lock") as worker:
            result = subprocess.run([sys.executable, "-I", "-S", str(artifact), "worker", str(directory),
                                     str(worker.fileno()), str(owned.fileno())], cwd=self.root,
                                    pass_fds=(worker.fileno(), owned.fileno()),
                                    capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        observed = execution.read_record(directory)
        self.assertEqual((observed["status"], observed["exit_code"]), ("succeeded", 0))
        self.assertEqual(observed["created_at"], 1)
        self.assertEqual(observed["request"]["task_id"], "controller-only-task")
        self.assertEqual((self.project / "count.txt").read_text(), "1")
        self.assertIn("worker-complete", logs.read(directory, 0, 65536)["output"])
        self.assertFalse((state / "tasks").exists())


if __name__ == "__main__":
    unittest.main()
