"""Optional worktree creation retains execution ownership, guards and recovery."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from openmachine import execution
from openmachine.config import Workspace
from openmachine.execution import api
from openmachine.execution.records import TERMINAL
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from openmachine.workspace.git import context
from test.support.execution_cases import ExecutionCaseFixture


class WorktreeExecutionTests(ExecutionCaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.git("init", "--quiet", "-b", "main")
        (self.work / "source.txt").write_text("original\n")
        self.git("add", "source.txt")
        self.commit("Initial")
        self.source_context = context(self.work)
        self.destinations = self.root / "checkouts"
        self.destinations.mkdir()
        self.config.workspaces["checkouts"] = Workspace(machine="local", root=str(self.destinations))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.work), *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self, message):
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                 "commit", "--allow-empty", "--quiet", "-m", message)

    def arguments(self, **updates):
        return {"workspace_name": "project", "destination": str(self.destinations / "feature"),
                "base_ref": "main", "new_branch": "feature/isolated",
                "expected_workspace_identity": self.source_context["workspace_identity"], **updates}

    def create(self, *, execution_id=None, **updates):
        identity = execution_id or uuid.uuid4().hex
        self.ids.append(identity)
        return execution.create_worktree(self.config, self.state, "local", execution_id=identity,
                                         **self.arguments(**updates))

    def receipt_path(self, identity):
        return self.state / "executions" / identity / "record.json"

    def mutate_before_worker(self, action):
        original = subprocess.Popen

        def launch(argv, *args, **kwargs):
            if len(argv) > 3 and argv[1:4] == ["-I", "-m", "openmachine.execution"]:
                action()
            return original(argv, *args, **kwargs)

        return patch.object(api.subprocess, "Popen", side_effect=launch)

    def test_busy_dirty_source_can_create_without_changing_ordinary_exclusion(self):
        (self.work / "source.txt").write_text("current uncommitted work\n")
        (self.work / "untracked.txt").write_text("keep me")
        original_config = self.config.model_dump()
        busy = self.start('import time; print("source-owned", flush=True); time.sleep(3)')
        self.until(busy["execution_id"], lambda item: "source-owned" in item["output"])
        with self.assertRaises(Fault) as caught:
            self.start('print("must remain excluded")')
        self.assertEqual(caught.exception.code, "resource_busy")
        created = self.finish(self.create())
        self.assertEqual(created["status"], "succeeded")
        self.assertEqual(created["format_version"], execution.CREATION_FORMAT)
        selected = created["context"]["created_worktree"]
        self.assertEqual(selected["head_sha"], self.source_context["head_sha"])
        self.assertEqual(selected["branch"], "feature/isolated")
        self.assertEqual((Path(selected["worktree"]) / "source.txt").read_text(), "original\n")
        self.assertFalse((Path(selected["worktree"]) / "untracked.txt").exists())
        self.assertEqual((self.work / "source.txt").read_text(), "current uncommitted work\n")
        self.assertEqual((self.work / "untracked.txt").read_text(), "keep me")
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(self.config.model_dump(), original_config)
        execution.cancel(self.config, self.state, "local", busy["execution_id"])
        self.finish(busy)

    def test_original_id_replays_after_ref_change_and_source_disappearance_in_fresh_process(self):
        created = self.finish(self.create())
        identity = created["execution_id"]
        original = self.receipt_path(identity).read_bytes()
        self.commit("Later source commit")
        moved = self.root / "moved-source"
        self.work.rename(moved)
        try:
            code = ('import json,sys; from pathlib import Path; from openmachine import execution; '
                    'from openmachine.config import Settings; request=json.load(sys.stdin); '
                    'result=execution.create_worktree(Settings.model_validate(request["config"]), '
                    'Path(request["state"]), "local", **request["arguments"]); '
                    'print(json.dumps({"status":result["status"],"execution_id":result["execution_id"]}))')
            result = subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True, text=True,
                                    input=json.dumps({"config": self.config.model_dump(mode="json"),
                                                      "state": str(self.state),
                                                      "arguments": self.arguments(execution_id=identity)}))
            self.assertEqual(json.loads(result.stdout), {"status": "succeeded", "execution_id": identity})
            with self.assertRaises(Fault) as caught:
                self.create(execution_id=identity, new_branch="different")
            self.assertEqual(caught.exception.code, "execution_conflict")
            self.assertIsNone(caught.exception.effect)
        finally:
            moved.rename(self.work)
        self.assertEqual(self.receipt_path(identity).read_bytes(), original)
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 2)

    def test_disappearing_original_receipt_is_never_reclassified_as_no_effect(self):
        result = self.finish(self.create())
        directory = self.receipt_path(result["execution_id"]).parent
        preserved = self.root / "preserved-receipt"

        def disappear(_):
            directory.rename(preserved)
            raise Fault("corrupt_state", "The original receipt disappeared during observation.")

        try:
            with patch.object(api, "read_record", side_effect=disappear), self.assertRaises(Fault) as caught:
                self.create(execution_id=result["execution_id"])
            self.assertNotEqual(caught.exception.effect, "known_no_effect")
            self.assertTrue(Path(self.arguments()["destination"]).is_dir())
        finally:
            if preserved.exists():
                preserved.rename(directory)

    def test_postdispatch_missing_receipt_is_never_reclassified_as_no_effect(self):
        identity = uuid.uuid4().hex
        directory = self.receipt_path(identity).parent
        preserved = self.root / "preserved-receipt"

        def disappear(*_args, **_kwargs):
            self.until(identity, lambda item: item["status"] in TERMINAL)
            directory.rename(preserved)
            raise Fault("execution_not_found", "The dispatched receipt is no longer available.")

        try:
            with patch.object(api, "read", side_effect=disappear), self.assertRaises(Fault) as caught:
                self.create(execution_id=identity)
            self.assertNotEqual(caught.exception.effect, "known_no_effect")
            self.assertTrue(Path(self.arguments()["destination"]).is_dir())
        finally:
            if preserved.exists():
                preserved.rename(directory)

    def test_source_head_can_advance_before_launch_but_destination_keeps_pinned_commit(self):
        with self.mutate_before_worker(lambda: self.commit("Concurrent source progress")):
            created = self.finish(self.create(new_branch=""))
        self.assertEqual(created["status"], "succeeded")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.source_context["head_sha"])
        self.assertEqual(created["context"]["created_worktree"]["head_sha"], self.source_context["head_sha"])
        self.assertIsNone(created["context"]["created_worktree"]["branch"])

    def test_destination_and_common_git_locks_reject_before_admission(self):
        private_directory(self.state)
        locks = private_directory(self.state / "locks")
        destination = self.arguments()["destination"]
        common = self.source_context["common_dir"]
        resource = "git-common:" + hashlib.sha256(common.encode()).hexdigest()
        names = ["workspace-" + hashlib.sha256(destination.encode()).hexdigest(),
                 "resource-" + hashlib.sha256(resource.encode()).hexdigest()]
        for name in names:
            with self.subTest(lease=name), lease(locks / (name + ".lock")):
                identity = uuid.uuid4().hex
                with self.assertRaises(Fault) as caught:
                    self.create(execution_id=identity)
                self.assertEqual(caught.exception.code, "resource_busy")
                self.assertEqual(caught.exception.effect, "known_no_effect")
                self.assertFalse(self.receipt_path(identity).parent.exists())
                self.assertFalse(Path(destination).exists())

    def test_two_ids_cannot_create_the_same_destination_and_branch(self):
        def attempt(_):
            try:
                return self.create()
            except Fault as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, range(2)))
        accepted = [item for item in outcomes if isinstance(item, dict)]
        rejected = [item for item in outcomes if isinstance(item, Fault)]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIn(rejected[0].code, {"resource_busy", "destination_exists", "branch_exists"})
        self.assertEqual(rejected[0].effect, "known_no_effect")
        self.assertEqual(self.finish(accepted[0])["status"], "succeeded")
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 2)

    def test_prelaunch_destination_race_preserves_new_contents_and_original_failed_receipt(self):
        destination = Path(self.arguments()["destination"])

        def occupy():
            destination.mkdir()
            (destination / "owned-elsewhere").write_text("preserve")

        with self.mutate_before_worker(occupy):
            result = self.finish(self.create())
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["command"]["spawned"])
        self.assertEqual(result["error"], "destination_exists")
        self.assertEqual((destination / "owned-elsewhere").read_text(), "preserve")
        before = self.receipt_path(result["execution_id"]).read_bytes()
        self.assertEqual(self.create(execution_id=result["execution_id"])["status"], "failed")
        self.assertEqual(self.receipt_path(result["execution_id"]).read_bytes(), before)

    def test_replaced_destination_parent_prevents_launch(self):
        previous = self.root / "previous-parent"

        def replace_parent():
            self.destinations.rename(previous)
            self.destinations.mkdir()

        with self.mutate_before_worker(replace_parent):
            result = self.finish(self.create())
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["command"]["spawned"])
        self.assertEqual(result["error"], "context_mismatch")
        self.assertFalse(Path(self.arguments()["destination"]).exists())

    def test_cancel_before_launch_uses_original_execution_control(self):
        setup = ('import time\noriginal=w.worktrees.validate_creation\n'
                 'def delayed(plan):\n    time.sleep(0.3)\n    original(plan)\n'
                 'w.worktrees.validate_creation=delayed')
        with self.injected_worker(setup):
            pending = self.create()
        self.until(pending["execution_id"], lambda item: item["status"] == "preparing")
        execution.cancel(self.config, self.state, "local", pending["execution_id"])
        result = self.finish(pending)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["command"]["spawned"])
        self.assertEqual(result["cleanup"]["status"], "verified")
        self.assertFalse(Path(self.arguments()["destination"]).exists())

    def test_git_environment_hooks_and_filters_cannot_redirect_creation_or_execute(self):
        marker = self.root / "external-code-ran"
        hooks = self.work / ".git" / "hooks"
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\nprintf executed > '" + str(marker) + "'\n")
        hook.chmod(0o700)
        (self.work / ".gitattributes").write_text("*.txt filter=fixture\n")
        self.git("add", ".gitattributes")
        self.commit("Filter attributes")
        self.git("config", "filter.fixture.smudge", "printf filtered > '" + str(marker) + "'; cat")
        self.git("config", "filter.fixture.required", "true")
        foreign = self.root / "foreign"
        foreign.mkdir()
        subprocess.run(["git", "init", "--quiet", str(foreign)], check=True)
        with patch.dict("os.environ", {"GIT_DIR": str(foreign / ".git"),
                                       "GIT_WORK_TREE": str(foreign), "GIT_INDEX_FILE": str(foreign / "foreign-index"),
                                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                                       "GIT_CONFIG_VALUE_0": str(hooks)}):
            result = self.finish(self.create())
            ordinary = self.finish(self.start('import os; print(os.environ["GIT_WORK_TREE"])'))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(ordinary["output"].strip(), str(foreign))
        self.assertEqual(ordinary["format_version"], execution.FORMAT)
        self.assertFalse(marker.exists())
        self.assertFalse((foreign / "foreign-index").exists())
        self.assertEqual(result["context"]["created_worktree"]["common_dir"], self.source_context["common_dir"])
        self.assertEqual((Path(self.arguments()["destination"]) / "source.txt").read_text(), "original\n")

    def test_unverified_creation_retains_worktree_and_never_relaunches(self):
        setup = ('from openmachine.foundation.sanitize import Fault\n'
                 'def unavailable(plan):\n    raise Fault("context_mismatch", "Injected observation loss")\n'
                 'w.worktrees.verify_creation=unavailable')
        with self.injected_worker(setup):
            result = self.finish(self.create())
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["error"], "context_mismatch")
        self.assertEqual(result["cleanup"]["status"], "verified")
        self.assertTrue(Path(self.arguments()["destination"]).is_dir())
        before = self.receipt_path(result["execution_id"]).read_bytes()
        with patch.object(api.subprocess, "Popen") as launcher:
            self.assertEqual(self.create(execution_id=result["execution_id"])["status"], "outcome_unknown")
        launcher.assert_not_called()
        self.assertEqual(self.receipt_path(result["execution_id"]).read_bytes(), before)
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 2)

    def test_typed_receipt_rejects_changed_argv_and_downgraded_semantics(self):
        result = self.finish(self.create())
        path = self.receipt_path(result["execution_id"])
        original = path.read_bytes()
        for tamper in ("argv", "format"):
            with self.subTest(tamper=tamper):
                record = json.loads(original)
                if tamper == "argv":
                    record["request"]["argv"] = [sys.executable, "-c", "print('unexpected')"]
                    record["context"]["creation_plan"]["argv"] = record["request"]["argv"]
                else:
                    record["format_version"] = execution.FORMAT
                path.write_text(json.dumps(record))
                try:
                    with self.assertRaises(Fault) as caught:
                        self.create(execution_id=result["execution_id"])
                    self.assertEqual(caught.exception.code, "corrupt_state")
                finally:
                    path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
