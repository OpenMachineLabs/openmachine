"""Execution lifecycle contracts using small real processes and owned fault windows."""

from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from openmachine import execution
from openmachine.config import Settings, Workspace
from openmachine.execution import api
from openmachine.execution.records import TERMINAL
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory, save_json


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-e1-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.config = Settings.model_validate({
            "schema_version": 1, "machines": {"local": {"transport": "local", "execution_enabled": True}},
            "workspaces": {"project": {"machine": "local", "root": str(self.work)}},
        })
        self.ids = []
        self.addCleanup(self.stop_owned)

    def stop_owned(self):
        for identity in self.ids:
            try:
                result = execution.read(self.config, self.state, "local", identity)
                if result["status"] not in TERMINAL:
                    execution.cancel(self.config, self.state, "local", identity)
                    self.until(identity, lambda item: item["status"] in TERMINAL)
            except Fault:
                # Corruption tests retain their intentionally unreadable disposable record.
                pass

    def start(self, code, *, execution_id=None, cwd=None, **options):
        identity = execution_id or uuid.uuid4().hex
        self.ids.append(identity)
        return execution.start(self.config, self.state, "local", identity, str(cwd or self.work),
                               [sys.executable, "-c", code], timeout_seconds=5, **options)

    def until(self, identity, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = execution.read(self.config, self.state, "local", identity)
            if predicate(result):
                return result
            time.sleep(0.02)
        self.fail(f"Original execution did not reach the expected observation: {identity}")

    def finish(self, result):
        return self.until(result["execution_id"], lambda item: item["status"] in TERMINAL)

    def output(self, identity):
        chunks, offset = [], 0
        while True:
            page = execution.read(self.config, self.state, "local", identity, offset=offset)
            chunks.append(page["output"])
            if page["next_offset"] == offset:
                return "".join(chunks)
            offset = page["next_offset"]

    def test_pipe_default_and_effective_request_identity(self):
        code = 'import sys; from pathlib import Path; p=Path("count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print(repr(sys.stdin.read())); print("stderr",file=sys.stderr)'
        result = self.finish(self.start(code))
        identity = result["execution_id"]
        self.assertEqual((result["status"], result["exit_code"], result["format_version"]), ("succeeded", 0, 4))
        self.assertEqual((result["io_mode"], result["termination_grace_seconds"], result["control"]), ("pipe", 5, None))
        self.assertIn("''", result["output"])
        self.assertIn("stderr", result["output"])
        for grace in (None, 5):
            self.assertEqual(self.start(code, execution_id=identity, termination_grace_seconds=grace)["created_at"], result["created_at"])
        with self.assertRaises(Fault) as caught:
            self.start(code, execution_id=identity, termination_grace_seconds=1)
        self.assertEqual(caught.exception.code, "execution_conflict")
        self.assertEqual((self.work / "count").read_text(), "1")

    def test_grace_rejects_nonfinite_boolean_and_out_of_range_before_effect(self):
        for grace in (True, False, float("nan"), float("inf"), -0.1, 30.1, "5"):
            with self.subTest(grace=grace), self.assertRaises(Fault) as caught:
                self.start('from pathlib import Path; Path("effect").touch()', termination_grace_seconds=grace)
            self.assertEqual(caught.exception.code, "invalid_grace")
        self.assertFalse((self.work / "effect").exists())

    def test_exec_failure_exit_127_signal_and_zero_output_are_distinct(self):
        result = execution.start(self.config, self.state, "local", uuid.uuid4().hex, str(self.work), [str(self.work / "absent")])
        result = self.finish(result)
        self.assertEqual((result["status"], result["exit_code"], result["command"]["spawned"]), ("failed", None, False))
        self.assertFalse(result["command"]["exited"])
        for code in (0, 7, 127):
            result = self.finish(self.start(f"raise SystemExit({code})"))
            self.assertEqual(result["exit_code"], code)
            self.assertTrue(result["command"]["spawned"] and result["command"]["exited"])
            self.assertEqual(result["status"], "failed" if code else "succeeded")
        result = self.finish(self.start("import os,signal; os.kill(os.getpid(),signal.SIGTERM)"))
        self.assertEqual((result["exit_code"], result["command"]["exit_signal"]), (-signal.SIGTERM, signal.SIGTERM))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cleanup"]["status"], "verified")

    def test_term_handler_drains_more_than_a_pipe_and_saves_before_returning_zero(self):
        code = '''import signal,sys,time
from pathlib import Path
def stop(*_):
    time.sleep(0.7)
    sys.stdout.write("x" * (512 * 1024) + "\\nfinal-diagnostic\\n")
    sys.stdout.flush()
    Path("saved").write_text("complete")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
print("ready", flush=True)
while True: time.sleep(0.05)
'''
        started = self.start(code)
        identity = started["execution_id"]
        self.until(identity, lambda item: "ready" in item["output"])
        execution.cancel(self.config, self.state, "local", identity)
        result = self.finish(started)
        self.assertEqual((result["status"], result["exit_code"]), ("cancelled", 0))
        self.assertEqual(result["stop"]["reason"], "cancel")
        self.assertFalse(result["stop"]["force_used"])
        self.assertEqual((self.work / "saved").read_text(), "complete")
        self.assertIn("final-diagnostic", self.output(identity))
        self.assertFalse(result["output_incomplete"])

    def test_forced_cleanup_covers_other_owned_groups_but_not_a_sentinel(self):
        other = self.root / "other"
        other.mkdir()
        self.config.workspaces["other"] = Workspace(machine="local", root=str(other))
        child = 'import os,signal,time; from pathlib import Path; os.setpgid(0,0); signal.signal(signal.SIGTERM,signal.SIG_IGN); print("child-ready",flush=True); time.sleep(1); Path("escaped").touch(); time.sleep(2)'
        code = 'import signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(4)'
        owned = self.start(code, termination_grace_seconds=0.1)
        sentinel = self.start('import time; from pathlib import Path; time.sleep(0.4); Path("sentinel").touch()', cwd=other)
        self.until(owned["execution_id"], lambda item: "child-ready" in item["output"])
        execution.cancel(self.config, self.state, "local", owned["execution_id"])
        result = self.finish(owned)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["stop"]["force_used"])
        self.assertEqual(result["cleanup"]["status"], "verified")
        self.assertEqual(self.finish(sentinel)["status"], "succeeded")
        time.sleep(1)
        self.assertFalse((self.work / "escaped").exists())
        self.assertTrue((other / "sentinel").exists())

    def test_natural_leader_exit_waits_for_children_and_late_cancel_does_not_relabel(self):
        child = 'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path("ready").touch(); time.sleep(3)'
        code = 'import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']);\nwhile not Path("ready").exists(): time.sleep(.01)'
        started = self.start(code, termination_grace_seconds=0.5)
        identity = started["execution_id"]
        pending = self.until(identity, lambda item: item["status"] == "finishing")
        self.assertTrue(pending["command"]["exited"])
        self.assertEqual(pending["cleanup"]["status"], "pending")
        execution.cancel(self.config, self.state, "local", identity)
        result = self.finish(started)
        self.assertEqual((result["status"], result["exit_code"], result["stop"]["reason"]), ("succeeded", 0, None))
        self.assertTrue(result["stop"]["force_used"])

    def test_repeated_cancel_preserves_first_reason_and_terminal_bytes(self):
        result = self.start('import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(3)', termination_grace_seconds=0.3)
        identity = result["execution_id"]
        self.until(identity, lambda item: "ready" in item["output"])
        execution.cancel(self.config, self.state, "local", identity)
        observed = self.until(identity, lambda item: item["stop"]["reason"] == "cancel")
        execution.cancel(self.config, self.state, "local", identity)
        final = self.finish(result)
        self.assertEqual(final["stop"]["requested_at"], observed["stop"]["requested_at"])
        path = self.state / "executions" / identity / "record.json"
        before = path.read_bytes()
        self.assertEqual(execution.cancel(self.config, self.state, "local", identity)["status"], "cancelled")
        self.assertEqual(path.read_bytes(), before)

    def injected_worker(self, setup):
        original = subprocess.Popen
        script = 'import sys; from pathlib import Path; import openmachine.execution.worker as w\n' + setup + '\nw.worker(Path(sys.argv[1]),*(int(x) for x in sys.argv[2:]))'
        def launch(argv, *args, **kwargs):
            if len(argv) > 3 and argv[1:4] == ["-I", "-m", "openmachine.execution"]:
                argv = [argv[0], "-I", "-c", script, *argv[4:]]
            return original(argv, *args, **kwargs)
        return patch.object(api.subprocess, "Popen", side_effect=launch)

    def test_log_and_receipt_failures_do_not_become_success_or_replay(self):
        cases = (
            'def fail(*_): raise OSError("injected log failure")\nw.logs.Writer.write=fail',
            'original=w.save_json\ncount=0\ndef fail(path,value):\n global count\n count+=1\n if count==2: raise OSError("injected receipt failure")\n original(path,value)\nw.save_json=fail',
        )
        for setup in cases:
            with self.subTest(setup=setup), self.injected_worker(setup):
                started = self.start('import time; print("effect",flush=True); time.sleep(.3)')
            result = self.finish(started)
            self.assertEqual(result["status"], "failed")
            self.assertIsNotNone(result["error"])
            self.assertTrue(result["command"]["spawned"])
            self.assertEqual(result["cleanup"]["status"], "verified")
            if result["error"] == "output_io_failed":
                self.assertTrue(result["output_incomplete"])

    def test_prelaunch_cancel_never_launches_the_command(self):
        setup = '(Path(sys.argv[1])/"cancel.request").write_text("cancel\\n")'
        with self.injected_worker(setup):
            result = self.finish(self.start('from pathlib import Path; Path("effect").touch()'))
        self.assertEqual((result["status"], result["exit_code"]), ("cancelled", None))
        self.assertFalse(result["command"]["spawned"])
        self.assertEqual(result["cleanup"]["status"], "verified")
        self.assertFalse((self.work / "effect").exists())

    def test_legacy_retry_preserves_grace_and_never_rewrites_the_receipt(self):
        private_directory(self.state)
        private_directory(self.state / "executions")
        directory = private_directory(self.state / "executions" / uuid.uuid4().hex)
        code = 'print("legacy")'
        record = {"version": "0.1.0", "status": "succeeded", "exit_code": 0, "created_at": 1,
                  "finished_at": 2, "log_truncated": False, "output_incomplete": False,
                  "context": {"worktree": str(self.work)},
                  "request": {"machine": "local", "cwd": str(self.work), "argv": [sys.executable, "-c", code],
                              "expected_head_sha": "", "timeout_seconds": 5}}
        save_json(directory / "record.json", record)
        (directory / "output.log").write_text("legacy\n")
        before = (directory / "record.json").read_bytes()
        for grace in (None, 0.5):
            result = self.start(code, execution_id=directory.name, termination_grace_seconds=grace)
            self.assertEqual(result["termination_grace_seconds"], 0.5)
            self.assertIsNone(result["command"])
            self.assertIsNone(result["cleanup"])
        self.assertEqual((directory / "record.json").read_bytes(), before)

    def test_deleted_cwd_still_allows_original_receipt_reads(self):
        result = self.finish(self.start("pass"))
        self.work.rmdir()
        self.assertEqual(execution.read(self.config, self.state, "local", result["execution_id"])["status"], "succeeded")
        with self.assertRaises(Fault) as caught:
            self.start("pass", execution_id=result["execution_id"])
        self.assertEqual(caught.exception.code, "context_unavailable")

    def test_unavailable_membership_never_infers_success(self):
        setup = 'def fail(*_): raise w.Fault("cleanup_unverified", "injected membership failure")\nw.Process.members=fail'
        with self.injected_worker(setup):
            result = self.finish(self.start('print("known output",flush=True)'))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["cleanup"]["status"], "unverified")
        self.assertTrue(result["command"]["exited"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("known output", result["output"])
        with self.assertRaises(Fault) as caught:
            execution.cancel(self.config, self.state, "local", result["execution_id"])
        self.assertEqual(caught.exception.code, "outcome_unknown")

    def test_command_budget_starts_at_launch_not_receipt_publication(self):
        setup = ('original=w._publish\n'
                 'def delayed(path,record):\n'
                 ' if record["status"]=="running": w.time.sleep(1.5)\n'
                 ' return original(path,record)\n'
                 'w._publish=delayed')
        with self.injected_worker(setup):
            result = execution.start(self.config, self.state, "local", uuid.uuid4().hex, str(self.work),
                                     [sys.executable, "-c", "import time; time.sleep(2)"], timeout_seconds=1)
        self.ids.append(result["execution_id"])
        final = self.finish(result)
        self.assertEqual(final["status"], "timed_out")
        self.assertEqual(final["stop"]["reason"], "timeout")
        self.assertEqual(final["cleanup"]["status"], "verified")

    def test_timeout_preserves_reason_when_term_handler_returns_zero(self):
        code = 'import signal,time; signal.signal(signal.SIGTERM,lambda *_:exit(0)); print("ready",flush=True); time.sleep(4)'
        result = execution.start(self.config, self.state, "local", uuid.uuid4().hex, str(self.work),
                                 [sys.executable, "-c", code], timeout_seconds=1)
        self.ids.append(result["execution_id"])
        final = self.finish(result)
        self.assertEqual((final["status"], final["exit_code"], final["stop"]["reason"]), ("timed_out", 0, "timeout"))
        self.assertFalse(final["stop"]["force_used"])


if __name__ == "__main__":
    unittest.main()
