"""Tests for tasks behavior at the resilience layer."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
import subprocess
import sys
import unittest
from openmachine.chat import tasks
from openmachine.foundation.sanitize import Fault
from openmachine.chat.tasks import database
from test.support.tasks import ContinuityFixture, task_contract
from test.support.goal_recovery import GoalRecoveryFixture
from test.support.github_cases import ACTOR, REPO


class GoalRecoveryResilienceTests(GoalRecoveryFixture, unittest.TestCase):
    def test_restart_before_original_result_publication_retains_unknown_before_goal(self):
        identity = "c" * 32
        arguments = dict(action="resolve", repository=REPO, number=2, thread_id="PRRT_goal",
                         expected_head_sha=self.provider.pr["head"]["sha"],
                         expected_base_sha=self.provider.pr["base"]["sha"],
                         task_context=self.context_for(identity))
        with patch("openmachine.chat.app.task_admission.TaskAdmission._publish", side_effect=SystemExit(91)):
            with self.assertRaises(SystemExit):
                self.app.github_review_thread(**arguments)
        admitted = tasks.operation(self.state, "structured-one", identity)
        self.assertEqual(admitted["state"], "admitted")
        self.assertIsNone(admitted["result"])
        self.assertTrue(self.provider.threads[0]["isResolved"])
        self.connect()
        recovered = self.recover(arguments)
        self.assertEqual(recovered["status"], "write_outcome_unknown")
        self.assertEqual(recovered["goal_witness"]["operation_id"], identity)
        original = tasks.operation(self.state, "structured-one", identity)
        self.assertEqual(original["state"], "unknown")
        self.assertEqual(original["result"]["status"], "unknown")
        for field in ("id", "locator", "request_sha256", "source_sha256", "admitted_at_ms"):
            self.assertEqual(original[field], admitted[field])
        self.assertEqual(self.recover(arguments)["goal_witness"], recovered["goal_witness"])
        self.accept_goal(original)
        self.control("finish", outcome="completed", acceptance=[
            {"stage_id": "verify", "operation_id": identity}], limitations="Original write attribution remains unknown.")
        self.assertEqual(tasks.operation(self.state, "structured-one", identity), original)
        self.assertEqual(self.thread_writes, 1)
        tasks.check_state(self.state)

    def crash_recovery(self, point):
        arguments, original = self.lose_ack()
        code = '''import json,os,sys
from pathlib import Path
from unittest.mock import Mock
from openmachine.chat import tasks
from openmachine.chat.bootstrap import create_application
from openmachine.config import Settings
from openmachine.chat.tasks import database
data=json.load(sys.stdin); state=Path(sys.argv[1]); point=sys.argv[2]
app=create_application(Settings.model_validate(data["config"]),state)
root="repos/"+data["repository"]
def transport(method,endpoint,payload=None):
    if method=="GET":
        values={"user":data["actor"],root:data["metadata"],root+"/pulls/2":data["pr"]}
        if endpoint in values: return values[endpoint]
    if method=="POST" and endpoint=="graphql" and payload["query"].startswith("query"):
        return {"data":{"repository":{"pullRequest":{"headRefOid":data["pr"]["head"]["sha"],
            "baseRefOid":data["pr"]["base"]["sha"],"reviewThreads":{"nodes":data["threads"],
            "pageInfo":{"hasNextPage":False,"endCursor":None}}}}}}
    raise AssertionError("Recovery repeated a mutation or unexpected request: "+method+" "+endpoint)
app.connections.request=Mock(return_value=transport)
record=tasks.read(state,"structured-one")["record"]
arguments=data["arguments"]
arguments["task_context"].update(task_revision=record["revision"],plan_revision=record["plan"]["revision"],
                               continuation_epoch=record["control"]["epoch"])
commit=database.commit
def interrupted(connection):
    if point=="before": os._exit(91)
    commit(connection)
    if point=="after": os._exit(91)
database.commit=interrupted
print(json.dumps(app.github_review_thread(**arguments)))
'''
        data = {"config": self.config.model_dump(mode="json"), "repository": REPO, "actor": ACTOR,
                "metadata": self.provider.metadata, "pr": self.provider.pr,
                "threads": self.provider.threads, "arguments": arguments}
        result = subprocess.run([sys.executable, "-I", "-c", code, str(self.state), point],
                                input=json.dumps(data), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 91, result.stderr)
        record = tasks.read(self.state, "structured-one")["record"]
        self.assertEqual(len(record.get("goal_witnesses", [])), int(point == "after"))
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        restarted = subprocess.run([sys.executable, "-I", "-c", code, str(self.state), "retry"],
                                   input=json.dumps(data), capture_output=True, text=True, timeout=15)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(json.loads(restarted.stdout)["status"], "write_outcome_unknown")
        self.assertEqual(len(tasks.read(self.state, "structured-one")["record"]["goal_witnesses"]), 1)
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)
        tasks.check_state(self.state)

    def test_process_loss_before_goal_commit_recovers_without_repeating_write(self):
        self.crash_recovery("before")

    def test_process_loss_after_goal_commit_reuses_exact_witness_without_repeating_write(self):
        self.crash_recovery("after")

    def test_simultaneous_recovery_excludes_competitor_and_publishes_one_witness(self):
        arguments, original = self.lose_ack()
        arguments["task_context"] = self.context_for(original["id"])
        entered, release = Event(), Event()
        read = self.app.task_receipts.read
        def held(*args, **kwargs):
            observed = read(*args, **kwargs)
            entered.set()
            self.assertTrue(release.wait(5))
            return observed
        with patch.object(self.app.task_receipts, "read", side_effect=held), ThreadPoolExecutor(max_workers=1) as pool:
            recovery = pool.submit(self.app.github_review_thread, **arguments)
            try:
                self.assertTrue(entered.wait(5))
                self.fault("resource_busy", self.app.github_review_thread, **arguments)
            finally:
                release.set()
            witness = recovery.result(timeout=5)["goal_witness"]
        self.assertEqual(self.recover(arguments)["goal_witness"], witness)
        self.assertEqual(len(self.view()["record"]["goal_witnesses"]), 1)
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)


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
            'import json,sys; from pathlib import Path; from openmachine.chat import tasks; print(json.dumps(tasks.read(Path(sys.argv[1]),"structured-one")))',
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
from openmachine.chat import tasks
from openmachine.chat.tasks import database
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
        "locator":{"kind":"execution","machine":"desktop",
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

    def test_interrupted_initialization_resumes_without_replacing_original_authority(self):
        code = '''import json,os,sys
from pathlib import Path
from openmachine.chat import tasks
from openmachine.chat.tasks import database
state=Path(sys.argv[1]); point,entry=sys.argv[2:4]
request=json.load(sys.stdin)
commit,link,rename,write,truncate=database.commit,os.link,os.rename,os.write,os.ftruncate
def interrupt_commit(connection):
    if point=="schema_before": os._exit(91)
    commit(connection)
    if point=="schema_after": os._exit(91)
def interrupt_link(source,target,**kwargs):
    link(source,target,**kwargs)
    if point=="lock_linked" and Path(target)==state/"locks/tasks.lock": os._exit(91)
def interrupt_rename(source,target):
    rename(source,target)
    if point=="published" and Path(target)==state/"tasks.db": os._exit(91)
def interrupt_write(fd,data):
    if point=="prepared_partial" and data==database.INITIALIZING:
        write(fd,data[:5]); os._exit(91)
    return write(fd,data)
def interrupt_truncate(fd,size):
    truncate(fd,size)
    if point=="marker_cleared" and size==0: os._exit(91)
database.commit=interrupt_commit
os.link,os.rename,os.write,os.ftruncate=interrupt_link,interrupt_rename,interrupt_write,interrupt_truncate
if entry=="create":
    identity=request.pop("task_id")
    tasks.create(state,identity,**request)
else:
    tasks.migrate(state)
'''
        for entry in ("create", "migrate"):
            points = ("schema_before", "schema_after", "published",
                      "prepared_partial", "lock_linked", "marker_cleared")
            for point in points:
                with self.subTest(entry=entry, point=point):
                    state = self.root / ("initialize-" + entry + "-" + point)
                    result = subprocess.run([sys.executable, "-I", "-c", code, str(state), point, entry],
                        input=json.dumps(self.request), capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 91, result.stderr)
                    lock = state / "locks/tasks.lock"
                    original = lock.stat() if lock.exists() else None
                    marker = lock.read_bytes() if original else None
                    if point == "lock_linked":
                        self.assertEqual(original.st_nlink, 2)
                    if point in {"schema_before", "schema_after", "prepared_partial", "lock_linked"}:
                        self.assertFalse((state / "tasks.db").exists())
                    tasks.storage_state(state)
                    if original:
                        self.assertEqual(lock.read_bytes(), marker)
                        self.assertEqual(lock.stat().st_nlink, original.st_nlink)
                    if point == "published":
                        with self.assertRaises(Fault) as caught:
                            with database.connect(state, write=True):
                                self.fail("Pending initialization admitted a write")
                        self.assertEqual(caught.exception.code, "task_activation_pending")
                    retry = subprocess.run([sys.executable, "-I", "-c", code, str(state), "retry", entry],
                        input=json.dumps(self.request), capture_output=True, text=True, timeout=15)
                    self.assertEqual(retry.returncode, 0, retry.stderr)
                    retained = lock.stat()
                    if original:
                        self.assertEqual((retained.st_dev, retained.st_ino), (original.st_dev, original.st_ino))
                    self.assertEqual((retained.st_nlink, lock.read_bytes()), (1, b""))
                    self.assertEqual(tasks.check_state(state)["task_records"], int(entry != "migrate"))
                    self.assertFalse((state / "tasks").exists())
                    self.assertFalse((state / "executions").exists())

    def test_two_processes_cannot_commit_different_controls_at_one_revision(self):
        request = dict(self.request)
        tasks.create(self.state, request.pop("task_id"), **request)
        code = '''import json,sys
from pathlib import Path
from openmachine.chat import tasks
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
