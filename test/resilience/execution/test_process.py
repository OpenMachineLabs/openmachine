"""Tests for execution behavior at the resilience layer."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import threading
import signal
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from openmachine import execution, service
from openmachine.execution import api, process
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from openmachine.service import checks as service_checks
from test.support.execution_cases import ExecutionCaseFixture


class ExecutionTests(ExecutionCaseFixture, unittest.TestCase):
    def test_pipe_default_and_effective_request_identity(self):
        code = 'import sys; from pathlib import Path; p=Path("count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print(repr(sys.stdin.read())); print("stderr",file=sys.stderr)'
        result = self.finish(self.start(code))
        identity = result["execution_id"]
        self.assertEqual((result["status"], result["exit_code"], result["format_version"]), ("succeeded", 0, execution.FORMAT))
        self.assertEqual((result["io_mode"], result["termination_grace_seconds"], result["control"]), ("pipe", 5, {"protocol": 1, "seq": 0, "last": None}))
        self.assertFalse(result["control_ready"])
        self.assertIn("''", result["output"])
        self.assertIn("stderr", result["output"])
        for grace in (None, 5):
            self.assertEqual(self.start(code, execution_id=identity, termination_grace_seconds=grace)["created_at"], result["created_at"])
        with self.assertRaises(Fault) as caught:
            self.start(code, execution_id=identity, termination_grace_seconds=1)
        self.assertEqual(caught.exception.code, "execution_conflict")
        self.assertEqual((self.work / "count").read_text(), "1")

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

    def test_deleted_cwd_still_allows_original_receipt_reads(self):
        result = self.finish(self.start("pass"))
        self.work.rmdir()
        self.assertEqual(execution.read(self.config, self.state, "local", result["execution_id"])["status"], "succeeded")
        with self.assertRaises(Fault) as caught:
            self.start("pass", execution_id=result["execution_id"])
        self.assertEqual(caught.exception.code, "context_unavailable")

    def test_unavailable_membership_never_infers_success(self):
        setup = 'w.FORCE_SECONDS=.05\ndef fail(*_): raise w.Fault("cleanup_unverified", "injected membership failure")\nw.Process.members=fail'
        with self.injected_worker(setup):
            result = self.finish(self.start('print("known output",flush=True)', termination_grace_seconds=0))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["cleanup"]["status"], "unverified")
        self.assertTrue(result["command"]["exited"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("known output", result["output"])
        with self.assertRaises(Fault) as caught:
            execution.cancel(self.config, self.state, "local", result["execution_id"])
        self.assertEqual(caught.exception.code, "outcome_unknown")

    def test_transient_cleanup_query_failure_keeps_original_owner_until_verified(self):
        setup = ('original=w.Process.members\ncount=0\n'
                 'def flaky(self,deadline):\n global count\n count+=1\n'
                 ' if count==1: raise w.Fault("cleanup_query_timeout", "injected transient query timeout")\n'
                 ' return original(self,deadline)\nw.Process.members=flaky')
        with self.injected_worker(setup):
            result = self.finish(self.start('from pathlib import Path; Path("once").write_text("1")'))
        self.assertEqual((result["status"], result["cleanup"]["status"]), ("succeeded", "verified"))
        self.assertIsNone(result["cleanup"]["error"])
        self.assertEqual((self.work / "once").read_text(), "1")

    def test_native_query_failures_retain_specific_diagnostics_and_bounded_cleanup(self):
        launch = subprocess.Popen
        for code, expected in (("import time; time.sleep(5)", "cleanup_query_timeout"),
                               ("raise SystemExit(2)", "cleanup_query_failed"),
                               ("pass", "cleanup_query_failed"),
                               ('print("invalid")', "cleanup_query_invalid")):
            def query(_argv, **kwargs):
                return launch([sys.executable, "-c", code], **kwargs)
            started = time.monotonic()
            with self.subTest(error=expected), patch.object(process.subprocess, "Popen", side_effect=query):
                with self.assertRaises(Fault) as caught:
                    process._mac_states(started + .2)
                self.assertEqual(caught.exception.code, expected)
                self.assertLess(time.monotonic() - started, 2)

    def test_native_session_scan_never_infers_empty_from_a_disappearing_candidate(self):
        for error in (ProcessLookupError(), FileNotFoundError()):
            with self.subTest(error=type(error)), patch.object(process.sys, "platform", "darwin"), \
                    patch.object(process, "_mac_states", return_value=[(42, "S")]), \
                    patch.object(process.os, "getsid", side_effect=error), self.assertRaises(Fault) as caught:
                process.session_members(42, time.monotonic() + 1, leader_exited=True)
            self.assertEqual(caught.exception.code, "cleanup_query_raced")
        for pid, known_exit, expected_race in ((42, True, False), (42, False, True), (43, True, True)):
            with self.subTest(pid=pid, known_exit=known_exit), patch.object(process.sys, "platform", "linux"), \
                    patch.object(process.os, "scandir") as entries, \
                    patch.object(process.Path, "read_text", return_value=f"{pid} (fixture) Z"), \
                    patch.object(process.os, "getsid", return_value=42):
                entries.return_value.__enter__.return_value = [SimpleNamespace(name=str(pid))]
                if expected_race:
                    with self.assertRaises(Fault) as caught:
                        process.session_members(42, time.monotonic() + 1, leader_exited=known_exit)
                    self.assertEqual(caught.exception.code, "cleanup_query_raced")
                else:
                    self.assertEqual(process.session_members(42, time.monotonic() + 1, leader_exited=known_exit), {})
        with patch.object(process.sys, "platform", "darwin"), \
                patch.object(process, "_mac_states", return_value=[(43, "Z")]), \
                patch.object(process.os, "getsid", side_effect=AssertionError("Snapshot already proved exit")):
            self.assertEqual(process.session_members(42, time.monotonic() + 1, leader_exited=True), {})

    def test_original_cleanup_reconciliation_survives_lost_reply_and_fresh_reader(self):
        setup = 'w.FORCE_SECONDS=.05\ndef fail(*_): raise w.Fault("cleanup_query_timeout", "injected failure")\nw.Process.members=fail'
        code = 'from pathlib import Path; p=Path("once"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'
        with self.injected_worker(setup):
            result = self.finish(self.start(code, termination_grace_seconds=0))
        identity = result["execution_id"]
        self.assertEqual((result["status"], result["cleanup"]["error"]), ("outcome_unknown", "cleanup_query_timeout"))
        path = self.state / "executions" / identity / "record.json"
        original = path.read_bytes()
        publish = api.save_json

        def lose_reply(path, value):
            publish(path, value)
            raise OSError("injected lost publication acknowledgement")

        with patch.object(api, "save_json", side_effect=lose_reply), self.assertRaises(OSError):
            execution.reconcile(self.config, self.state, "local", identity)
        evidence = (path.parent / "reconciliation.json").read_bytes()
        with patch.object(api, "session_members", side_effect=AssertionError("Never repeat verified recovery")):
            recovered = execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual((recovered["status"], recovered["cleanup"]["source"]), ("succeeded", "native_reconciliation"))
        self.assertEqual((path.parent / "reconciliation.json").read_bytes(), evidence)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.start(code, execution_id=identity, termination_grace_seconds=0)["status"], "succeeded")
        self.assertEqual((self.work / "once").read_text(), "1")
        script = ('import sys; from pathlib import Path; from openmachine.execution.records import read_record; '
                  'print(read_record(Path(sys.argv[1]))["status"])')
        output = subprocess.check_output([sys.executable, "-I", "-c", script, str(path.parent)], text=True)
        self.assertEqual(output.strip(), "succeeded")
        config_path = self.root / "config.toml"
        config_path.write_text('schema_version=1\n[machines.local]\ntransport="local"\n'
                               '[workspaces.project]\nmachine="local"\nroot=' + json.dumps(str(self.work)) + '\n')
        proof = service.check_state(config_path, self.state)
        self.assertEqual(proof["execution_reconciliations"], 1)
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), config_path, self.state), proof)
        for delta in ({"state_contract": 19}, {"execution_reconciliations": 0},
                      {"execution_reconciliation_sha256": "f" * 64}):
            with self.subTest(delta=delta), patch.object(service_checks, "_run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")):
                with self.assertRaises(Fault) as caught:
                    service_checks._candidate_state(Path(sys.executable), config_path, self.state)
                self.assertEqual(caught.exception.code, "incompatible_state")
        self.assertEqual(path.read_bytes(), original)

    def test_cleanup_reconciliation_rejects_missing_identity_changed_boot_and_uncertain_control(self):
        setup = 'w.FORCE_SECONDS=.05\ndef fail(*_): raise w.Fault("cleanup_unverified", "injected failure")\nw.Process.members=fail'
        with self.injected_worker(setup):
            result = self.finish(self.start('pass', termination_grace_seconds=0))
        identity = result["execution_id"]
        path = self.state / "executions" / identity / "record.json"
        original = path.read_bytes()
        legacy = json.loads(original)
        del legacy["cleanup"]["session_id"]
        del legacy["cleanup"]["boot_id"]
        path.write_text(json.dumps(legacy))
        with self.assertRaises(Fault) as caught:
            execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual(caught.exception.code, "cleanup_identity_unavailable")
        self.assertEqual(execution.read(self.config, self.state, "local", identity)["status"], "outcome_unknown")
        path.write_bytes(original)
        with patch.object(api, "boot_identity", return_value="different-boot"), self.assertRaises(Fault) as caught:
            execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual(caught.exception.code, "cleanup_identity_unavailable")
        unknown_exit = json.loads(original)
        unknown_exit["exit_code"] = None
        unknown_exit["command"].update(exited=False, exited_at=None, exit_signal=None)
        path.write_text(json.dumps(unknown_exit))
        with self.assertRaises(Fault) as caught:
            execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual(caught.exception.code, "cleanup_recovery_unavailable")
        path.write_bytes(original)
        uncertain = json.loads(original)
        uncertain["control"] = {"protocol": 1, "seq": 1, "last": {"seq": 1, "action": "interrupt",
            "request_sha256": "a" * 64, "status": "outcome_unknown", "bytes_written": None,
            "accepted_at": result["created_at"], "completed_at": result["finished_at"], "error": "lost_reply"}}
        path.write_text(json.dumps(uncertain))
        with self.assertRaises(Fault) as caught:
            execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual(caught.exception.code, "cleanup_recovery_unavailable")
        path.write_bytes(original)
        for name in ("worker.lock", "control.lock"):
            with lease(path.parent / name), self.assertRaises(Fault) as caught:
                execution.reconcile(self.config, self.state, "local", identity)
            self.assertEqual(caught.exception.code, "resource_busy")
        self.assertFalse((path.parent / "reconciliation.json").exists())
        self.assertEqual(path.read_bytes(), original)

    def test_cleanup_reconciliation_never_signals_remaining_session_members(self):
        setup = 'w.FORCE_SECONDS=.05\ndef fail(*_): raise w.Fault("cleanup_unverified", "injected failure")\nw.Process.members=fail'
        child = 'import time; time.sleep(3)'
        code = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",' + repr(child) + '])'
        with self.injected_worker(setup):
            result = self.finish(self.start(code, termination_grace_seconds=0))
        identity = result["execution_id"]
        with self.assertRaises(Fault) as caught:
            execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual(caught.exception.code, "cleanup_unverified")
        deadline = time.monotonic() + 5
        while True:
            try:
                recovered = execution.reconcile(self.config, self.state, "local", identity)
                break
            except Fault as error:
                self.assertEqual(error.code, "cleanup_unverified")
                if time.monotonic() >= deadline:
                    self.fail("Disposable original child did not exit within its deadline")
                time.sleep(.05)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertTrue(recovered["output_incomplete"])

    def test_cleanup_reconciliation_keeps_known_failure_and_binds_original_bytes(self):
        setup = 'w.FORCE_SECONDS=.05\ndef fail(*_): raise w.Fault("cleanup_unverified", "injected failure")\nw.Process.members=fail'
        with self.injected_worker(setup):
            result = self.finish(self.start('raise SystemExit(7)', termination_grace_seconds=0))
        identity = result["execution_id"]
        path = self.state / "executions" / identity / "record.json"
        original = path.read_bytes()
        recovered = execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual((recovered["status"], recovered["exit_code"]), ("failed", 7))
        path.write_bytes(original + b" ")
        with self.assertRaises(Fault) as caught:
            execution.read(self.config, self.state, "local", identity)
        self.assertEqual(caught.exception.code, "corrupt_state")

        path.write_bytes(original)
        evidence = path.parent / "reconciliation.json"
        changed = json.loads(evidence.read_text())
        changed["execution_id"] = "f" * 32
        evidence.write_text(json.dumps(changed))
        with self.assertRaises(Fault) as caught:
            execution.read(self.config, self.state, "local", identity)
        self.assertEqual(caught.exception.code, "corrupt_state")
        evidence.write_text('{"format":1}')
        with self.assertRaises(Fault) as caught:
            execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual(caught.exception.code, "corrupt_state")

    def test_cleanup_reconciliation_preserves_cancel_reason_with_zero_exit(self):
        setup = ('w.FORCE_SECONDS=.05\noriginal=w.Process.members\n'
                 'def fail_after_exit(self,deadline):\n'
                 ' if self.exited: raise w.Fault("cleanup_query_failed", "injected failure")\n'
                 ' return original(self,deadline)\nw.Process.members=fail_after_exit')
        with self.injected_worker(setup):
            started = self.start('import signal,time; signal.signal(signal.SIGTERM,lambda *_:exit(0)); print("ready",flush=True); time.sleep(30)',
                                 termination_grace_seconds=.2)
        identity = started["execution_id"]
        self.until(identity, lambda item: "ready" in item["output"])
        execution.cancel(self.config, self.state, "local", identity)
        result = self.finish(started)
        self.assertEqual((result["status"], result["exit_code"], result["stop"]["reason"]), ("outcome_unknown", 0, "cancel"))
        recovered = execution.reconcile(self.config, self.state, "local", identity)
        self.assertEqual((recovered["status"], recovered["exit_code"]), ("cancelled", 0))
        self.assertEqual(recovered["stop"], result["stop"])

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

    def test_quiet_wait_expires_without_changing_command_or_receipt(self):
        identity = self.start('import time; time.sleep(3)')["execution_id"]
        before = self.until(identity, lambda value: value["status"] == "running")
        path = self.state / "executions" / identity / "record.json"
        original = path.read_bytes()
        started = time.monotonic()
        result = execution.read(self.config, self.state, "local", identity, wait_seconds=0.12)
        self.assertGreaterEqual(time.monotonic() - started, 0.1)
        self.assertEqual((result["status"], result["exit_code"], result["wait_expired"]), ("running", None, True))
        self.assertEqual(result["created_at"], before["created_at"])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(execution.read(self.config, self.state, "local", identity)["wait_expired"])
        with self.assertRaises(Fault) as caught:
            execution.read(self.config, self.state, "local", identity, offset=1, wait_seconds=5)
        self.assertEqual(caught.exception.code, "invalid_range")

    def test_independent_waiters_wake_on_delayed_output_and_keep_cursors(self):
        gate = self.root / "emit"
        code = ('import time; from pathlib import Path; print("ready", flush=True)\n'
                f'while not Path({str(gate)!r}).exists(): time.sleep(.01)\n'
                'print("progress", flush=True); time.sleep(.2)')
        identity = self.start(code)["execution_id"]
        ready = self.until(identity, lambda value: "ready" in value["output"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(execution.read, self.config, self.state, "local", identity,
                                   ready["next_offset"], 65536, 2) for _ in range(2)]
            time.sleep(0.1)
            gate.touch()
            results = [future.result(timeout=3) for future in futures]
        for result in results:
            self.assertEqual(result["output"], "progress\n")
            self.assertFalse(result["wait_expired"])
            self.assertEqual(result["offset"], ready["next_offset"])
        self.assertEqual(results[0]["next_offset"], results[1]["next_offset"])
        again = execution.read(self.config, self.state, "local", identity, ready["next_offset"], 65536, 2)
        self.assertEqual(again["output"], "progress\n")
        self.assertFalse(again["wait_expired"])
        self.finish(again)

    def test_zero_output_exit_wakes_without_requiring_log_growth(self):
        gate = self.root / "exit"
        code = ('import time; from pathlib import Path; print("ready", flush=True)\n'
                f'while not Path({str(gate)!r}).exists(): time.sleep(.01)\n')
        identity = self.start(code)["execution_id"]
        ready = self.until(identity, lambda value: "ready" in value["output"])
        timer = threading.Timer(0.1, gate.touch)
        timer.start()
        try:
            result = execution.read(self.config, self.state, "local", identity, ready["next_offset"], wait_seconds=2)
        finally:
            timer.join()
        self.assertFalse(result["wait_expired"])
        self.assertTrue(result["command"]["exited"])
        self.assertEqual((result["output"], result["next_offset"]), ("", ready["next_offset"]))
        self.assertEqual(self.finish(result)["status"], "succeeded")

    def test_observer_cancellation_leaves_original_command_and_other_reader_live(self):
        identity = self.start('import time; time.sleep(3)')["execution_id"]
        before = self.until(identity, lambda value: value["status"] == "running")
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(Fault) as caught:
                execution.read(self.config, self.state, "local", identity, wait_seconds=5, cancelled=cancelled)
        finally:
            timer.join()
        self.assertEqual(caught.exception.code, "observation_cancelled")
        result = execution.read(self.config, self.state, "local", identity, wait_seconds=0.08)
        self.assertEqual((result["status"], result["wait_expired"], result["cancel_requested"]), ("running", True, False))
        self.assertEqual(result["created_at"], before["created_at"])
        self.assertFalse((self.state / "executions" / identity / "cancel.request").exists())

    def test_wait_wakes_for_explicit_cancellation_and_never_holds_control_lock(self):
        identity = self.start('import time; time.sleep(3)')["execution_id"]
        self.until(identity, lambda value: value["status"] == "running")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execution.read, self.config, self.state, "local", identity, 0, 65536, 2)
            time.sleep(0.1)
            requested = execution.cancel(self.config, self.state, "local", identity)
            result = future.result(timeout=3)
        self.assertTrue(requested["cancel_requested"])
        self.assertTrue(result["cancel_requested"])
        self.assertFalse(result["wait_expired"])
        self.assertEqual(self.finish(result)["status"], "cancelled")

    def test_contended_read_locks_share_the_original_bounded_budget(self):
        identity = self.start('import time; time.sleep(3)')["execution_id"]
        self.until(identity, lambda value: value["status"] == "running")
        directory = self.state / "executions" / identity
        for name in ("control.lock", "output.lock"):
            with self.subTest(lock=name), lease(directory / name, create=False):
                with self.assertRaises(Fault) as caught:
                    execution.read(self.config, self.state, "local", identity, wait_seconds=0.08)
                self.assertEqual(caught.exception.code, "observation_busy")
        self.assertEqual(execution.read(self.config, self.state, "local", identity)["status"], "running")

    def test_preparing_without_logs_waits_but_unknown_or_terminal_does_not(self):
        result = self.finish(self.start('raise SystemExit(0)'))
        identity = result["execution_id"]
        self.assertFalse(execution.read(self.config, self.state, "local", identity, wait_seconds=5)["wait_expired"])
        preparing = result | {"status": "preparing", "output_unavailable": True, "finished_at": None,
                              "exit_code": None, "command": {"spawned": False, "exited": False}}
        with patch.object(api, "_snapshot", side_effect=lambda *args: preparing | {"observed_at": time.time()}):
            observed = execution.read(self.config, self.state, "local", identity, wait_seconds=0.08)
        self.assertTrue(observed["wait_expired"])
        with patch.object(api, "_snapshot", return_value=preparing | {"status": "outcome_unknown"}):
            observed = execution.read(self.config, self.state, "local", identity, wait_seconds=5)
        self.assertEqual(observed["status"], "outcome_unknown")
        self.assertFalse(observed["wait_expired"])

    def test_forced_cleanup_covers_other_owned_groups_but_not_a_sentinel(self):
        self.assert_owned_cleanup()


if __name__ == "__main__":
    unittest.main()
