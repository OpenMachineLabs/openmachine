"""Tests for app behavior at the resilience layer."""

import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from openmachine import tasks
from openmachine.execution.records import TERMINAL
from openmachine.tasks import database, store as task_store
from openmachine.bootstrap import create_application
from openmachine.foundation.state import lease
from test.support.tasks import TaskFixture, task_contract
from test.support.execution import ExecutionFixture



class TaskAdmissionTests(TaskFixture, unittest.TestCase):
    def test_original_cleanup_recovery_unblocks_task_observation_without_accepting_or_replaying(self):
        self.accept_plan()
        args = self.start_arguments('from pathlib import Path; Path("once").write_text("1")', termination_grace_seconds=0)
        setup = 'w.FORCE_SECONDS=.05\ndef fail(*_): raise w.Fault("cleanup_query_timeout", "injected failure")\nw.Process.members=fail'
        with ExecutionFixture.injected_worker(self, setup):
            terminal = self.wait(self.app.start_execution(**args))
        self.assertEqual(terminal["status"], "outcome_unknown")
        observed = self.app.task_admission.reconcile(self.view())["record"]
        self.assertEqual(observed["operations"][0]["state"], "unknown")
        revision = dict(contract=observed["contract"], stages=self.request["stages"],
                        invalidated_stage_ids=["verify"], reason="Continue with original cleanup evidence.")
        self.fault("task_effect_unresolved", self.control, "revise_plan", **revision)
        before = self.task_bytes()
        recovered = self.app.reconcile_execution("desktop", args["execution_id"])
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(self.task_bytes(), before)
        current = self.app.task_admission.reconcile(self.view())["record"]
        self.assertEqual(current["operations"][0]["result"]["status"], "succeeded")
        self.assertEqual(current["plan"], observed["plan"])
        self.assertIsNone(current["terminal"])
        self.assertEqual((self.work / "once").read_text(), "1")
        revised = self.control("revise_plan", **revision)["record"]
        self.assertEqual(revised["plan"]["revision"], current["plan"]["revision"] + 1)
        self.assertEqual(revised["operations"], current["operations"])

    def test_committed_head_can_reattach_but_requires_revision_before_new_effects(self):
        git = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
        subprocess.run(["git", "init", "-q", str(self.work)], check=True, capture_output=True)
        subprocess.run(git + ["commit", "--allow-empty", "-qm", "runtime(fixture): initial head"],
                       cwd=self.work, check=True, capture_output=True)
        self.context = self.app.read_workspace_file("desktop", "project", "AGENTS.md")["context"]
        self.request = task_contract(self.context)
        self.accept_plan()
        args = self.start_arguments(expected_head_sha=self.context["head_sha"])
        args["argv"] = git + ["commit", "--allow-empty", "-qm", "runtime(fixture): advance head"]
        self.assertEqual(self.wait(self.app.start_execution(**args))["status"], "succeeded")
        original = self.app.task_admission.reconcile(self.view())["record"]
        owner = original["control"]["owner"]
        with patch.object(task_store.time, "time_ns", return_value=(owner["expires_at_ms"] + 1) * 1000000):
            claimed = self.control("claim", handoff_id=original["handoff"]["id"])["record"]
        self.assertEqual(claimed["operations"], original["operations"])
        self.assertEqual(claimed["contract"], original["contract"])
        write = self.write_arguments()
        write["expected_head_sha"] = self.context["head_sha"]
        self.fault("context_mismatch", self.app.write_workspace_file, **write)
        self.fault("context_mismatch", self.control, "record_decision", stage_id="verify",
                   outcome="accepted", evidence=[args["execution_id"]], reason="Stale HEAD is not acceptance.")
        self.control("pause", reason="Reattach after the task's own commit.")
        current = self.app.read_workspace_file("desktop", "project", "AGENTS.md")["context"]
        with patch.object(self.app.machines, "read_workspace_file",
                          return_value={"context": dict(current, workspace_identity="f" * 64)}):
            self.fault("context_mismatch", self.control, "resume", reason="Wrong workspace must remain blocked.")
        resumed = self.control("resume", reason="Continue in the original workspace.")["record"]
        # Keep HEAD unchanged in this mock so the write rejects only the wrong workspace identity.
        with patch.object(self.app.machines, "read_workspace_file",
                          return_value={"context": dict(current, workspace_identity="f" * 64,
                                                        head_sha=self.context["head_sha"])}):
            reattached = self.control("claim", handoff_id=resumed["handoff"]["id"])["record"]
            self.assertEqual(reattached["operations"], original["operations"])
            self.assertEqual(reattached["contract"], original["contract"])
            write = self.write_arguments()
            write["expected_head_sha"] = self.context["head_sha"]
            self.fault("context_mismatch", self.app.write_workspace_file, **write)
        write = self.write_arguments()
        write["expected_head_sha"] = current["head_sha"]
        self.fault("context_mismatch", self.app.write_workspace_file, **write)
        self.assertEqual(self.view()["record"]["operations"], original["operations"])
        contract = json.loads(json.dumps(original["contract"]))
        contract["targets"][0]["head_sha"] = current["head_sha"]
        revised = self.control("revise_plan", contract=contract, stages=self.request["stages"],
            invalidated_stage_ids=["verify"], reason="Reconcile the verified original commit.")["record"]
        self.assertEqual(revised["operations"], original["operations"])
        self.control("claim", handoff_id=revised["handoff"]["id"])
        write = self.write_arguments()
        write["expected_head_sha"] = current["head_sha"]
        self.assertEqual(self.app.write_workspace_file(**write)["status"], "applied")
        self.assertEqual(tasks.read(self.state, "structured-one")["facts"]["operation_count"], 2)
        self.assertEqual(subprocess.check_output(["git", "rev-list", "--count", "HEAD"],
                         cwd=self.work, text=True).strip(), "2")

    def test_original_execution_finishes_once_without_read_mutations(self):
        self.accept_plan()
        args = self.start_arguments('from pathlib import Path; p=Path("count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")')
        result = self.app.start_execution(**args)
        terminal = self.wait(result)
        self.assertEqual(terminal["status"], "succeeded")
        before = self.task_bytes()
        self.app.read_execution("desktop", args["execution_id"])
        self.view()
        self.assertEqual(self.task_bytes(), before)
        recovered = self.app.start_execution(**args)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual((self.work / "count").read_text(), "1")
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["execution_id"]], reason="Read the original terminal receipt and counter.")
        self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["execution_id"]}], limitations="No observer was involved.")

    def test_live_command_cancel_and_pause_do_not_relaunch(self):
        self.accept_plan()
        args = self.start_arguments('import time; from pathlib import Path; Path("started").write_text("once"); time.sleep(30)')
        result = self.app.start_execution(**args)
        self.addCleanup(lambda: self.app.cancel_execution("desktop", args["execution_id"]))
        self.control("pause", reason="Keep the original command while blocking new work.")
        self.assertNotIn(self.app.read_execution("desktop", args["execution_id"])["status"], TERMINAL)
        cancelled = self.control("cancel", reason="Explicitly terminate only the owned fixture.")
        terminal = self.wait(result)
        self.assertEqual(terminal["status"], "cancelled")
        if cancelled["record"]["terminal"] is None:
            cancelled = self.control("cancel", reason="Read the original cancellation outcome.")
        self.assertEqual(cancelled["record"]["terminal"]["outcome"], "cancelled")
        retained = self.app.read_task("structured-one", section="operations", operation_id=args["execution_id"])
        self.assertEqual([item["id"] for item in retained["items"]], [args["execution_id"]])

    def test_original_cancel_remains_available_with_busy_task_identity(self):
        from openmachine.tasks import records, transitions
        self.accept_plan()
        args = self.start_arguments("import time; time.sleep(30)")
        original = self.app.start_execution(**args)
        self.addCleanup(lambda: self.app.cancel_execution("desktop", args["execution_id"]))
        key = ("execution", "local", args["execution_id"])
        with lease(self.state / "locks" / ("task-effect-" + records.digest(list(key)) + ".lock")):
            self.app.cancel_execution("desktop", args["execution_id"])
        self.assertEqual(self.wait(original)["status"], "cancelled")

    def test_pty_input_lost_task_reply_recovers_without_sending_again(self):
        self.accept_plan()
        code = 'from pathlib import Path; import sys; print("READY",flush=True)\nfor line in sys.stdin:\n p=Path("input-count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print("ACK",flush=True)'
        args = self.start_arguments(code, tty=True)
        self.app.start_execution(**args)
        self.addCleanup(lambda: self.app.cancel_execution("desktop", args["execution_id"]))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            original = self.app.read_execution("desktop", args["execution_id"])
            if original["control_ready"] and "READY" in original["output"]:
                break
            time.sleep(0.02)
        self.assertTrue(original["control_ready"])
        self.assertIn("READY", original["output"])
        self.app.start_execution(**args)
        identity = uuid.uuid4().hex
        input_args = dict(machine="desktop", execution_id=args["execution_id"], expected_control_seq=0,
            action="write_stdin", text="only-once-fixture-input\n", task_context=self.context_for(identity))
        original_commit = database.commit
        def fail(connection):
            row = connection.execute("SELECT payload FROM operations WHERE task_id=? AND id=?",
                                     ("structured-one", identity)).fetchone()
            if row is not None and json.loads(row["payload"])["result"] is not None:
                raise OSError("Lost task publication after terminal input.")
            return original_commit(connection)
        with patch.object(database, "commit", side_effect=fail):
            self.fault("task_result_unverified", self.app.control_execution, **input_args)
        self.assertIsNone(tasks.operation(self.state, "structured-one", identity)["result"])
        deadline = time.monotonic() + 5
        counter = self.work / "input-count"
        while time.monotonic() < deadline and not counter.exists():
            time.sleep(0.02)
        self.assertEqual(counter.read_text(), "1")
        recovered = self.app.control_execution(**input_args)
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual(recovered["control_result"]["seq"], 1)
        self.assertEqual(counter.read_text(), "1")
        retained = [self.app.read_task("structured-one", section=section, limit=50) for section in ("operations", "history")]
        self.assertTrue(all(page["complete"] for page in retained))
        self.assertNotIn("only-once-fixture-input", json.dumps(retained))
        self.fault("task_operation_conflict", self.app.control_execution,
            **dict(input_args, task_context=self.context_for(uuid.uuid4().hex)))
        bare = {key: value for key, value in input_args.items() if key != "task_context"}
        self.fault("task_context_required", self.app.control_execution, **dict(bare, expected_control_seq=1))
        self.app.cancel_execution("desktop", args["execution_id"])
        self.assertEqual(self.wait(original)["status"], "cancelled")
        self.assertEqual(counter.read_text(), "1")

    def test_actual_process_loss_before_and_after_control_publication(self):
        for phase, exit_code in (("before", 17), ("after", 18)):
            state = self.root / ("control-crash-" + phase)
            app = create_application(self.config, state)
            request = task_contract(self.context, "crash-" + phase)
            created = app.create_task(**request)["record"]
            args = dict(task_id=request["task_id"], expected_revision=1, expected_plan_revision=1, expected_epoch=0,
                update_id=uuid.uuid4().hex, transition={"action": "claim", "handoff_id": created["handoff"]["id"]})
            payload = self.root / ("control-crash-" + phase + ".json")
            payload.write_text(json.dumps({"config": self.config.model_dump(mode="json"), "state": str(state), "request": args}))
            code = '''import json,os,sys
from pathlib import Path
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.tasks import database
value=json.loads(Path(sys.argv[1]).read_text())
original=database.commit
def stop(connection):
 if sys.argv[2]=="before": os._exit(17)
 original(connection)
 os._exit(18)
database.commit=stop
create_application(Settings.model_validate(value["config"]),Path(value["state"])).control_task(**value["request"])
'''
            result = subprocess.run([sys.executable, "-I", "-c", code, str(payload), phase], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, exit_code, result.stderr)
            observed = app.read_task(args["task_id"])
            self.assertEqual(observed["record"]["revision"], 1 if phase == "before" else 2)
            self.assertEqual(observed["index_status"], "current")
            self.assertEqual(tasks.control_update(state, args["task_id"], args["update_id"]) is not None,
                             phase == "after")
            original_record = observed["record"]
            recovered = app.control_task(**args)
            self.assertEqual(recovered["record"]["revision"], 2)
            if phase == "after":
                self.assertEqual(recovered["record"], original_record)
                self.assertEqual(recovered["status"], "unchanged")
            self.assertEqual(app.control_task(**args)["record"], recovered["record"])
            self.assertFalse((state / "executions").exists())


if __name__ == "__main__":
    unittest.main()
