"""Behavior checks for TaskAdmissionTests."""

import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from openmachine import tasks
from openmachine.execution.records import TERMINAL
from openmachine.tasks import store as task_store
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from support_tasks import TaskFixture, task_contract, terminal_task_fixture


class TaskAdmissionTests(TaskFixture, unittest.TestCase):
    def test_control_claim_retry_deadline_and_reads_are_passive(self):
        created = self.accept_plan(claim=False)
        raw = self.path.read_bytes()
        self.view()
        self.app.find_tasks()
        self.assertEqual(self.path.read_bytes(), raw)
        args = self.control_arguments("claim", handoff_id=created["record"]["handoff"]["id"])
        claimed = self.app.control_task(**args)
        owner = claimed["record"]["control"]["owner"]
        self.assertEqual(owner["principal_ref"], f"native-uid:{os.getuid()}")
        self.assertLessEqual(owner["expires_at_ms"], claimed["record"]["updated_at_ms"] + 300000)
        with patch.object(task_store.time, "time_ns", return_value=(owner["expires_at_ms"] + 1) * 1000000):
            same = self.app.control_task(**args)
            self.assertEqual(same["status"], "unchanged")
            self.assertEqual(same["record"], claimed["record"])
            next_claim = self.control("claim", handoff_id=created["record"]["handoff"]["id"])
        self.assertEqual(next_claim["record"]["control"]["epoch"], 2)
        self.assertEqual(next_claim["record"]["handoff"]["id"], created["record"]["handoff"]["id"])
        self.fault("task_conflict", self.app.control_task, **args)

    def test_strict_controls_and_context_reject_forged_runtime_facts(self):
        self.accept_plan(claim=False)
        raw = self.path.read_bytes()
        original = self.control_arguments("claim", handoff_id=self.view()["record"]["handoff"]["id"])
        invalid = [dict(original, expected_revision=True), dict(original, expected_epoch=-1),
            dict(original, transition={**original["transition"], "principal_ref": "forged"}),
            dict(original, transition={"action": "unknown"}), dict(original, transition={"action": "pause", "reason": "x" * 513})]
        for args in invalid:
            self.fault("invalid_task_control", self.app.control_task, **args)
        args = self.write_arguments()
        for context in ({**args["task_context"], "principal_ref": "forged"}, {**args["task_context"], "task_revision": True}):
            self.fault("invalid_task_context", self.app.write_workspace_file, **dict(args, task_context=context))
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse((self.work / "result.txt").exists())

    def test_file_admission_original_retry_conflict_and_real_completion(self):
        self.accept_plan()
        args = self.write_arguments()
        first = self.app.write_workspace_file(**args)
        self.assertEqual(first["status"], "applied")
        record = self.view()["record"]
        self.assertEqual(len(record["operations"]), 1)
        self.assertEqual(record["operations"][0]["result"]["status"], "succeeded")
        self.assertEqual(record["handoff"]["reason"], "result_ready")
        same = self.app.write_workspace_file(**args)
        self.assertFalse(same["task_operation_replayed"])
        self.assertEqual(self.view()["record"], record)
        self.fault("task_operation_conflict", self.app.write_workspace_file, **dict(args, content="different"))
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["change_id"]], reason="Verified the exact file result.")
        finished = self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["change_id"]}], limitations="Fixture only.")
        self.assertEqual(finished["record"]["terminal"]["outcome"], "completed")
        snapshot = self.path.read_bytes()
        self.fault("task_terminal", self.control, "resume", reason="Cannot reactivate history.")
        self.view()
        self.assertEqual(self.path.read_bytes(), snapshot)
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")

    def test_original_execution_finishes_once_without_read_mutations(self):
        self.accept_plan()
        args = self.start_arguments('from pathlib import Path; p=Path("count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")')
        result = self.app.start_execution(**args)
        terminal = self.wait(result)
        self.assertEqual(terminal["status"], "succeeded")
        before = self.path.read_bytes()
        self.app.read_execution("desktop", args["execution_id"])
        self.view()
        self.assertEqual(self.path.read_bytes(), before)
        recovered = self.app.start_execution(**args)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual((self.work / "count").read_text(), "1")
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["execution_id"]], reason="Read the original terminal receipt and counter.")
        self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["execution_id"]}], limitations="No observer was involved.")

    def test_expired_claim_reclaims_final_handoff_without_repeating_accepted_work(self):
        self.accept_plan()
        args = self.write_arguments()
        self.app.write_workspace_file(**args)
        accepted = self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["change_id"]],
                                reason="Verified original file and receipt.")["record"]
        expired = accepted["control"]["owner"]["expires_at_ms"] + 1
        finish = dict(outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["change_id"]}],
                      limitations="Local evidence; clock advanced only to the original claim deadline.")
        self.app = create_application(self.config, self.state)
        with patch.object(task_store.time, "time_ns", return_value=expired * 1000000):
            self.assertEqual(self.view()["record"], accepted)
            self.fault("task_owner_conflict", self.control, "finish", **finish)
            reclaimed = self.control("claim", handoff_id=accepted["handoff"]["id"])["record"]
            self.assertEqual(reclaimed["plan"], accepted["plan"])
            self.assertEqual(reclaimed["operations"], accepted["operations"])
            self.assertEqual(reclaimed["handoff"], accepted["handoff"])
            self.assertEqual(reclaimed["control"]["epoch"], accepted["control"]["epoch"] + 1)
            done = self.control("finish", **finish)["record"]
        self.assertEqual(done["terminal"]["outcome"], "completed")
        self.assertIsNone(done["handoff"])
        self.assertEqual(done["operations"], accepted["operations"])
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")

    def test_stale_revision_plan_epoch_and_pause_reject_before_effect(self):
        self.accept_plan()
        args = self.write_arguments()
        for key in ("task_revision", "plan_revision", "continuation_epoch"):
            context = dict(args["task_context"])
            context[key] += 1
            self.fault("task_conflict", self.app.write_workspace_file, **dict(args, task_context=context))
        self.control("pause", reason="Explicit fixture pause.")
        self.fault("task_conflict", self.app.write_workspace_file, **args)
        self.fault("task_stopped", self.app.write_workspace_file, **dict(args, task_context=self.context_for(args["change_id"])))
        self.assertFalse((self.work / "result.txt").exists())
        restarted = create_application(self.config, self.state)
        self.assertEqual(restarted.read_task("structured-one")["record"]["control"]["stop_intent"], "pause")
        resumed = self.control("resume", reason="Explicitly resume the fixture.")
        self.assertIsNone(resumed["record"]["control"]["owner"])
        self.fault("task_owner_conflict", self.app.write_workspace_file, **dict(args, task_context=self.context_for(args["change_id"])))

    def test_pause_can_commit_while_admitted_domain_call_is_waiting(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        self.accept_plan()
        entered, proceed = threading.Event(), threading.Event()
        args = self.write_arguments()
        original = self.app.machines.write_workspace_file
        def delayed(**values):
            with lease(self.state / "locks/tasks.lock", create=False):
                pass
            entered.set()
            if not proceed.wait(5):
                raise AssertionError("Pause failed to release the domain fixture.")
            return original(**values)
        with patch.object(self.app.machines, "write_workspace_file", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.app.write_workspace_file, **args)
            self.assertTrue(entered.wait(5))
            try:
                paused = self.control("pause", reason="Pause after intent admission, before the existing file effect.")
                self.assertEqual(paused["record"]["control"]["state"], "paused")
            finally:
                proceed.set()
            self.assertEqual(future.result(10)["status"], "applied")
        record = self.view()["record"]
        self.assertEqual(record["control"]["state"], "paused")
        self.assertEqual(record["operations"][0]["result"]["status"], "succeeded")
        self.assertIsNone(record["handoff"])
        self.fault("task_stopped", self.app.write_workspace_file, **self.write_arguments(path="later.txt"))
        self.assertFalse((self.work / "later.txt").exists())

    def test_two_claimants_and_two_intents_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        self.accept_plan(claim=False)
        args = self.control_arguments("claim", handoff_id=self.view()["record"]["handoff"]["id"])
        other = dict(args, update_id=uuid.uuid4().hex)
        barrier = threading.Barrier(2)
        def claim(values):
            barrier.wait(5)
            try:
                return self.app.control_task(**values)["status"]
            except Fault as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [future.result(10) for future in [pool.submit(claim, args), pool.submit(claim, other)]]
        self.assertEqual(outcomes.count("saved"), 1)
        self.assertTrue(set(outcomes) <= {"saved", "resource_busy", "task_conflict"})
        first, second = self.write_arguments(path="one.txt"), self.write_arguments(path="two.txt")
        barrier = threading.Barrier(2)
        def write(values):
            barrier.wait(5)
            try:
                return self.app.write_workspace_file(**values)["status"]
            except Fault as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [future.result(10) for future in [pool.submit(write, first), pool.submit(write, second)]]
        self.assertEqual(outcomes.count("applied"), 1)
        self.assertEqual(sum((self.work / name).exists() for name in ("one.txt", "two.txt")), 1)
        self.assertEqual(len(self.view()["record"]["operations"]), 1)

    def test_uncertain_admission_fsync_never_dispatches_or_replays(self):
        self.accept_plan()
        args = self.write_arguments()
        original = task_store._sync_directory
        def fail(directory):
            if directory == self.path.parent:
                raise OSError("injected directory sync uncertainty")
            return original(directory)
        with patch.object(task_store, "_sync_directory", side_effect=fail), patch.object(self.app.machines, "write_workspace_file") as dispatch:
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
            dispatch.assert_not_called()
        record = self.view()["record"]
        self.assertEqual(len(record["operations"]), 1)
        self.assertEqual(record["operations"][0]["state"], "admitted")
        self.fault("change_not_found", self.app.write_workspace_file, **args)
        self.assertFalse((self.work / "result.txt").exists())
        self.assertEqual(len(self.view()["record"]["operations"]), 1)

    def test_lost_file_result_publication_recovers_original_change(self):
        self.accept_plan()
        args = self.write_arguments()
        original = task_store._replace
        def fail(path, raw):
            value = json.loads(raw)
            if path == self.path and value["operations"] and value["operations"][0]["result"] is not None:
                raise OSError("injected result publication failure")
            return original(path, raw)
        with patch.object(task_store, "_replace", side_effect=fail):
            self.fault("task_result_unverified", self.app.write_workspace_file, **args)
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")
        domain = self.app.read_workspace_change("desktop", args["change_id"])
        self.assertEqual(domain["status"], "applied")
        self.app = create_application(self.config, self.state)
        result = self.app.write_workspace_file(**args)
        self.assertFalse(result["task_operation_replayed"])
        self.assertEqual(self.app.read_workspace_change("desktop", args["change_id"])["created_at"], domain["created_at"])
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")

    def test_stale_index_preserves_pause_and_original_evidence(self):
        self.accept_plan()
        args = self.write_arguments()
        with patch.object(task_store, "_write_index", side_effect=OSError("index unavailable")), \
                patch.object(self.app.machines, "write_workspace_file") as dispatch:
            self.fault("task_index_incomplete", self.app.write_workspace_file, **args)
            dispatch.assert_not_called()
        self.assertEqual(self.view()["index_status"], "stale")
        paused = self.control("pause", reason="Stop remains writable with stale derived metadata.")
        self.assertEqual(paused["record"]["control"]["stop_intent"], "pause")
        self.assertEqual(paused["record"]["operations"][0]["result"]["status"], "failed")
        self.assertFalse((self.work / "result.txt").exists())
        self.app.rebuild_task_index()
        self.assertEqual(self.view()["index_status"], "current")

    def test_current_file_drift_blocks_stage_acceptance(self):
        self.accept_plan()
        args = self.write_arguments()
        self.app.write_workspace_file(**args)
        (self.work / "result.txt").write_text("independent later modification\n")
        raw = self.path.read_bytes()
        self.fault("task_evidence_changed", self.control, "record_decision", stage_id="verify", outcome="accepted",
                   evidence=[args["change_id"]], reason="Stale evidence cannot pass.")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_plan_change_invalidates_dependents_not_independent_acceptance(self):
        from openmachine.tasks import records, transitions
        sources = [{"locator": "fixture:a", "sha256": "a" * 64}, {"locator": "fixture:b", "sha256": "b" * 64}]
        self.request["contract"]["sources"] = sources
        base = self.request["stages"][0]
        self.request["stages"] = [dict(base, id="independent", source_refs=["fixture:b"], source_sha256=records.digest([sources[1]])),
            dict(base, id="changed", source_refs=["fixture:a"], source_sha256=records.digest([sources[0]])),
            dict(base, id="dependent", depends_on=["changed"])]
        self.accept_plan()
        self.control("record_decision", stage_id="independent", outcome="accepted", evidence=[], reason="Explicit non-command design decision.")
        changed = json.loads(json.dumps(self.request))
        changed["contract"]["sources"][0]["sha256"] = "c" * 64
        changed["stages"][1]["source_sha256"] = records.digest([changed["contract"]["sources"][0]])
        values = dict(contract=changed["contract"], stages=changed["stages"], invalidated_stage_ids=["changed"], reason="Source changed.")
        self.fault("task_plan_conflict", self.control, "revise_plan", **values)
        values["invalidated_stage_ids"].append("dependent")
        revised = self.control("revise_plan", **values)["record"]
        self.assertEqual([stage["state"] for stage in revised["plan"]["stages"]], ["accepted", "invalidated", "invalidated"])
        self.assertEqual(revised["plan"]["revision"], 2)
        self.assertIsNone(revised["control"]["owner"])
        self.control("claim", handoff_id=revised["handoff"]["id"])
        values["stages"] = values["stages"][1:]
        self.fault("task_plan_conflict", self.control, "revise_plan", **values)

    def test_finish_requires_accepted_stages_not_zero_exit_or_claim(self):
        self.accept_plan()
        self.fault("task_evidence_invalid", self.control, "finish", outcome="completed", acceptance=[], limitations="No evidence.")
        self.fault("task_evidence_invalid", self.control, "record_decision", stage_id="verify", outcome="accepted",
                   evidence=["a" * 32], reason="Cannot invent an operation.")
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[], reason="Explicit non-command decision.")
        done = self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": None}], limitations="No command was required.")
        self.assertEqual(done["record"]["control"]["state"], "completed")

    def test_control_same_update_id_different_intent_is_rejected(self):
        self.accept_plan()
        args = self.control_arguments("pause", reason="First pause.")
        self.app.control_task(**args)
        raw = self.path.read_bytes()
        changed = {**args, "transition": {"action": "cancel", "reason": "Changed intent."}}
        self.fault("task_conflict", self.app.control_task, **changed)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.app.control_task(**args)["status"], "unchanged")

    def test_capacity_reserves_stop_without_deleting_history(self):
        from openmachine.tasks import records, transitions
        self.accept_plan()
        def fill(value, now):
            while len(value["history"]) < 60:
                transitions.material(value, now, "decision", "Retained material decision.")
        tasks.update(self.state, "structured-one", fill)
        raw = self.path.read_bytes()
        self.fault("task_capacity", self.control, "release", reason="Cannot consume terminal reserve.")
        self.assertEqual(self.path.read_bytes(), raw)
        self.control("pause", reason="Capacity must not prevent stop.")
        done = self.control("cancel", reason="No operations remain to cancel.")
        self.assertEqual(done["record"]["control"]["state"], "cancelled")
        self.assertLessEqual(len(done["record"]["history"]), 64)
        self.assertEqual(done["record"]["history"][:60], json.loads(raw)["history"])

    def test_oversized_record_binding_lookup_is_bounded_and_nonmutating(self):
        self.accept_plan()
        self.path.write_bytes(b"x" * (tasks.MAX_INDEX + 1))
        before = self.path.stat()
        locator = {"kind": "change", "target_identity": None, "change_id": "a" * 32}
        self.fault("corrupt_tasks", tasks.operation_binding, self.state, tasks.operation_key(locator))
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.path.stat().st_size, tasks.MAX_INDEX + 1)

    def test_supervised_known_identity_cannot_be_stripped_or_rebound(self):
        self.accept_plan()
        args = self.write_arguments()
        with patch.object(task_store, "_sync_directory", side_effect=OSError("uncertain admission")):
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
        self.app.rebuild_task_index()
        bare = {key: value for key, value in args.items() if key != "task_context"}
        self.fault("task_context_required", self.app.write_workspace_file, **bare)
        other = task_contract(self.context, "other")
        other["contract"]["tools"] = self.request["contract"]["tools"]
        created = self.app.create_task(**other)["record"]
        claimed = self.app.control_task("other", 1, 1, 0, uuid.uuid4().hex,
            {"action": "claim", "handoff_id": created["handoff"]["id"]})["record"]
        context = dict(task_id="other", task_revision=claimed["revision"], plan_revision=1,
                       continuation_epoch=claimed["control"]["epoch"], operation_id=args["change_id"])
        self.fault("task_operation_conflict", self.app.write_workspace_file, **dict(args, task_context=context))
        self.assertFalse((self.work / "result.txt").exists())

    def test_scope_grant_revocation_and_cwd_escape_fail_before_intent(self):
        self.accept_plan()
        args = self.write_arguments()
        self.config.machines["desktop"].workspace_write_enabled = False
        self.fault("workspace_write_disabled", self.app.write_workspace_file, **args)
        self.config.machines["desktop"].workspace_write_enabled = True
        outside = self.root / "outside"
        outside.mkdir()
        (self.work / "escape").symlink_to(outside, target_is_directory=True)
        start = self.start_arguments()
        self.fault("path_denied", self.app.start_execution, **dict(start, cwd=str(self.work / "escape")))
        self.assertEqual(self.view()["record"]["operations"], [])

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
        self.assertEqual(len([item for item in cancelled["record"]["operations"] if item["id"] == args["execution_id"]]), 1)

    def test_all_three_file_mutations_use_original_domain_receipts(self):
        self.accept_plan()
        first = self.write_arguments()
        self.app.write_workspace_file(**first)
        second = uuid.uuid4().hex
        result = self.app.edit_workspace_file("desktop", "project", "result.txt", [{"old_text": "original", "new_text": "edited"}],
            self.context["workspace_identity"], "", hashlib.sha256(b"original\n").hexdigest(), second,
            task_context=self.context_for(second))
        self.assertEqual(result["status"], "applied")
        third = uuid.uuid4().hex
        result = self.app.apply_workspace_patch("desktop", "project", "*** Begin Patch\n*** Add File: other.txt\n+patched\n*** End Patch\n",
            self.context["workspace_identity"], "", [{"path": "other.txt", "expected_sha256": ""}], change_id=third,
            task_context=self.context_for(third))
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.work / "result.txt").read_text(), "edited\n")
        self.assertEqual((self.work / "other.txt").read_text(), "patched\n")
        self.assertEqual([item["result"]["status"] for item in self.view()["record"]["operations"]], ["succeeded"] * 3)

    def test_ambiguous_busy_after_dispatch_is_not_proof_of_no_effect(self):
        self.accept_plan()
        args = self.write_arguments()
        with patch.object(self.app.machines, "write_workspace_file", side_effect=Fault("resource_busy", "Uncertain domain observation.")):
            self.fault("resource_busy", self.app.write_workspace_file, **args)
        record = self.view()["record"]
        self.assertEqual(record["operations"][0]["state"], "unknown")
        self.assertEqual(record["operations"][0]["result"]["status"], "unknown")
        self.fault("task_effect_unresolved", self.app.write_workspace_file, **self.write_arguments(path="second.txt"))
        self.assertFalse((self.work / "second.txt").exists())

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

    def test_provider_unknown_write_keeps_original_identity_and_never_resubmits(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        provider = SimpleNamespace(read=Mock(return_value={"configured_write": True, "data": {"permissions": {"push": True}}}),
            issue=Mock(return_value={"status": "write_outcome_unknown"}), reconcile=Mock())
        self.app.github = provider
        self.request["contract"]["repositories"] = ["Owner/repo"]
        self.accept_plan(tools=["github_issue", "read_task", "control_task"])
        identity = uuid.uuid4().hex
        args = dict(action="create", repository="Owner/repo", title="Publication fixture", body="Do not copy this body into task history.",
                    task_context=self.context_for(identity))
        result = self.app.github_issue(**args)
        self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertIn("<!-- om-task:structured-one:" + identity + " -->", provider.issue.call_args.kwargs["body"])
        self.assertNotIn(args["body"], self.path.read_text())
        provider.reconcile.return_value = {"status": "verified", "receipt": {"id": 71, "number": 7, "updated_at": "fixture"}}
        recovered = self.app.github_issue(**args)
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual(provider.issue.call_count, 1)
        self.assertEqual(self.view()["record"]["operations"][0]["locator"]["number"], 7)
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")
        self.fault("task_operation_conflict", self.app.github_issue, **dict(args, title="Different request"))
        self.assertEqual(provider.issue.call_count, 1)

    def test_bodyless_provider_update_cannot_strip_known_pending_context(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        provider = SimpleNamespace(read=Mock(return_value={"configured_write": True, "data": {"permissions": {"push": True}}}),
            issue=Mock(return_value={"status": "write_outcome_unknown"}), reconcile=Mock(return_value={"status": "write_outcome_unknown"}))
        self.app.github = provider
        self.request["contract"]["repositories"] = ["Owner/repo"]
        self.accept_plan(tools=["github_issue", "read_task", "control_task"])
        identity = uuid.uuid4().hex
        args = dict(action="update", repository="Owner/repo", title="New title", number=7,
                    task_context=self.context_for(identity))
        self.app.github_issue(**args)
        self.assertEqual(self.view()["record"]["operations"][0]["state"], "unknown")
        bare = {key: value for key, value in args.items() if key != "task_context"}
        self.fault("task_context_required", self.app.github_issue, **bare)
        self.assertEqual(provider.issue.call_count, 1)
        recovered = self.app.github_issue(**args)
        self.assertEqual(recovered["status"], "write_outcome_unknown")
        self.assertEqual(provider.issue.call_count, 1)

    def test_all_provider_entrypoints_reject_stale_context_before_provider_access(self):
        self.accept_plan()
        context = self.context_for(uuid.uuid4().hex)
        self.control("pause", reason="Reject all old provider continuations.")
        cases = [(self.app.github_issue, dict(action="create", repository="Owner/repo", title="fixture", body="fixture")),
            (self.app.github_pull_request, dict(action="create", repository="Owner/repo", title="fixture", body="fixture", head="topic", base="main")),
            (self.app.github_comment, dict(action="create", repository="Owner/repo", body="fixture", number=1)),
            (self.app.github_review, dict(action="create", repository="Owner/repo", body="fixture", number=1, expected_head_sha="a" * 40)),
            (self.app.github_merge, dict(repository="Owner/repo", number=1, expected_head_sha="a" * 40))]
        with patch.object(self.app, "_provider", side_effect=AssertionError("Stale request reached provider.")) as provider:
            for method, arguments in cases:
                self.fault("task_conflict", method, **arguments, task_context=context)
            provider.assert_not_called()
        self.assertEqual(self.view()["record"]["operations"], [])

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
        original_replace = task_store._replace
        def fail(path, raw):
            value = json.loads(raw)
            if path == self.path and len(value["operations"]) == 2 and value["operations"][1]["result"] is not None:
                raise OSError("Lost task publication after terminal input.")
            return original_replace(path, raw)
        with patch.object(task_store, "_replace", side_effect=fail):
            self.fault("task_result_unverified", self.app.control_execution, **input_args)
        deadline = time.monotonic() + 5
        counter = self.work / "input-count"
        while time.monotonic() < deadline and not counter.exists():
            time.sleep(0.02)
        self.assertEqual(counter.read_text(), "1")
        recovered = self.app.control_execution(**input_args)
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual(recovered["control_result"]["seq"], 1)
        self.assertEqual(counter.read_text(), "1")
        self.assertNotIn("only-once-fixture-input", self.path.read_text())
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
from openmachine.tasks import store
value=json.loads(Path(sys.argv[1]).read_text())
original=store._replace
def stop(path,raw):
 if path.name=="task.json" and sys.argv[2]=="before": os._exit(17)
 original(path,raw)
 if path.name=="task.json": os._exit(18)
store._replace=stop
create_application(Settings.model_validate(value["config"]),Path(value["state"])).control_task(**value["request"])
'''
            result = subprocess.run([sys.executable, "-I", "-c", code, str(payload), phase], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, exit_code, result.stderr)
            observed = app.read_task(args["task_id"])
            self.assertEqual(observed["record"]["revision"], 1 if phase == "before" else 2)
            if phase == "after":
                self.assertEqual(observed["index_status"], "stale")
            original_record = observed["record"]
            recovered = app.control_task(**args)
            self.assertEqual(recovered["record"]["revision"], 2)
            if phase == "after":
                self.assertEqual(recovered["record"], original_record)
                self.assertEqual(recovered["status"], "unchanged")
            self.assertEqual(app.control_task(**args)["record"], recovered["record"])
            self.assertFalse((state / "executions").exists())
