"""Installed native workers on independent worktrees and process admission races."""

import json
import subprocess
import sys
import time
import unittest
import uuid

from openmachine import execution, tasks
from openmachine.foundation.sanitize import Fault
from openmachine.workspace.git import context
from test.support.tasks import TaskFixture, task_contract


class NativeWriterReservationTests(TaskFixture, unittest.TestCase):
    def create_at(self, identity, name, root):
        self.config.workspaces[name] = self.config.workspaces["project"].model_copy(update={"root": str(root)})
        observed = context(root)
        request = task_contract(observed, identity)
        request["contract"]["targets"][0]["workspace"] = name
        request["contract"]["tools"] = ["start_execution", "write_workspace_file", "read_task", "control_task"]
        created = self.app.create_task(**request)["record"]
        self.app.control_task(task_id=identity, expected_revision=created["revision"], expected_plan_revision=1,
            expected_epoch=0, update_id=uuid.uuid4().hex, transition={"action":"claim", "handoff_id":created["handoff"]["id"]})
        return observed

    def task_context(self, identity, operation):
        record = tasks.read(self.state, identity)["record"]
        return {"task_id":identity, "task_revision":record["revision"], "plan_revision":record["plan"]["revision"],
                "continuation_epoch":record["control"]["epoch"], "operation_id":operation}

    def test_linked_worktrees_have_real_overlapping_native_workers(self):
        def git(*arguments):
            subprocess.run(["git", "-C", str(self.work), *arguments], check=True, capture_output=True)
        git("init", "-q")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Fixture", "--allow-empty")
        other = self.root / "linked"
        git("worktree", "add", "-qb", "fixture-linked", str(other))
        first = self.create_at("writer-a", "project", self.work)
        second = self.create_at("writer-b", "linked", other)
        self.assertEqual(context(self.work)["common_dir"], context(other)["common_dir"])
        release = self.root / "release"
        code = ("from pathlib import Path\nimport time\nPath('ready').touch()\n"
                f"while not Path({str(release)!r}).exists(): time.sleep(.02)\n")
        receipts = []
        try:
            for identity, root, observed in (("writer-a", self.work, first), ("writer-b", other, second)):
                operation = uuid.uuid4().hex
                receipts.append(self.app.start_execution(machine="desktop", execution_id=operation, cwd=str(root),
                    argv=[sys.executable, "-c", code], timeout_seconds=12, expected_head_sha=observed["head_sha"],
                    task_context=self.task_context(identity, operation)))
            deadline = time.monotonic() + 8
            while not all((root / "ready").exists() for root in (self.work, other)) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(all((root / "ready").exists() for root in (self.work, other)))
            for receipt in receipts:
                self.assertEqual(self.app.read_execution("desktop", receipt["execution_id"])["status"], "running")
        finally:
            release.touch()
            completed = [self.wait(receipt) for receipt in receipts]
        for index, result in enumerate(completed):
            if result["status"] == "outcome_unknown":
                # Prove cleanup against the original native session after a
                # raced process snapshot; never infer success from the exit code.
                self.assertTrue(result["command"]["exited"], result)
                self.assertEqual(result["exit_code"], 0, result)
                self.assertIsNone(result["stop"]["reason"], result)
                self.assertIsNone(result["error"], result)
                completed[index] = execution.reconcile(self.config, self.state, "desktop", result["execution_id"])
        self.assertEqual([result["status"] for result in completed], ["succeeded", "succeeded"], completed)
        self.assertLess(max(result["command"]["started_at"] for result in completed),
                        min(result["command"]["exited_at"] for result in completed))
        for result in completed:
            self.assertEqual(result["cleanup"]["status"], "verified")

    def _held_native(self, root):
        ready = self.root / ("ready-" + root.name)
        release = self.root / ("release-" + root.name)
        code = ("from pathlib import Path\nimport time\n"
                f"Path({str(ready)!r}).touch()\n"
                f"while not Path({str(release)!r}).exists(): time.sleep(.02)\n")
        started = self.app.start_execution(machine="desktop", execution_id=uuid.uuid4().hex,
            cwd=str(root), argv=[sys.executable, "-c", code], timeout_seconds=15)
        self.addCleanup(release.touch, exist_ok=True)
        deadline = time.monotonic() + 9
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(ready.exists(), "Original native writer did not start")
        return started, release

    def _finish_held(self, started, release):
        release.touch()
        result = self.wait(started)
        if result["status"] == "outcome_unknown":
            self.assertTrue(result["command"]["exited"], result)
            self.assertEqual(result["exit_code"], 0, result)
            result = execution.reconcile(self.config, self.state, "desktop", started["execution_id"])
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["cleanup"]["status"], "verified", result)

    def test_active_non_git_parent_blocks_git_task_first_write_without_effect(self):
        parent = self.root / "outside-parent"
        repo = parent / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        observed = self.create_at("git-task", "nested-git", repo)
        started, release = self._held_native(parent)
        try:
            operation = uuid.uuid4().hex
            args = dict(machine="desktop", workspace_name="nested-git", path="blocked.txt",
                content="not allowed", expected_head_sha=observed["head_sha"] or "",
                expected_sha256="", expected_workspace_identity=observed["workspace_identity"],
                change_id=operation, task_context=self.task_context("git-task", operation))
            with self.assertRaises(Fault) as blocked:
                self.app.write_workspace_file(**args)
            self.assertEqual(blocked.exception.code, "resource_busy")
            self.assertEqual(blocked.exception.effect, "known_no_effect")
            self.assertFalse((repo / "blocked.txt").exists())
            self.assertEqual(tasks.read(self.state, "git-task")["facts"]["operation_count"], 0)
        finally:
            self._finish_held(started, release)
        operation = uuid.uuid4().hex
        args.update(path="owned.txt", content="retained", change_id=operation,
                    task_context=self.task_context("git-task", operation))
        self.assertEqual(self.app.write_workspace_file(**args)["status"], "applied")
        self.assertEqual((repo / "owned.txt").read_text(), "retained")
        bare = uuid.uuid4().hex
        with self.assertRaises(Fault) as caught:
            self.app.start_execution(machine="desktop", execution_id=bare, cwd=str(parent),
                argv=[sys.executable, "-c", "print('should not start')"])
        self.assertEqual(caught.exception.code, "task_resource_conflict")
        self.assertFalse((self.state / "executions" / bare).exists())

    def test_active_git_worker_blocks_non_git_parent_task_first_write(self):
        parent = self.root / "bare-parent"
        repo = parent / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        observed = self.create_at("parent-task", "bare-parent", parent)
        started, release = self._held_native(repo)
        try:
            operation = uuid.uuid4().hex
            args = dict(machine="desktop", workspace_name="bare-parent", path="blocked.txt",
                content="not allowed", expected_head_sha="", expected_sha256="",
                expected_workspace_identity=observed["workspace_identity"],
                change_id=operation, task_context=self.task_context("parent-task", operation))
            with self.assertRaises(Fault) as blocked:
                self.app.write_workspace_file(**args)
            self.assertEqual(blocked.exception.code, "resource_busy")
            self.assertEqual(blocked.exception.effect, "known_no_effect")
            self.assertFalse((parent / "blocked.txt").exists())
            self.assertEqual(tasks.read(self.state, "parent-task")["facts"]["operation_count"], 0)
        finally:
            self._finish_held(started, release)
        operation = uuid.uuid4().hex
        args.update(path="owned.txt", content="retained", change_id=operation,
                    task_context=self.task_context("parent-task", operation))
        self.assertEqual(self.app.write_workspace_file(**args)["status"], "applied")

    def test_unrelated_non_git_worker_does_not_serialize_git_task(self):
        bare = self.root / "unrelated-bare"
        repo = self.root / "unrelated-git"
        bare.mkdir()
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        observed = self.create_at("independent-git", "independent-worktree", repo)
        started, release = self._held_native(bare)
        try:
            operation = uuid.uuid4().hex
            result = self.app.write_workspace_file(machine="desktop", workspace_name="independent-worktree",
                path="independent.txt", content="independent", expected_head_sha=observed["head_sha"] or "",
                expected_sha256="", expected_workspace_identity=observed["workspace_identity"],
                change_id=operation, task_context=self.task_context("independent-git", operation))
            self.assertEqual(result["status"], "applied")
            self.assertEqual(self.app.read_execution("desktop", started["execution_id"])["status"], "running")
            self.assertEqual((repo / "independent.txt").read_text(), "independent")
        finally:
            self._finish_held(started, release)

    def test_two_native_processes_race_one_target_without_duplicate_effect(self):
        observed = self.create_at("race-a", "project", self.work)
        self.create_at("race-b", "project", self.work)
        gate = self.root / "go"
        script = '''import json,sys,time
from pathlib import Path
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
request=json.loads(sys.argv[1]); app=create_application(Settings.model_validate(request['config']),Path(request['state']))
Path(request['ready']).touch()
end=time.monotonic()+8
while not Path(request['gate']).exists():
 if time.monotonic()>end: raise RuntimeError('Fixture barrier timeout')
 time.sleep(.01)
try:
 result=app.write_workspace_file(**request['arguments']); print(json.dumps({'status':result['status']}))
except Fault as error:
 print(json.dumps({'error':error.code,'effect':error.effect}))
finally: app.task_observer.stop()
'''
        processes = []
        arguments = []
        try:
            for identity in ("race-a", "race-b"):
                operation = uuid.uuid4().hex
                args = dict(machine="desktop", workspace_name="project", path="winner.txt", content=identity,
                    expected_head_sha="", expected_sha256="", expected_workspace_identity=observed["workspace_identity"],
                    change_id=operation, task_context=self.task_context(identity, operation))
                arguments.append(args)
                payload = dict(config=self.config.model_dump(),state=str(self.state),ready=str(self.root / identity),
                               gate=str(gate),arguments=args)
                processes.append(subprocess.Popen([sys.executable,"-I","-c",script,json.dumps(payload)],
                    cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            deadline = time.monotonic()+8
            while not all((self.root / name).exists() for name in ("race-a","race-b")) and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(all((self.root / name).exists() for name in ("race-a","race-b")))
            gate.touch()
            replies = []
            for process in processes:
                out, err = process.communicate(timeout=12)
                self.assertEqual(process.returncode,0,err)
                replies.append(json.loads(out))
        finally:
            gate.touch(exist_ok=True)
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
        self.assertEqual(sum(reply.get("status")=="applied" for reply in replies),1,replies)
        loser = next(reply for reply in replies if "error" in reply)
        self.assertIn(loser["error"], {"task_resource_conflict","resource_busy"})
        self.assertEqual(loser["effect"],"known_no_effect")
        self.assertIn((self.work / "winner.txt").read_text(), {"race-a","race-b"})
        retained = [tasks.read(self.state,name)["facts"]["operation_count"] for name in ("race-a","race-b")]
        self.assertEqual(sorted(retained),[0,1])
        for args, reply in zip(arguments,replies,strict=True):
            if "error" in reply:
                self.assertFalse((self.state / "workspace-changes" / args["change_id"]).exists())
