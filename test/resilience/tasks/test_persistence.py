"""Tests for tasks behavior at the resilience layer."""

import json
import subprocess
import sys
import unittest
from openmachine import tasks
from test.support.tasks import ContinuityFixture, task_contract


class StructuredTaskTests(ContinuityFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.request = task_contract()
        self.path = self.state / "tasks.db"

    def test_committed_creation_survives_restart_with_truthful_current_state(self):
        request = dict(self.request)
        value = tasks.create(self.state, request.pop("task_id"), **request)["record"]
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((value["revision"], value["plan"]["revision"]), (1, 1))
        self.assertEqual(value["operations"], [])
        self.assertIsNone(value["control"]["owner"])
        self.assertEqual(value["handoff"]["reason"], "plan_ready")
        result = subprocess.run([sys.executable, "-I", "-c",
            'import json,sys; from pathlib import Path; from openmachine import tasks; print(json.dumps(tasks.read(Path(sys.argv[1]),"structured-one")))',
            str(self.state)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["record"], value)
        self.assertEqual(observed["index_status"], "current")
        self.assertEqual(observed["observations"][0]["state"], "unknown")
        self.assertTrue(observed["support"]["supervised_admission"])
        self.assertFalse(any(observed["support"][key] for key in ("runtime_recovery", "events_delivery")))
        self.assertFalse((self.state / "executions").exists())
        self.assertFalse((self.state / "tasks").exists())
        self.assertEqual(tasks.check_state(self.state)["task_storage"]["kind"], "sqlite")

    def test_real_process_loss_before_and_after_creation_and_admission_commit(self):
        code = '''import json,os,sys
from pathlib import Path
from openmachine import tasks
from openmachine.tasks import database
request=json.load(sys.stdin)
state=Path(sys.argv[1])
point,method=sys.argv[2:4]
commit=database.commit
def interrupt(connection):
    if point=="before": os._exit(91)
    commit(connection)
    os._exit(91)
if point!="retry": database.commit=interrupt
identity=request.pop("task_id")
def admit(record,now):
    record["operations"].append({"id":"7"*32,"stage_id":"verify","plan_revision":1,"epoch":0,
        "request_sha256":"b"*64,"source_sha256":record["plan"]["stages"][0]["source_sha256"],
        "locator":{"kind":"execution","machine":"desktop","target_identity":None,
            "execution_id":"7"*32,"control_seq":None},
        "admitted_at_ms":now,"state":"admitted","observed_at_ms":None,"result":None})
    record["plan"]["stages"][0]["state"]="running"
    record["control"]["state"]="running"
    record["handoff"]=None
if method=="create":
    result=tasks.create(state,identity,**request)
else:
    result=tasks.update(state,identity,admit,expected_revision=1,
        last_update={"id":"9"*32,"request_sha256":"c"*64},operation_ids=["7"*32])
print(json.dumps(result))
'''
        for method in ("create", "admit"):
            for point in ("before", "after"):
                with self.subTest(method=method, point=point):
                    state = self.root / ("crash-" + method + "-" + point)
                    # Interrupt only the Task transaction, never initial schema creation.
                    tasks.migrate(state)
                    if method == "admit":
                        request = dict(self.request)
                        tasks.create(state, request.pop("task_id"), **request)
                    result = subprocess.run([sys.executable, "-I", "-c", code, str(state), point, method],
                        input=json.dumps(self.request), capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 91, result.stderr)
                    if method == "create" and point == "before":
                        self.assertEqual(tasks.find(state)["tasks"], [])
                    else:
                        observed = tasks.read(state, "structured-one")
                        committed = method == "admit" and point == "after"
                        self.assertEqual(observed["record"]["revision"], 2 if committed else 1)
                        self.assertEqual(observed["facts"]["operation_count"], int(committed))
                        self.assertEqual(tasks.operation(state, "structured-one", "7" * 32) is not None, committed)
                    retry = subprocess.run([sys.executable, "-I", "-c", code, str(state), "retry", method],
                        input=json.dumps(self.request), capture_output=True, text=True, timeout=15)
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    observed = json.loads(retry.stdout)
                    self.assertEqual(observed["status"], "unchanged" if point == "after" else
                                     "created" if method == "create" else "saved")
                    self.assertEqual(observed["record"]["revision"], 1 if method == "create" else 2)
                    self.assertEqual(observed["facts"]["operation_count"], int(method == "admit"))
                    if method == "admit":
                        self.assertEqual(tasks.operation(state, "structured-one", "7" * 32)["state"], "admitted")
                        self.assertEqual(tasks.control_update(state, "structured-one", "9" * 32),
                                         {"id": "9" * 32, "request_sha256": "c" * 64})
                        events = tasks.public_read(state, "structured-one", section="history")["items"]
                        self.assertEqual(sum(event["kind"] == "operation_admitted" for event in events), 1)
                    self.assertEqual(tasks.check_state(state)["task_records"], 1)
                    self.assertFalse((state / "executions").exists())

    def test_two_processes_cannot_commit_different_controls_at_one_revision(self):
        request = dict(self.request)
        tasks.create(self.state, request.pop("task_id"), **request)
        code = '''import json,sys
from pathlib import Path
from openmachine import tasks
from openmachine.foundation.sanitize import Fault
state=Path(sys.argv[1])
identity=sys.argv[2]
print("ready",flush=True)
if sys.stdin.readline()!="go\\n": raise SystemExit(90)
def stop(record,now):
    record["control"].update(state="paused",stop_intent="pause",pending_decision=identity)
    record["handoff"]=None
try:
    result=tasks.update(state,"structured-one",stop,expected_revision=1,
        last_update={"id":identity,"request_sha256":identity*2})
    print(json.dumps({"status":result["status"],"id":identity}))
except Fault as error:
    print(json.dumps({"status":error.code,"id":identity}))
'''
        processes = []
        results = []
        try:
            for identity in ("a" * 32, "b" * 32):
                processes.append(subprocess.Popen([sys.executable, "-I", "-c", code, str(self.state), identity],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            for process in processes:
                self.assertEqual(process.stdout.readline(), "ready\n")
            for process in processes:
                process.stdin.write("go\n")
                process.stdin.flush()
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                results.append(json.loads(stdout))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=15)
        self.assertEqual(sorted(item["status"] for item in results), ["saved", "task_conflict"])
        winner = next(item["id"] for item in results if item["status"] == "saved")
        loser = next(item["id"] for item in results if item["status"] == "task_conflict")
        observed = tasks.read(self.state, "structured-one")
        self.assertEqual((observed["record"]["revision"], observed["record"]["control"]["pending_decision"]), (2, winner))
        self.assertIsNotNone(tasks.control_update(self.state, "structured-one", winner))
        self.assertIsNone(tasks.control_update(self.state, "structured-one", loser))


if __name__ == "__main__":
    unittest.main()
