"""Interactive controls using the same owned-process fixture."""

from concurrent.futures import ThreadPoolExecutor
import json
import shlex
import socket
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from openmachine import execution, service
from openmachine.bootstrap import create_application
from openmachine.execution import control
from openmachine.execution.records import CONTROL_PROTOCOL, MAX_CONTROL_SEQ, MAX_INPUT, TERMINAL
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, save_json
from support_execution import ExecutionFixture


class InteractiveTests(ExecutionFixture, unittest.TestCase):
    def start(self, code, *, execution_id=None, cwd=None, tty=True, timeout_seconds=10, **options):
        identity = execution_id or uuid.uuid4().hex
        self.ids.append(identity)
        return execution.start(self.config, self.state, "local", identity, str(cwd or self.work),
                               [sys.executable, "-c", code], timeout_seconds=timeout_seconds, tty=tty, **options)

    def ready(self, identity, marker="ready"):
        return self.until(identity, lambda value: value["control_ready"] and marker in value["output"])

    def send(self, identity, sequence, action="write_stdin", text=None, terminal_size=None):
        return execution.control_execution(self.config, self.state, "local", identity,
                                           sequence, action, text, terminal_size)

    def frame(self, identity, sequence, text):
        return json.dumps({"protocol": CONTROL_PROTOCOL, "execution_id": identity,
                           **execution.control_request(sequence, "write_stdin", text)}).encode()

    def test_candidate_reader_preserves_live_and_terminal_control_receipts(self):
        config_path = self.root / "config.toml"
        config_path.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        started = self.start('print("ready",flush=True); input(); print("received",flush=True); input()')
        identity = started["execution_id"]
        self.ready(identity)
        receipt = self.send(identity, 0, text="once\n")["control_result"]
        self.assertEqual(receipt["status"], "applied")
        self.until(identity, lambda item: "received" in item["output"])
        record_path = self.state / "executions" / identity / "record.json"
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                if terminal:
                    execution.cancel(self.config, self.state, "local", identity)
                    self.assertEqual(self.finish(started)["status"], "cancelled")
                before = record_path.read_bytes()
                result = service.check_state(config_path, self.state)
                self.assertEqual(result["state_contract"], 10)
                self.assertEqual(result["execution_records"], 1)
                self.assertEqual(record_path.read_bytes(), before)
                self.assertEqual(json.loads(before)["control"]["last"], receipt)

    def test_real_terminal_dimensions_exact_input_and_terminal_duplicate(self):
        code = ('import os,sys,termios; from pathlib import Path; '
                'print("ready",[os.isatty(i) for i in range(3)],termios.tcgetwinsize(0),flush=True); '
                'line=sys.stdin.readline(); Path("received").write_text(line); print("consumed",flush=True)')
        started = self.start(code, terminal_size={"rows": 31, "cols": 92})
        identity = started["execution_id"]
        ready = self.ready(identity)
        self.assertIn("[True, True, True] (31, 92)", ready["output"])
        self.assertEqual(ready["io_mode"], "pty")
        first = self.send(identity, 0, text="literal-input")
        self.assertEqual(first["control_result"]["bytes_written"], 13)
        self.assertNotIn("output", first)
        self.assertFalse((self.work / "received").exists())  # No implicit newline.
        second = self.send(identity, 1, text="\n")
        self.assertEqual(second["control_result"]["status"], "applied")
        final = self.finish(started)
        self.assertEqual((final["status"], final["exit_code"]), ("succeeded", 0))
        self.assertFalse(final["output_incomplete"])
        self.assertEqual((self.work / "received").read_bytes(), b"literal-input\n")
        self.assertFalse(control.socket_path(self.state, identity).exists())
        self.assertEqual(self.send(identity, 1, text="\n")["control_result"], second["control_result"])
        with self.assertRaises(Fault) as caught:
            self.send(identity, 2, text="new\n")
        self.assertEqual(caught.exception.code, "execution_finished")

    def test_resize_preserves_initial_request_and_wakes_observer(self):
        code = 'import sys,time; print("ready",flush=True); time.sleep(3)'
        identity = self.start(code)["execution_id"]
        ready = self.ready(identity)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execution.read, self.config, self.state, "local", identity,
                                 ready["next_offset"], 65536, 2)
            time.sleep(0.05)
            resized = self.send(identity, 0, "resize", terminal_size={"rows": 45, "cols": 120})
            observed = future.result(timeout=3)
        self.assertEqual(resized["control_result"]["status"], "applied")
        self.assertFalse(observed["wait_expired"])
        record = execution.read_record(self.state / "executions" / identity)
        self.assertEqual(record["terminal_size"], {"rows": 45, "cols": 120})
        self.assertEqual(record["request"]["terminal_size"], {"rows": 24, "cols": 80})
        same = self.start(code, execution_id=identity)
        self.assertEqual(same["created_at"], ready["created_at"])
        for options in ({"tty": False}, {"terminal_size": {"rows": 45, "cols": 120}}):
            with self.assertRaises(Fault) as caught:
                self.start(code, execution_id=identity, **options)
            self.assertEqual(caught.exception.code, "execution_conflict")

    def test_real_python_repl_survives_interrupt_with_original_state(self):
        identity = uuid.uuid4().hex
        self.ids.append(identity)
        execution.start(self.config, self.state, "local", identity, str(self.work),
                        [sys.executable, "-I", "-q", "-i", "-c", ""], timeout_seconds=15, tty=True)
        self.until(identity, lambda value: value["control_ready"])
        self.send(identity, 0, text="x = 41\nprint('state-ready')\n")
        self.until(identity, lambda value: "state-ready" in value["output"])
        self.send(identity, 1, text="while True: pass\n\n")
        time.sleep(0.1)
        interrupted = self.send(identity, 2, "interrupt")
        self.assertEqual(interrupted["control_result"]["status"], "applied")
        self.until(identity, lambda value: "KeyboardInterrupt" in value["output"])
        self.send(identity, 3, text="print('answer', x + 1)\n")
        result = self.until(identity, lambda value: "answer 42" in value["output"])
        self.assertEqual(result["status"], "running")
        self.assertIsNone(result["stop"]["reason"])
        self.assertEqual(result["control"]["seq"], 4)

    def test_shell_foreground_interrupt_and_real_resize_keep_shell_alive(self):
        identity = uuid.uuid4().hex
        self.ids.append(identity)
        execution.start(self.config, self.state, "local", identity, str(self.work),
                        ["/bin/sh", "-i"], timeout_seconds=15, tty=True)
        self.until(identity, lambda value: value["control_ready"])
        child = ('import time; from pathlib import Path; '
                 'Path("foreground-ready").touch(); time.sleep(8)')
        self.send(identity, 0, text=shlex.join([sys.executable, "-c", child]) + "\n")
        self.until(identity, lambda _: (self.work / "foreground-ready").exists())
        self.assertEqual(self.send(identity, 1, "interrupt")["control_result"]["status"], "applied")
        self.send(identity, 2, "resize", terminal_size={"rows": 40, "cols": 101})
        query = 'import termios; print("shell-alive",termios.tcgetwinsize(0))'
        self.send(identity, 3, text=shlex.join([sys.executable, "-c", query]) + "\n")
        observed = self.until(identity, lambda value: "shell-alive (40, 101)" in value["output"])
        self.assertEqual(observed["status"], "running")

    def test_new_pipe_interrupt_does_not_open_stdin_or_cancel_execution(self):
        code = ('import signal,time; signal.signal(signal.SIGINT,lambda *_:print("interrupted",flush=True)); '
                'print("ready",flush=True); time.sleep(4)')
        identity = self.start(code, tty=False)["execution_id"]
        self.ready(identity)
        for action, text, size in (("write_stdin", "x", None), ("resize", None, {"rows": 1, "cols": 1})):
            with self.assertRaises(Fault) as caught:
                self.send(identity, 0, action, text, size)
            self.assertEqual(caught.exception.code, "unsupported_operation")
        result = self.send(identity, 0, "interrupt")
        self.assertEqual(result["control_result"]["status"], "applied")
        observed = self.until(identity, lambda value: "interrupted" in value["output"])
        self.assertIsNone(observed["stop"]["reason"])
        self.assertEqual(self.send(identity, 0, "interrupt")["control_result"], result["control_result"])
        self.assertEqual(self.output(identity).count("interrupted"), 1)

    def test_concurrent_input_admits_at_most_one_without_interleaving(self):
        code = ('import sys,time; from pathlib import Path; print("ready",flush=True); '
                'line=sys.stdin.readline(); Path("received").write_text(line); time.sleep(2)')
        identity = self.start(code)["execution_id"]
        self.ready(identity)
        def send(text):
            try:
                return self.send(identity, 0, text=text)["control_result"]["status"]
            except Fault as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, ("alpha\n", "beta\n")))
        self.assertEqual(results.count("applied"), 1)
        self.assertTrue(set(results) <= {"applied", "control_busy", "control_conflict"}, results)
        self.until(identity, lambda _: (self.work / "received").exists())
        self.assertIn((self.work / "received").read_text(), ("alpha\n", "beta\n"))
        self.assertEqual(execution.read_record(self.state / "executions" / identity)["control"]["seq"], 1)

    def test_two_contending_connects_reach_worker_admission_before_dispatch(self):
        gate = self.work / "dispatch-gate"
        setup = ('original_run=w._run\n'
                 'def gated(*args):\n'
                 f' gate=Path({str(gate)!r}); end=w.time.monotonic()+3\n'
                 ' while not gate.exists() and w.time.monotonic()<end: w.time.sleep(.01)\n'
                 ' return original_run(*args)\n'
                 'w._run=gated')
        with self.injected_worker(setup):
            identity = self.start('import time; print("ready",flush=True); input(); time.sleep(2)')["execution_id"]
        self.until(identity, lambda value: value["control_ready"])
        connections = []
        try:
            for _ in range(2):
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connections.append(connection)
                connection.settimeout(1)
                connection.connect(str(control.socket_path(self.state, identity)))
            gate.touch()
            for connection, text in zip(connections, ("alpha\n", "beta\n"), strict=True):
                connection.settimeout(3)
                connection.sendall(self.frame(identity, 0, text))
                connection.shutdown(socket.SHUT_WR)
            statuses = []
            for connection in connections:
                data = bytearray()
                while chunk := connection.recv(65536):
                    data.extend(chunk)
                response = json.loads(data)
                statuses.append(response["error"]["code"] if "error" in response
                                else response["result"]["control_result"]["status"])
            self.assertEqual(statuses.count("applied"), 1)
            self.assertTrue(set(statuses) <= {"applied", "control_busy", "control_conflict"}, statuses)
        finally:
            gate.touch()
            for connection in connections:
                connection.close()

    def test_lost_response_duplicate_conflict_and_retired_result_never_replay(self):
        code = ('import sys,time; from pathlib import Path; print("ready",flush=True)\n'
                'for line in sys.stdin:\n p=Path("count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print("consumed",flush=True)')
        identity = self.start(code)["execution_id"]
        self.ready(identity)
        text = "unique-control-body\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(control.socket_path(self.state, identity)))
            connection.sendall(self.frame(identity, 0, text))
            connection.shutdown(socket.SHUT_WR)  # Drop the acknowledgement, not the worker.
        observed = self.until(identity, lambda value: value["control"]["seq"] == 1 and value["control"]["last"]["status"] == "applied")
        self.until(identity, lambda _: (self.work / "count").exists())
        original = self.send(identity, 0, text=text)
        self.assertEqual(original["control_result"], observed["control"]["last"])
        self.assertEqual((self.work / "count").read_text(), "1")
        with self.assertRaises(Fault) as conflict:
            self.send(identity, 0, text="different\n")
        self.assertEqual(conflict.exception.code, "control_conflict")
        self.send(identity, 1, "resize", terminal_size={"rows": 25, "cols": 81})
        with self.assertRaises(Fault) as retired:
            self.send(identity, 0, text=text)
        self.assertEqual(retired.exception.code, "control_result_unavailable")
        self.assertEqual((self.work / "count").read_text(), "1")
        self.assertNotIn(text.strip(), (self.state / "executions" / identity / "record.json").read_text())

    def test_invalid_inputs_modes_and_frames_reserve_no_sequence(self):
        for tty, size in ((1, None), (False, {"rows": 2, "cols": 3}), (True, {"rows": True, "cols": 3}),
                          (True, {"rows": 0, "cols": 3}), (True, {"rows": 2, "cols": 1001}),
                          (True, {"rows": 2, "cols": 3, "extra": 1})):
            with self.assertRaises(Fault):
                self.start('from pathlib import Path; Path("effect").touch()', tty=tty, terminal_size=size)
        for sequence in (-1, True, 0.5, MAX_CONTROL_SEQ, "0"):
            with self.assertRaises(Fault):
                execution.control_request(sequence, "interrupt")
        for text in ("", "\0", "\ud800", "x" * (MAX_INPUT + 1), "\u00e9" * (MAX_INPUT // 2 + 1), 1):
            with self.assertRaises(Fault):
                execution.control_request(0, "write_stdin", text)
        self.assertFalse((self.work / "effect").exists())
        identity = self.start('import time; print("ready",flush=True); time.sleep(5)')["execution_id"]
        self.ready(identity)
        for frame in (b"{}{}", b'{"protocol":1,"protocol":1}', b"[]", b"[" * 2000 + b"0" + b"]" * 2000, self.frame(identity, 0, "x")[:-1] + b',"extra":1}'):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect(str(control.socket_path(self.state, identity)))
                connection.sendall(frame)
                connection.shutdown(socket.SHUT_WR)
                response = bytearray()
                while chunk := connection.recv(65536):
                    response.extend(chunk)
            self.assertEqual(json.loads(response)["error"]["code"], "invalid_control")
        self.assertEqual(execution.read_record(self.state / "executions" / identity)["control"]["seq"], 0)

    def test_slow_control_frame_cannot_block_cancellation(self):
        identity = self.start('import time; print("ready",flush=True); time.sleep(5)')["execution_id"]
        self.ready(identity)
        other = self.root / "other"
        other.mkdir()
        self.config.workspaces["other"] = self.config.workspaces["project"].model_copy(update={"root": str(other)})
        independent = self.start('import time; print("ready",flush=True); time.sleep(5)', cwd=other)["execution_id"]
        self.ready(independent)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
            slow.connect(str(control.socket_path(self.state, identity)))
            slow.sendall(b'{"protocol":')
            time.sleep(0.1)
            resized = self.send(independent, 0, "resize", terminal_size={"rows": 30, "cols": 90})
            self.assertEqual(resized["control_result"]["status"], "applied")
            original_peer_check = control._check_peer
            def delayed_peer_check(connection):
                time.sleep(0.15)  # A busy peer must not disappear before credential verification.
                return original_peer_check(connection)
            with patch.object(control, "_check_peer", side_effect=delayed_peer_check):
                with self.assertRaises(Fault) as caught:
                    self.send(identity, 0, "interrupt")
            self.assertEqual(caught.exception.code, "control_busy")
            execution.cancel(self.config, self.state, "local", identity)
            result = self.until(identity, lambda value: value["status"] in TERMINAL)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["control"]["seq"], 0)

    def test_backpressured_input_has_a_bounded_prefix_and_cancel_progresses(self):
        code = ('import os,time,tty; tty.setraw(0); print("ready",flush=True)\n'
                'while True: os.write(1,b"tick\\n"); time.sleep(.03)')
        identity = self.start(code)["execution_id"]
        self.ready(identity)
        partial = None
        for sequence in range(8):
            receipt = self.send(identity, sequence, text="x" * MAX_INPUT)["control_result"]
            if receipt["status"] != "applied":
                partial = receipt
                break
        self.assertIsNotNone(partial, "The fixture did not reach terminal input backpressure within its bound.")
        self.assertIn(partial["status"], {"partial", "not_applied"})
        self.assertLess(partial["bytes_written"], MAX_INPUT)
        self.assertEqual(self.send(identity, partial["seq"] - 1, text="x" * MAX_INPUT)["control_result"], partial)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.send, identity, partial["seq"], text="y" * MAX_INPUT)
            self.until(identity, lambda value: value["control"]["last"]["status"] == "pending")
            execution.cancel(self.config, self.state, "local", identity)
            try:
                last = future.result(timeout=4)["control_result"]
            except Fault as error:
                self.assertEqual(error.code, "outcome_unknown")
                last = self.send(identity, partial["seq"], text="y" * MAX_INPUT)["control_result"]
        self.assertIn(last["status"], {"partial", "not_applied"})
        final = self.until(identity, lambda value: value["status"] in TERMINAL)
        self.assertEqual(final["status"], "cancelled")
        self.assertIn("tick", self.output(identity))

    def test_persist_intent_failure_has_no_input_effect(self):
        setup = ('original=w.save_json\n'
                 'def fail(path,record):\n'
                 ' last=(record.get("control") or {}).get("last")\n'
                 ' if last and last["status"]=="pending": raise OSError("intent fault")\n'
                 ' original(path,record)\n'
                 'w.save_json=fail')
        with self.injected_worker(setup):
            identity = self.start('from pathlib import Path; print("ready",flush=True); line=input(); Path("received").write_text(line)')["execution_id"]
        self.ready(identity)
        with self.assertRaises(Fault) as caught:
            self.send(identity, 0, text="must-not-apply\n")
        self.assertEqual(caught.exception.code, "control_persistence_failed")
        self.until(identity, lambda value: value["status"] in TERMINAL)
        self.assertFalse((self.work / "received").exists())

    def test_worker_loss_after_intent_or_effect_retains_unknown_without_replay(self):
        for after_effect in (False, True):
            with self.subTest(after_effect=after_effect):
                destination = "effect-count" if after_effect else "no-effect-count"
                phase = "applied" if after_effect else "pending"
                setup = ('original=w.save_json\n'
                         'def fail(path,record):\n'
                         ' last=(record.get("control") or {}).get("last")\n'
                         f' if last and last["status"]=={phase!r}:\n'
                         + ('  end=w.time.monotonic()+1\n'
                            f'  while not (Path(record["request"]["cwd"])/{destination!r}).exists() and w.time.monotonic()<end: w.time.sleep(.01)\n'
                            if after_effect else '  original(path,record)\n') +
                         '  w.os._exit(77)\n'
                         ' original(path,record)\n'
                         'w.save_json=fail')
                code = ('import sys,select,signal,time; from pathlib import Path; signal.signal(signal.SIGHUP,signal.SIG_IGN); '
                        'print("ready",flush=True)\n'
                        'if select.select([sys.stdin],[],[],1)[0]:\n'
                        f' line=sys.stdin.readline()\n if line: Path({destination!r}).write_text("1")\n')
                with self.injected_worker(setup):
                    identity = self.start(code)["execution_id"]
                self.ready(identity)
                try:
                    self.send(identity, 0, text="one-effect\n")
                except Fault as error:
                    self.assertEqual(error.code, "outcome_unknown")
                unknown = self.until(identity, lambda value: value["status"] == "outcome_unknown")
                self.assertEqual(unknown["control"]["last"]["status"], "outcome_unknown")
                self.assertIsNone(unknown["control"]["last"]["bytes_written"])
                self.assertEqual(self.send(identity, 0, text="one-effect\n")["control_result"]["status"], "outcome_unknown")
                with self.assertRaises(Fault) as caught:
                    self.send(identity, 1, text="new-effect\n")
                self.assertEqual(caught.exception.code, "control_unresolved")
                time.sleep(1.1)  # The deliberately orphaned fixture is itself bounded.
                if after_effect:
                    self.assertEqual((self.work / destination).read_text(), "1")
                else:
                    self.assertFalse((self.work / destination).exists())

    def test_final_save_failure_freezes_controls_and_keeps_cancel_available(self):
        setup = ('original=w.save_json\n'
                 'def fail(path,record):\n'
                 ' last=(record.get("control") or {}).get("last")\n'
                 ' if last and last["status"]=="applied": raise OSError("final fault")\n'
                 ' original(path,record)\n'
                 'w.save_json=fail')
        with self.injected_worker(setup):
            identity = self.start('import time; print("ready",flush=True); time.sleep(4)')["execution_id"]
        self.ready(identity)
        result = self.send(identity, 0, "resize", terminal_size={"rows": 30, "cols": 90})
        self.assertEqual(result["control_result"]["status"], "outcome_unknown")
        with self.assertRaises(Fault) as caught:
            self.send(identity, 1, text="new-effect\n")
        self.assertEqual(caught.exception.code, "control_unresolved")
        execution.cancel(self.config, self.state, "local", identity)
        final = self.until(identity, lambda value: value["status"] in TERMINAL)
        self.assertEqual(final["cleanup"]["status"], "verified")
        self.assertEqual(final["control"]["last"]["status"], "outcome_unknown")

    def test_permissions_lifecycle_and_idle_terminal_leases(self):
        identity = self.start('import signal,time; signal.signal(signal.SIGINT,lambda *_:None); print("ready",flush=True); time.sleep(5)')["execution_id"]
        self.ready(identity)
        application = create_application(self.config, self.state)
        self.config.machines["local"].execution_enabled = False
        for action, text, size in (("write_stdin", "x", None), ("resize", None, {"rows": 2, "cols": 3})):
            with self.assertRaises(Fault) as caught:
                application.control_execution("local", identity, 0, action, text, size)
            self.assertEqual(caught.exception.code, "execution_disabled")
        self.assertEqual(application.control_execution("local", identity, 0, "interrupt")["control_result"]["status"], "applied")
        with lease(self.state / "locks/lifecycle.lock", create=False), self.assertRaises(Fault) as caught:
            application.control_execution("local", identity, 1, "interrupt")
        self.assertEqual(caught.exception.code, "resource_busy")
        self.config.machines["local"].execution_enabled = True
        with self.assertRaises(Fault) as caught:
            self.start("pass")
        self.assertEqual(caught.exception.code, "resource_busy")
        self.config.machines["local"].execution_enabled = False
        application.cancel_execution("local", identity)
        self.assertEqual(self.until(identity, lambda value: value["status"] in TERMINAL)["status"], "cancelled")

    def test_pty_exec_failure_is_not_a_program_returning_127(self):
        identity = uuid.uuid4().hex
        result = execution.start(self.config, self.state, "local", identity, str(self.work),
                                 [str(self.work / "absent")], tty=True)
        self.ids.append(identity)
        failed = self.finish(result)
        self.assertEqual((failed["status"], failed["exit_code"], failed["command"]["spawned"]), ("failed", None, False))
        self.assertFalse(failed["command"]["exited"])
        self.assertEqual(failed["cleanup"]["status"], "verified")
        exited = self.finish(self.start("raise SystemExit(127)"))
        self.assertEqual((exited["status"], exited["exit_code"], exited["command"]["spawned"]), ("failed", 127, True))

    def test_legacy_v4_facts_survive_and_controls_remain_unsupported(self):
        result = self.finish(self.start("pass", tty=False))
        directory = self.state / "executions" / result["execution_id"]
        record = execution.read_record(directory)
        record["format_version"] = 4
        for key in ("tty", "terminal_size"):
            del record["request"][key]
        for key in ("control", "control_ready", "terminal_size"):
            del record[key]
        save_json(directory / "record.json", record)
        before = (directory / "record.json").read_bytes()
        observed = execution.read(self.config, self.state, "local", directory.name)
        self.assertEqual(observed["command"], record["command"])
        self.assertEqual(observed["cleanup"]["status"], "verified")
        self.assertIsNone(observed["control"])
        with self.assertRaises(Fault) as caught:
            self.send(directory.name, 0, "interrupt")
        self.assertEqual(caught.exception.code, "unsupported_operation")
        self.assertEqual((directory / "record.json").read_bytes(), before)

    def test_long_control_path_fails_before_pty_launch_without_pipe_fallback(self):
        state = self.root / ("s" * 70) / "state"
        with self.assertRaises(Fault) as caught:
            execution.start(self.config, state, "local", uuid.uuid4().hex, str(self.work),
                            [sys.executable, "-c", 'from pathlib import Path; Path("effect").touch()'], tty=True)
        self.assertEqual(caught.exception.code, "control_path_too_long")
        self.assertFalse((self.work / "effect").exists())
        identity = uuid.uuid4().hex
        execution.start(self.config, state, "local", identity, str(self.work), [sys.executable, "-c", "pass"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = execution.read(self.config, state, "local", identity)
            if result["status"] in TERMINAL:
                break
            time.sleep(.02)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual((result["io_mode"], result["control_actions"]), ("pipe", []))

    def test_pty_owned_job_groups_are_cleaned_without_stopping_other_sessions(self):
        self.assert_owned_cleanup()
