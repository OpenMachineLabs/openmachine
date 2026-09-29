"""Local contracts with disposable directories and real short commands."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from openmachine import execution, workspace
from openmachine.config import Settings, Workspace, load
from openmachine.sanitize import Fault, redact


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.config = Settings.model_validate({
            "schema_version": 1, "machines": {"my-laptop": {"transport": "local", "execution_enabled": True}},
            "workspaces": {"project": {"machine": "my-laptop", "root": str(self.work)}},
        })

    def start(self, code, *, execution_id=None, cwd=None, **kwargs):
        return execution.start(self.config, self.state, "my-laptop", execution_id or uuid.uuid4().hex,
                               str(cwd or self.work), [sys.executable, "-c", code], **kwargs)

    def wait(self, result):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = execution.read(self.config, self.state, "my-laptop", result["execution_id"])
            if result["status"] in execution.TERMINAL | {"outcome_unknown"}:
                return result
            time.sleep(0.03)
        self.fail(f"Execution did not finish: {result['execution_id']}")

    def test_configuration_rejects_unknown_or_invalid_fields(self):
        path = self.root / "config.toml"
        for text in (
            'schema_version = 1\n[machines.any]\ntransport = "ssh"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\nextra = true\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\n[workspaces.x]\nmachine = "missing"\nroot = "/tmp"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\n[workspaces.x]\nmachine = "any"\nroot = "relative"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\nexecution_enabled = "true"\n',
        ):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(Fault) as caught:
                    load(path)
                self.assertEqual(caught.exception.code, "invalid_config")

    def test_init_is_disabled_private_and_never_overwrites(self):
        path = self.root / "config.toml"
        command = [sys.executable, "-I", "-m", "openmachine.cli", "--config", str(path),
                   "init", "--workspace", str(self.work)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse(load(path).machines["local"].execution_enabled)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        content = path.read_bytes()
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(path.read_bytes(), content)

    def test_doctor_does_not_create_state_or_claim_live_health(self):
        path = self.root / "config.toml"
        path.write_text('schema_version = 1\n[machines.local]\ntransport = "local"\n')
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(path),
                                 "--state-dir", str(self.state), "doctor"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual(json.loads(result.stdout)["chatgpt"], "not_probed")

    def test_disabled_machine_cannot_execute(self):
        self.config.machines["my-laptop"].execution_enabled = False
        with self.assertRaises(Fault) as caught:
            self.start('print("no")')
        self.assertEqual(caught.exception.code, "execution_disabled")
        self.assertFalse(self.state.exists())

    def test_dotfiles_are_read_but_credentials_and_escaping_symlinks_are_not(self):
        (self.work / ".github").mkdir()
        (self.work / ".github/AGENTS.md").write_text("Read this instruction.\n")
        result = workspace.inspect(self.config, "my-laptop", "project", "read", ".github/AGENTS.md")
        self.assertEqual(result["text"], "Read this instruction.\n")
        (self.work / ".env").write_text("private fixture\n")
        (self.root / "outside").write_text("outside fixture\n")
        (self.work / "escape").symlink_to(self.root / "outside")
        (self.work / "hidden-secret").symlink_to(self.work / ".env")
        for path in (".env", "../outside", "escape", "hidden-secret"):
            with self.subTest(path=path), self.assertRaises(Fault):
                workspace.inspect(self.config, "my-laptop", "project", "read", path)
        names = [item["name"] for item in workspace.inspect(self.config, "my-laptop", "project", "files")["entries"]]
        self.assertNotIn(".env", names)

    def test_binary_and_fifo_reads_do_not_block(self):
        (self.work / "binary").write_bytes(b"a\0b")
        os.mkfifo(self.work / "fifo")
        for path in ("binary", "fifo"):
            with self.subTest(path=path), self.assertRaises(Fault):
                workspace.inspect(self.config, "my-laptop", "project", "read", path)

    def test_file_byte_pagination_and_listing(self):
        content = "x" * (workspace.MAX_READ * 3)
        (self.work / "large.txt").write_text(content)
        parts, offset = [], 0
        while True:
            page = workspace.inspect(self.config, "my-laptop", "project", "read", "large.txt", offset)
            parts.append(page["text"])
            offset = page["next_offset"]
            if not page["has_more"]:
                break
        self.assertEqual("".join(parts), content)
        self.assertEqual(len(parts), 3)
        for name in ("a", "b", "c"):
            (self.work / name).touch()
        page = workspace.inspect(self.config, "my-laptop", "project", "files", limit=2)
        self.assertEqual((len(page["entries"]), page["next_offset"]), (2, 2))

    def test_redaction_is_applied(self):
        self.assertNotIn("synthetic-value", redact("password=synthetic-value"))

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

    def test_failed_command_and_missing_executable_are_not_success(self):
        result = self.wait(self.start('import sys; print("failed"); sys.exit(7)'))
        self.assertEqual((result["status"], result["exit_code"]), ("failed", 7))
        result = execution.start(self.config, self.state, "my-laptop", uuid.uuid4().hex,
                                 str(self.work), [str(self.root / "missing-executable")])
        result = self.wait(result)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["exit_code"])

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

    def test_expected_head_mismatch_prevents_launch(self):
        with self.assertRaises(Fault) as caught:
            self.start('from pathlib import Path; Path("should-not-exist").touch()', expected_head_sha="0" * 40)
        self.assertEqual(caught.exception.code, "context_mismatch")
        self.assertFalse((self.work / "should-not-exist").exists())

    def test_log_limit_does_not_kill_command_and_pages_remain_bounded(self):
        result = self.wait(self.start('import sys; sys.stdout.write("x" * (4*1024*1024+100))'))
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["log_truncated"])
        self.assertEqual(result["retained_bytes"], execution.MAX_LOG)
        self.assertEqual(len(result["output"]), execution.MAX_READ)
        page = execution.read(self.config, self.state, "my-laptop", result["execution_id"], offset=10, max_bytes=7)
        self.assertEqual((page["output"], page["next_offset"]), ("xxxxxxx", 17))

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

    def test_unsafe_state_directory_is_rejected_without_chmod(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaises(Fault) as caught:
            self.start('print("no")')
        self.assertEqual(caught.exception.code, "unsafe_state")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
