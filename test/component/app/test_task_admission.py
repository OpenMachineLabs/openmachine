"""Tests for app behavior at the component layer."""

import hashlib
import json
import os
import unittest
from unittest.mock import patch
import uuid
from openmachine import tasks
from openmachine.tasks import store as task_store
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from test.support.tasks import TaskFixture, task_contract



class TaskAdmissionTests(TaskFixture, unittest.TestCase):
    def test_workspace_request_rejection_is_no_effect_and_corrected_work_continues(self):
        self.accept_plan()
        identity = uuid.uuid4().hex
        args = dict(machine="desktop", workspace_name="project", path="result.txt", edits=[],
                    expected_workspace_identity=self.context["workspace_identity"], expected_head_sha="",
                    expected_sha256="0" * 64, change_id=identity, task_context=self.context_for(identity))
        self.fault("invalid_edits", self.app.edit_workspace_file, **args)
        operation = self.view()["record"]["operations"][0]
        self.assertEqual((operation["state"], operation["result"]["status"], operation["result"]["summary"]),
                         ("observed", "failed", "Domain failure: invalid_edits."))
        self.assertFalse((self.state / "workspace-changes" / identity).exists())
        corrected = self.write_arguments(path="result.txt")
        self.assertEqual(self.app.write_workspace_file(**corrected)["status"], "applied")
        self.assertEqual(self.view()["record"]["operations"][-1]["result"]["status"], "succeeded")

    def test_pre_dispatch_busy_is_failed_and_old_unknown_reconciles(self):
        self.accept_plan()
        args = self.start_arguments()
        key = hashlib.sha256(str(self.work).encode()).hexdigest()
        with lease(self.state / "locks" / f"workspace-{key}.lock"):
            self.fault("resource_busy", self.app.start_execution, **args)
        original = self.view()["record"]["operations"][0]
        self.assertEqual(original["result"]["status"], "failed")
        self.assertFalse((self.state / "executions" / args["execution_id"]).exists())
        def old_bug(value, now):
            operation = value["operations"][0]
            operation["state"], operation["result"]["status"] = "unknown", "unknown"
        tasks.update(self.state, "structured-one", old_bug)
        raw = self.path.read_bytes()
        with lease(self.state / "locks" / f"{args['execution_id']}.lock", create=False):
            self.app.task_admission.reconcile(self.view())
        self.assertEqual(self.path.read_bytes(), raw)
        repaired = self.app.task_admission.reconcile(self.view())["record"]["operations"][0]
        self.assertEqual(repaired["result"]["status"], "failed")
        self.assertEqual(repaired["id"], original["id"])
        self.assertEqual(repaired["request_sha256"], original["request_sha256"])
        self.assertEqual(self.app.write_workspace_file(**self.write_arguments())["status"], "applied")

    def test_missing_receipt_without_pre_dispatch_proof_remains_unknown(self):
        self.accept_plan()
        args = self.start_arguments()
        with patch.object(self.app.machines, "start_execution", side_effect=Fault("resource_busy", "Fixture.")):
            self.fault("resource_busy", self.app.start_execution, **args)
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "unknown")
        original = self.path.read_bytes()
        self.app.task_admission.reconcile(self.view())
        self.assertEqual(self.path.read_bytes(), original)
        # An existing partial directory is not absence, even after its lock is free.
        with lease(self.state / "locks" / f"{args['execution_id']}.lock"):
            (self.state / "executions" / args["execution_id"]).mkdir(parents=True)
        self.app.task_admission.reconcile(self.view())
        self.assertEqual(self.path.read_bytes(), original)

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


if __name__ == "__main__":
    unittest.main()
