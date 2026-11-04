"""Task writer lifetime, bare-route fences and transactional rollback."""

from copy import deepcopy
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.tasks import database
from openmachine.workspace.git import writable_target
from test.support.tasks import TaskFixture, task_contract


class WriterReservationTests(TaskFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.accept_plan()

    def rows(self):
        with database.connect(self.state) as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM writer_reservations ORDER BY task_id,root")]

    def other(self, identity="other", root=None):
        name = "project"
        if root is not None:
            name = identity
            self.config.workspaces[name] = self.config.workspaces["project"].model_copy(update={"root": str(root)})
        observed = self.app.read_workspace_file("desktop", name, "AGENTS.md")["context"]
        request = task_contract(observed, identity)
        request["contract"]["tools"] = self.request["contract"]["tools"]
        request["contract"]["targets"][0]["workspace"] = name
        created = self.app.create_task(**request)["record"]
        claimed = self.app.control_task(task_id=identity, expected_revision=created["revision"],
            expected_plan_revision=1, expected_epoch=0, update_id=uuid.uuid4().hex,
            transition={"action": "claim", "handoff_id": created["handoff"]["id"]})["record"]
        operation = uuid.uuid4().hex
        return dict(machine="desktop", workspace_name=name, path="other.txt", content="other",
            expected_head_sha="", expected_sha256="", expected_workspace_identity=observed["workspace_identity"],
            change_id=operation, task_context=dict(task_id=identity, task_revision=claimed["revision"],
                plan_revision=1, continuation_epoch=claimed["control"]["epoch"], operation_id=operation))

    def denied(self, operation, *args, **kwargs):
        before = self.rows()
        with self.assertRaises(Fault) as caught:
            operation(*args, **kwargs)
        error = caught.exception
        self.assertEqual(error.code, "task_resource_conflict")
        self.assertEqual(error.effect, "known_no_effect")
        self.assertEqual(error.evidence["blocking_task_ids"], ["structured-one"])
        self.assertEqual(self.rows(), before)
        return error

    def test_creation_claim_and_reads_do_not_reserve(self):
        self.other()
        self.app.read_workspace_file("desktop", "project", "AGENTS.md")
        self.assertEqual(self.rows(), [])

    def test_first_write_excludes_foreign_task_and_reuses_same_owner(self):
        self.app.write_workspace_file(**self.write_arguments())
        other = self.other()
        before = tasks.read(self.state, "other")["record"]
        error = self.denied(self.app.write_workspace_file, **other)
        self.assertEqual(error.evidence["canonical_root"], str(self.work))
        self.assertEqual(error.evidence["reason"], "active_reservation")
        self.assertEqual(tasks.read(self.state, "other")["record"], before)
        self.assertFalse((self.state / "workspace-changes" / other["change_id"]).exists())
        self.assertFalse((self.work / "other.txt").exists())
        self.app.write_workspace_file(**self.write_arguments(path="next.txt"))
        self.assertEqual(len(self.rows()), 1)

    def test_bare_file_routes_shared_shell_and_plain_task_id_are_fenced(self):
        self.app.write_workspace_file(**self.write_arguments())
        write = self.write_arguments(path="bare.txt")
        write.pop("task_context")
        self.denied(self.app.write_workspace_file, **write)
        self.denied(self.app.write_workspace_file, **dict(write, change_id=""))
        read = self.app.read_workspace_file("desktop", "project", "result.txt")
        edit = dict(machine="desktop", workspace_name="project", path="result.txt", edits=[{"old_text":"original", "new_text":"changed"}],
                    expected_head_sha="", expected_sha256=read["sha256"], expected_workspace_identity=self.context["workspace_identity"])
        self.assertEqual(self.app.edit_workspace_file(**edit, dry_run=True)["status"], "preview")
        self.denied(self.app.edit_workspace_file, **edit, change_id=uuid.uuid4().hex)
        self.denied(self.app.apply_workspace_patch, machine="desktop", workspace_name="project",
                    patch="*** Begin Patch\n*** Add File: bare.txt\n+bad\n*** End Patch\n",
                    expected_head_sha="", expected_workspace_identity=self.context["workspace_identity"],
                    preconditions=[{"path":"bare.txt", "expected_sha256":""}], change_id=uuid.uuid4().hex)
        for shared in ("exclusive", "shared"):
            args = self.start_arguments(workspace_access=shared)
            args.pop("task_context")
            self.denied(self.app.start_execution, **args)
            self.assertFalse((self.state / "executions" / args["execution_id"]).exists())
        # Supplying a structured Task ID alone retains the existing authority error.
        args = self.start_arguments(task_id="structured-one")
        args.pop("task_context")
        self.fault("supervised_admission_unavailable", self.app.start_execution, **args)
        self.assertFalse((self.work / "bare.txt").exists())
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")

    def test_original_result_is_observation_after_pause_and_restart(self):
        args = self.write_arguments()
        self.app.write_workspace_file(**args)
        self.control("pause", reason="Fixture pause")
        restarted = create_application(self.config, self.state)
        replay = restarted.write_workspace_file(**args)
        self.assertFalse(replay["task_operation_replayed"])
        self.assertEqual(len(self.rows()), 1)
        self.denied(restarted.write_workspace_file, **self.other())

    def test_release_claim_and_lawful_successor_keep_task_ownership(self):
        self.app.write_workspace_file(**self.write_arguments())
        before = self.rows()
        stale = self.write_arguments(path="stale.txt")
        released = self.control("release", reason="Eligible handoff")
        self.assertEqual(self.rows(), before)
        self.control("claim", handoff_id=released["record"]["handoff"]["id"])
        self.fault("task_conflict", self.app.write_workspace_file, **stale)
        self.app.write_workspace_file(**self.write_arguments(path="successor.txt"))
        self.assertEqual(self.rows(), before)
        self.denied(self.app.write_workspace_file, **self.other())

    def test_non_git_ancestors_and_machine_aliases_overlap_but_siblings_progress(self):
        self.app.write_workspace_file(**self.write_arguments())
        nested = self.work / "nested"
        nested.mkdir()
        self.denied(self.app.write_workspace_file, **self.other("nested", nested))
        sibling = self.root / "sibling"
        sibling.mkdir()
        self.assertEqual(self.app.write_workspace_file(**self.other("sibling", sibling))["status"], "applied")
        self.config.machines["alias"] = self.config.machines["desktop"].model_copy()
        self.denied(self.app.write_workspace_file, machine="alias", workspace_name=str(self.work),
                    path="alias.txt", content="bad", expected_head_sha="", expected_sha256="")
        self.assertFalse((self.work / "alias.txt").exists())

    def test_failed_commit_rolls_back_reservation_and_original_admission(self):
        args = self.write_arguments()
        before = self.task_bytes()
        with patch.object(database, "commit", side_effect=sqlite3.OperationalError("fixture commit failure")):
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.task_bytes(), before)
        self.assertFalse((self.work / "result.txt").exists())
        self.assertFalse((self.state / "workspace-changes" / args["change_id"]).exists())

    def test_lost_admission_reply_never_replays_original_effect(self):
        args = self.write_arguments()
        commit = database.commit
        def lose(connection):
            commit(connection)
            raise sqlite3.OperationalError("fixture lost acknowledgement")
        with patch.object(database, "commit", side_effect=lose):
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
        self.assertEqual(len(self.rows()), 1)
        self.fault("change_not_found", self.app.write_workspace_file, **args)
        self.assertFalse((self.work / "result.txt").exists())
        self.denied(self.app.write_workspace_file, **self.other())

    def test_unknown_and_abandon_never_release(self):
        args = self.write_arguments()
        with patch.object(self.app.machines, "write_workspace_file", side_effect=Fault("fixture_unknown", "Lost outcome")):
            self.fault("fixture_unknown", self.app.write_workspace_file, **args)
        self.fault("change_not_found", self.control, "abandon", reason="Missing receipt cannot prove terminal work")
        self.assertEqual(len(self.rows()), 1)
        self.denied(self.app.write_workspace_file, **self.other())

    def test_safe_cancel_releases_atomically(self):
        self.app.write_workspace_file(**self.write_arguments())
        self.control("cancel", reason="Settled fixture cancellation")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.app.write_workspace_file(**self.other())["status"], "applied")

    def test_final_guard_rechecks_replaced_root(self):
        args = self.write_arguments()
        dispatch = self.app.machines.write_workspace_file
        def replace(**values):
            self.work.rename(self.root / "retained-original")
            self.work.mkdir()
            return dispatch(**values)
        with patch.object(self.app.machines, "write_workspace_file", side_effect=replace):
            self.fault("context_mismatch", self.app.write_workspace_file, **args)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse((self.work / "result.txt").exists())

    def test_partial_publication_abandon_preserves_reservation(self):
        from openmachine.workspace import changes
        original = changes.publish_file
        def partial(config, machine, workspace, source, desired, temporary, **values):
            if source["path"] == "second.txt":
                return {"stage":"failed_no_effect", "cleanup":"not_created", "error":"fixture_failure"}
            return original(config, machine, workspace, source, desired, temporary, **values)
        operation = uuid.uuid4().hex
        with patch.object(changes,"publish_file",side_effect=partial):
            result = self.app.apply_workspace_patch(machine="desktop",workspace_name="project",
                patch="*** Begin Patch\n*** Add File: first.txt\n+first\n*** Add File: second.txt\n+second\n*** End Patch\n",
                expected_workspace_identity=self.context["workspace_identity"],expected_head_sha="",
                preconditions=[{"path":"first.txt","expected_sha256":""},{"path":"second.txt","expected_sha256":""}],
                change_id=operation,task_context=self.context_for(operation))
        self.assertEqual(result["status"],"partial")
        self.assertTrue((self.work / "first.txt").exists())
        self.assertFalse((self.work / "second.txt").exists())
        self.control("abandon",reason="Retire partially applied fixture without releasing it")
        self.assertEqual(len(self.rows()),1)
        self.denied(self.app.write_workspace_file,**self.other())

    def test_safe_plan_target_removal_releases_and_failed_revision_retains(self):
        self.app.write_workspace_file(**self.write_arguments())
        record = self.view()["record"]
        contract = deepcopy(record["contract"])
        contract["targets"] = []
        contract["tools"] = ["read_task", "control_task"]
        stages = [{key:value for key,value in stage.items() if key not in {"state","evidence"}}
                  for stage in record["plan"]["stages"]]
        self.fault("task_plan_conflict",self.control,"revise_plan",contract=contract,stages=stages,
                   invalidated_stage_ids=[],reason="Missing required explicit invalidation")
        self.assertEqual(len(self.rows()),1)
        self.control("revise_plan",contract=contract,stages=stages,
                     invalidated_stage_ids=["verify"],reason="Drop settled local target")
        self.assertEqual(self.rows(),[])
        self.assertEqual(self.app.write_workspace_file(**self.other())["status"],"applied")

    def test_expired_retained_claim_keeps_owner_and_only_lawful_renewal_continues(self):
        from openmachine.tasks import store
        self.app.write_workspace_file(**self.write_arguments())
        record = self.view()["record"]
        owner = record["control"]["owner"]
        before = self.rows()
        now = owner["expires_at_ms"] + 1
        with patch.object(store.time,"time_ns",return_value=now*1000000):
            self.fault("task_owner_conflict",self.control,"renew",claim_id="0"*32)
            self.assertEqual(self.rows(),before)
            self.control("renew",claim_id=owner["claim_id"])
            self.app.write_workspace_file(**self.write_arguments(path="renewed.txt"))
        self.assertEqual(self.rows(),before)
        self.denied(self.app.write_workspace_file,**self.other())

    def test_same_physical_directory_rename_does_not_release_ownership(self):
        self.app.write_workspace_file(**self.write_arguments())
        before = self.rows()
        moved = self.root / "renamed-identical-directory"
        self.work.rename(moved)
        context = self.app.read_workspace_file("desktop", str(moved), "AGENTS.md")["context"]
        contract = deepcopy(self.view()["record"]["contract"])
        contract["targets"] = [{"machine": "desktop", "workspace": str(moved),
                                "workspace_identity": context["workspace_identity"], "head_sha": None}]
        self.control("revise_plan", contract=contract, stages=self.request["stages"],
                     invalidated_stage_ids=["verify"], reason="Retain the same verified physical directory under its new name")
        self.assertEqual(self.rows(), before)
        self.assertEqual((moved.stat().st_dev, moved.stat().st_ino),
                         (before[0]["device"], before[0]["inode"]))
        with self.assertRaises(Fault) as caught:
            self.app.write_workspace_file("desktop", str(moved), "foreign-after-rename.txt", "bad", "", "")
        self.assertEqual(caught.exception.code, "task_resource_conflict")
        self.assertFalse((moved / "foreign-after-rename.txt").exists())
        handoff = self.view()["record"]["handoff"]
        self.assertIsNotNone(handoff)
        self.control("claim", handoff_id=handoff["id"])
        operation = uuid.uuid4().hex
        result = self.app.write_workspace_file(machine="desktop", workspace_name=str(moved),
            path="lawful-continuation.txt", content="continued", expected_head_sha="",
            expected_sha256="", expected_workspace_identity=context["workspace_identity"],
            change_id=operation, task_context=self.context_for(operation))
        self.assertEqual(result["status"], "applied")
        self.assertEqual((moved / "lawful-continuation.txt").read_text(), "continued")
        self.assertEqual(self.rows(), before)

    def test_legacy_checkpoint_id_does_not_bypass_another_task(self):
        self.app.write_workspace_file(**self.write_arguments())
        tasks.save(self.state,"legacy",0,"Fixture legacy","No write authority",[])
        args = self.start_arguments(task_id="legacy",workspace_access="shared")
        args.pop("task_context")
        self.denied(self.app.start_execution,**args)
        self.assertFalse((self.state / "executions" / args["execution_id"]).exists())

    def test_contextual_edit_acquires_first_reservation(self):
        (self.work / "source.txt").write_text("before")
        source = self.app.read_workspace_file("desktop","project","source.txt")
        operation = uuid.uuid4().hex
        result = self.app.edit_workspace_file(machine="desktop",workspace_name="project",path="source.txt",
            edits=[{"old_text":"before","new_text":"after"}],expected_sha256=source["sha256"],expected_head_sha="",
            expected_workspace_identity=self.context["workspace_identity"],change_id=operation,
            task_context=self.context_for(operation))
        self.assertEqual(result["status"],"applied")
        self.assertEqual(len(self.rows()),1)
        self.denied(self.app.write_workspace_file,**self.other())

    def test_git_nested_and_absolute_aliases_share_the_top_root(self):
        import subprocess
        root = self.root / "git"
        root.mkdir()
        subprocess.run(["git","-C",str(root),"init","-q"],check=True,capture_output=True)
        args = self.other("git-owner",root)
        self.app.write_workspace_file(**args)
        nested = root / "nested"
        nested.mkdir()
        second = self.other("nested-git",nested)
        with self.assertRaises(Fault) as caught:
            self.app.write_workspace_file(**second)
        self.assertEqual(caught.exception.code,"task_resource_conflict")
        self.assertEqual(caught.exception.evidence["canonical_root"],str(root))
        with self.assertRaises(Fault) as caught:
            self.app.write_workspace_file("desktop",str(nested),"bare.txt","bad","","")
        self.assertEqual(caught.exception.code,"task_resource_conflict")
        self.assertFalse((nested / "bare.txt").exists())
        self.assertFalse((nested / "other.txt").exists())

    def test_bare_final_guard_closes_admission_gap_under_workspace_lease(self):
        import hashlib
        from openmachine.foundation.state import lease
        check = tasks.check_writer
        calls = 0
        def interleave(state,target,identity=None,**values):
            nonlocal calls
            calls += 1
            if calls == 1:
                check(state,target,identity,**values)
                self.app.write_workspace_file(**self.write_arguments())
                return
            if identity is None:
                with self.assertRaises(Fault) as held:
                    with lease(self.state / "locks" / ("workspace-"+hashlib.sha256(b"/").hexdigest()+".lock")):
                        pass
                self.assertEqual(held.exception.code,"resource_busy")
            return check(state,target,identity,**values)
        args = self.write_arguments(path="bare-race.txt")
        args.pop("task_context")
        with patch.object(tasks,"check_writer",side_effect=interleave), self.assertRaises(Fault) as caught:
            self.app.write_workspace_file(**args)
        self.assertEqual(caught.exception.code,"task_resource_conflict")
        self.assertEqual(caught.exception.effect,"known_no_effect")
        self.assertFalse((self.work / "bare-race.txt").exists())

    def test_restored_authority_fences_bare_mutations(self):
        # A backup watermark cannot prove that a different target stayed free.
        self.app.write_workspace_file(**self.write_arguments())
        with database.connect(self.state, write=True) as connection:
            connection.execute("UPDATE store SET restore_required=1")
        other = self.root / "restored-other"
        other.mkdir()
        self.fault("task_restore_unreconciled", self.app.write_workspace_file,
                   "desktop", str(other), "must-not-exist.txt", "bad", "", "")
        self.assertFalse((other / "must-not-exist.txt").exists())
        source = self.app.read_workspace_file("desktop", "project", "result.txt")
        self.assertEqual(source["text"], "original\n")
