"""Tests for execution behavior at the resilience layer."""

import json
import subprocess
import sys
import time
import unittest
import uuid
from openmachine import execution
from openmachine.execution import logs
from openmachine.config import Workspace
from openmachine.foundation.sanitize import Fault
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_native_write_readback_and_identical_id_deduplication(self):
        code = 'from pathlib import Path; p=Path("counter"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print("written")'
        first = self.wait(self.start(code))
        self.assertEqual((first["status"], first["exit_code"]), ("succeeded", 0))
        again = self.start(code, execution_id=first["execution_id"])
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.assertIn("written", first["output"])
        with self.assertRaises(Fault) as caught:
            self.start('print("different")', execution_id=first["execution_id"])
        self.assertEqual(caught.exception.code, "execution_conflict")

    def test_empty_positional_argument_is_preserved(self):
        result = execution.start(self.config, self.state, "my-laptop", uuid.uuid4().hex,
                                 str(self.work), [sys.executable, "-c", "import sys; print(repr(sys.argv[1]))", ""])
        self.assertEqual(self.wait(result)["output"], "''\n")

    def test_timeout_has_a_terminal_receipt(self):
        result = self.wait(self.start("import time; time.sleep(20)", timeout_seconds=1))
        self.assertEqual(result["status"], "timed_out")
        self.assertNotEqual(result["exit_code"], 0)

    def test_same_worktree_is_exclusive_but_other_workspaces_can_run(self):
        first = self.start("import time; time.sleep(1)")
        try:
            with self.assertRaises(Fault) as caught:
                self.start('print("conflict")')
            self.assertEqual(caught.exception.code, "resource_busy")
            other = self.root / "other"
            other.mkdir()
            self.config.workspaces["other"] = Workspace(machine="my-laptop", root=str(other))
            self.assertEqual(self.wait(self.start('print("independent")', cwd=other))["status"], "succeeded")
        finally:
            self.assertEqual(self.wait(first)["status"], "succeeded")

    def test_git_repositories_under_one_parent_can_run_independently(self):
        first_repo, second_repo = self.work / "first", self.work / "second"
        for repo in (first_repo, second_repo):
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        first = self.start("from pathlib import Path\nimport time\n"
                           "while not Path('release').exists(): time.sleep(0.01)",
                           cwd=first_repo, timeout_seconds=5)
        try:
            with self.assertRaises(Fault) as caught:
                self.start('print("conflict")', cwd=first_repo)
            self.assertEqual(caught.exception.code, "resource_busy")
            self.assertEqual(caught.exception.evidence["workspace_lock_root"], str(first_repo))
            second = self.wait(self.start('print("independent")', cwd=second_repo))
            self.assertEqual(second["status"], "succeeded")
        finally:
            (first_repo / "release").touch()
            self.assertEqual(self.wait(first)["status"], "succeeded")

    def test_log_limit_does_not_kill_command_and_pages_remain_bounded(self):
        result = self.wait(self.start('import sys; sys.stdout.write("x" * (4*1024*1024+100))'))
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["log_truncated"])
        self.assertLessEqual(result["retained_bytes"], execution.MAX_LOG)
        self.assertEqual(result["log_start_offset"], logs.SEGMENT_BYTES)
        self.assertTrue(result["output_gap"])
        self.assertEqual(len(result["output"]), execution.MAX_READ)
        page = execution.read(self.config, self.state, "my-laptop", result["execution_id"], offset=10, max_bytes=7)
        self.assertEqual((page["output"], page["next_offset"]), ("xxxxxxx", logs.SEGMENT_BYTES + 7))

    def test_missing_worker_and_corrupt_receipt_never_become_success(self):
        result = self.wait(self.start('print("original")'))
        path = self.state / "executions" / result["execution_id"] / "record.json"
        record = json.loads(path.read_text())
        record.update(status="running", exit_code=None)
        path.write_text(json.dumps(record))
        unknown = execution.read(self.config, self.state, "my-laptop", result["execution_id"])
        self.assertEqual(unknown["status"], "outcome_unknown")
        path.write_text("broken json")
        with self.assertRaises(Fault) as caught:
            execution.read(self.config, self.state, "my-laptop", result["execution_id"])
        self.assertEqual(caught.exception.code, "corrupt_state")

    def test_missing_unrelated_workspace_does_not_disable_valid_workspace(self):
        valid = self.config.workspaces.pop("project")
        self.config.workspaces["missing"] = Workspace(machine="my-laptop", root=str(self.root / "absent"))
        self.config.workspaces["project"] = valid
        self.assertEqual(self.wait(self.start('print("available")'))["status"], "succeeded")

    def test_incomplete_terminal_receipt_is_corrupt(self):
        result = self.wait(self.start('print("original")'))
        path = self.state / "executions" / result["execution_id"] / "record.json"
        original = json.loads(path.read_text())
        for key in ("version", "log_truncated", "exit_code", "finished_at"):
            with self.subTest(key=key):
                partial = dict(original)
                del partial[key]
                path.write_text(json.dumps(partial))
                with self.assertRaises(Fault) as caught:
                    execution.read(self.config, self.state, "my-laptop", result["execution_id"])
                self.assertEqual(caught.exception.code, "corrupt_state")

    def test_worktree_alias_does_not_bypass_lock_and_dirty_files_survive(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.work), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial")
        head = git("rev-parse", "HEAD")
        (self.work / "unrelated.txt").write_text("preserve me")
        sub = self.work / "sub"
        sub.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.work, target_is_directory=True)
        self.config.workspaces["alias"] = Workspace(machine="my-laptop", root=str(alias))
        first = self.start("import time; time.sleep(1)", expected_head_sha=head)
        try:
            for cwd in (sub, alias):
                with self.subTest(cwd=cwd), self.assertRaises(Fault) as caught:
                    self.start('print("conflict")', cwd=cwd)
                self.assertEqual(caught.exception.code, "resource_busy")
        finally:
            self.assertEqual(self.wait(first)["status"], "succeeded")
        self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve me")
        self.assertEqual(git("rev-parse", "HEAD"), head)

    def test_timeout_default_and_explicit_day_limit_are_separate(self):
        first = self.wait(self.start('print("default")'))
        record = json.loads((self.state / "executions" / first["execution_id"] / "record.json").read_text())
        self.assertEqual(record["request"]["timeout_seconds"], 600)
        self.assertEqual(self.wait(self.start('print("long limit, short command")', timeout_seconds=86400))["status"], "succeeded")
        for value in (0, 86401):
            with self.subTest(value=value), self.assertRaises(Fault) as caught:
                self.start('print("not started")', timeout_seconds=value)
            self.assertEqual(caught.exception.code, "invalid_timeout")

    def test_cancellation_cleans_owned_children_and_preserves_other_work(self):
        other = self.root / "other"
        other.mkdir()
        self.config.workspaces["other"] = Workspace(machine="my-laptop", root=str(other))
        child = 'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("child-ready",flush=True); time.sleep(1.5); Path("escaped").touch(); time.sleep(10)'
        command = 'import signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(10)'
        owned = self.start(command, termination_grace_seconds=0.1)
        independent = self.start('import time; from pathlib import Path; time.sleep(0.7); Path("done").touch()', cwd=other)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "child-ready" in execution.read(self.config, self.state, "my-laptop", owned["execution_id"])["output"]:
                break
            time.sleep(0.03)
        requested = execution.cancel(self.config, self.state, "my-laptop", owned["execution_id"])
        self.assertTrue(requested["cancel_requested"])
        execution.cancel(self.config, self.state, "my-laptop", owned["execution_id"])
        result = self.wait(owned)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(self.wait(independent)["status"], "succeeded")
        time.sleep(1.5)
        self.assertFalse((self.work / "escaped").exists())
        self.assertTrue((other / "done").exists())

    def test_completed_execution_cancellation_is_a_noop(self):
        result = self.wait(self.start('from pathlib import Path; Path("once").write_text("once")'))
        path = self.state / "executions" / result["execution_id"] / "record.json"
        original = path.read_bytes()
        cancelled = execution.cancel(self.config, self.state, "my-laptop", result["execution_id"])
        self.assertEqual(cancelled["status"], "succeeded")
        self.assertFalse(cancelled["cancel_requested"])
        self.assertEqual(path.read_bytes(), original)

    def test_shared_workspaces_and_exclusive_named_resources(self):
        first = self.start('import time; time.sleep(0.7)', workspace_access="shared", resource_keys=["device:0"])
        try:
            self.assertEqual(self.wait(self.start('print("shared")', workspace_access="shared"))["status"], "succeeded")
            with self.assertRaises(Fault) as caught:
                self.start('print("writer")')
            self.assertEqual(caught.exception.code, "resource_busy")
            other = self.root / "other"
            other.mkdir()
            self.config.workspaces["other"] = Workspace(machine="my-laptop", root=str(other))
            with self.assertRaises(Fault) as caught:
                self.start('print("resource conflict")', cwd=other, resource_keys=["device:0"])
            self.assertEqual(caught.exception.code, "resource_busy")
        finally:
            self.wait(first)
        result = self.wait(self.start('print("normalized")', resource_keys=["b", "a"]))
        again = self.start('print("normalized")', execution_id=result["execution_id"], resource_keys=["a", "b", "a"])
        self.assertEqual(again["status"], "succeeded")

    def test_execution_listing_pages_and_reports_corruption_without_commands(self):
        results = [self.wait(self.start('print("not in listing")', execution_id=f"{i:032x}")) for i in (1, 2, 3)]
        damaged = self.state / "executions" / f"{2:032x}" / "record.json"
        damaged.write_text("broken")
        first = execution.list_records(self.config, self.state, "my-laptop", limit=2)
        self.assertEqual(first["next_cursor"], f"{2:032x}")
        self.assertEqual(len(first["unreadable"]), 1)
        second = execution.list_records(self.config, self.state, "my-laptop", cursor=first["next_cursor"], limit=2)
        self.assertEqual(second["executions"][0]["execution_id"], results[2]["execution_id"])
        self.assertIsNone(second["next_cursor"])
        self.assertNotIn("not in listing", json.dumps(first))

    def test_legacy_receipts_and_prefix_logs_remain_readable_without_rewriting(self):
        result = self.wait(self.start('print("legacy")'))
        directory = self.state / "executions" / result["execution_id"]
        record = json.loads((directory / "record.json").read_text())
        del record["format_version"]
        record["version"] = "0.1.0a1"
        del record["request"]["workspace_access"]
        del record["request"]["resource_keys"]
        (directory / "record.json").write_text(json.dumps(record))
        (directory / "output-00000000000000000000.log").rename(directory / "output.log")
        (directory / "control.lock").unlink()
        original = (directory / "record.json").read_bytes()
        observed = execution.read(self.config, self.state, "my-laptop", result["execution_id"])
        self.assertEqual((observed["output"], observed["log_retention"]), ("legacy\n", "prefix_only"))
        self.assertEqual(self.start('print("legacy")', execution_id=result["execution_id"])["status"], "succeeded")
        self.assertEqual((directory / "record.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
