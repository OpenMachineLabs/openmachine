"""Tests for app behavior at the component layer."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import subprocess
import threading
import unittest
from unittest.mock import patch
import uuid
from openmachine import tasks
from openmachine.tasks import database, records, store as task_store
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from test.support.tasks import TaskFixture, task_contract



class TaskAdmissionTests(TaskFixture, unittest.TestCase):
    def test_expired_renewal_and_handoff_claim_have_one_winner(self):
        record = self.accept_plan()["record"]
        tasks.update(self.state, "structured-one", lambda value, now:
                     value["control"]["owner"].update(expires_at_ms=record["updated_at_ms"]))
        owner = record["control"]["owner"]
        requests = [self.control_arguments("renew", claim_id=owner["claim_id"]),
                    self.control_arguments("claim", handoff_id=record["handoff"]["id"])]
        barrier = threading.Barrier(2)

        def compete(request):
            barrier.wait(timeout=5)
            try:
                return self.app.control_task(**request)
            except Fault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, requests))
        winners = [value for value in results if isinstance(value, dict)]
        failures = [value for value in results if isinstance(value, str)]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(failures), 1, results)
        self.assertIn(failures[0], {"task_conflict", "resource_busy"})
        current = self.view()["record"]
        self.assertEqual(self.app.read_task("structured-one")["record"], winners[0]["record"])
        self.assertEqual(current["operations"], [])
        if current["control"]["owner"]["claim_id"] != owner["claim_id"]:
            self.fault("task_owner_conflict", self.control, "renew", claim_id=owner["claim_id"])
        else:
            self.fault("task_owner_conflict", self.control, "claim", handoff_id=record["handoff"]["id"])

    def revised_plan(self, *, expires_at_ms=None, authority_ref=None):
        record = self.view()["record"]
        contract = json.loads(json.dumps(record["contract"]))
        if expires_at_ms is not None:
            contract["expires_at_ms"] = expires_at_ms
        if authority_ref is not None:
            contract["authority_refs"].append(authority_ref)
        return dict(contract=contract,
            stages=[{key: value for key, value in stage.items() if key not in {"state", "evidence"}}
                    for stage in record["plan"]["stages"]],
            invalidated_stage_ids=[], reason="Explicitly revise the disposable fixture contract.")

    def retained_local_operation(self, status="running"):
        """Seed an original record for control tests; no worker or effect is created."""
        identity = uuid.uuid4().hex
        def retain(record, now):
            record["operations"].append({"id": identity, "stage_id": "verify",
                "plan_revision": record["plan"]["revision"], "epoch": record["control"]["epoch"],
                "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
                "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
                            "execution_id": identity, "control_seq": None},
                "admitted_at_ms": now, "state": "unknown" if status == "unknown" else "observed",
                "observed_at_ms": now, "result": {"status": status, "receipt_sha256": "c" * 64,
                    "summary": "Retained fixture outcome; no worker was created.", "log_offset": 0}})
            record["control"]["state"] = record["plan"]["stages"][0]["state"] = "running"
            record["handoff"] = None
        tasks.update(self.state, "structured-one", retain)
        return identity

    def test_historical_unknown_can_be_abandoned_and_same_spec_restarted_isolated(self):
        """v0.2.16-like known exit without verified cleanup keeps original uncertainty."""
        self.accept_plan()
        old_id = self.retained_local_operation(status="unknown")
        original = self.view()["record"]["operations"][0].copy()
        legacy = {"status": "outcome_unknown", "execution_id": old_id, "exit_code": 0,
                  "command": {"exited": True}, "cleanup": {"status": "unverified"}}
        with patch.object(self.app.machines, "read_execution", return_value=legacy):
            request = self.control_arguments("abandon", reason="Replace the blocked Spec attempt.")
            abandoned = self.app.control_task(**request)
            repeated = self.app.control_task(**request)
        self.assertEqual(abandoned["record"]["terminal"]["outcome"], "abandoned")
        self.assertEqual(repeated["status"], "unchanged")
        self.assertEqual(tasks.read(self.state, "structured-one", operation_ids=[old_id])["record"]["operations"][0], original)
        self.assertIsNone(self.app.read_task("structured-one")["foreground"]["original_work_running"])
        self.assertEqual(tasks.read(self.state, "structured-one")["counts"]["unknown_count"], 1)
        self.fault("task_terminal", self.control, "resume", reason="A new task is required.")

        separate = self.root / "isolated"
        separate.mkdir()
        self.config.workspaces["isolated"] = type(self.config.workspaces["project"])(
            machine="desktop", root=separate)
        new_context = self.app.read_workspace_file("desktop", "isolated", "AGENTS.md")["context"]
        replacement = task_contract(new_context, "replacement")
        replacement["anchors"] = self.request["anchors"]
        replacement["contract"]["targets"][0]["workspace"] = "isolated"
        replacement["contract"]["tools"] = ["read_task", "write_workspace_file"]
        created = self.create(replacement)["record"]
        next_task = self.app.control_task(task_id="replacement", expected_revision=created["revision"],
            expected_plan_revision=1, expected_epoch=0, update_id=uuid.uuid4().hex,
            transition={"action": "claim", "handoff_id": created["handoff"]["id"]})["record"]
        operation_id = uuid.uuid4().hex
        result = self.app.write_workspace_file(machine="desktop", workspace_name="isolated",
            path="continued.txt", content="new isolated work\\n", expected_sha256="", expected_head_sha="",
            expected_workspace_identity=new_context["workspace_identity"], change_id=operation_id,
            task_context={"task_id": "replacement", "task_revision": next_task["revision"],
                "plan_revision": 1, "continuation_epoch": next_task["control"]["epoch"],
                "operation_id": operation_id})
        self.assertEqual(result["status"], "applied")
        self.assertEqual((separate / "continued.txt").read_text(), "new isolated work\\n")
        self.assertEqual(self.app.read_task("structured-one")["record"]["terminal"]["outcome"], "abandoned")

    def test_unverified_live_execution_cannot_be_force_abandoned(self):
        self.accept_plan()
        self.retained_local_operation(status="unknown")
        old_bytes = self.task_bytes()
        with patch.object(self.app.machines, "read_execution", return_value={
                "status": "outcome_unknown", "command": {"exited": False},
                "cleanup": {"status": "unverified"}}):
            self.fault("task_effect_active", self.control, "abandon",
                       reason="An unseen command must not be declared ended.")
        self.assertEqual(self.task_bytes(), old_bytes)

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
        raw = self.task_bytes()
        with lease(self.state / "locks" / f"{args['execution_id']}.lock", create=False):
            self.app.task_admission.reconcile(self.view())
        self.assertEqual(self.task_bytes(), raw)
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
        original = self.task_bytes()
        self.app.task_admission.reconcile(self.view())
        self.assertEqual(self.task_bytes(), original)
        # An existing partial directory is not absence, even after its lock is free.
        with lease(self.state / "locks" / f"{args['execution_id']}.lock"):
            (self.state / "executions" / args["execution_id"]).mkdir(parents=True)
        self.app.task_admission.reconcile(self.view())
        self.assertEqual(self.task_bytes(), original)

    def test_control_claim_retry_deadline_and_reads_are_passive(self):
        created = self.accept_plan(claim=False)
        raw = self.task_bytes()
        self.view()
        self.app.find_tasks()
        self.assertEqual(self.task_bytes(), raw)
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
        replay = self.app.control_task(**args)
        self.assertEqual(replay["status"], "unchanged")
        self.assertEqual(replay["record"]["control"]["owner"], next_claim["record"]["control"]["owner"])

    def test_strict_controls_and_context_reject_forged_runtime_facts(self):
        self.accept_plan(claim=False)
        raw = self.task_bytes()
        original = self.control_arguments("claim", handoff_id=self.view()["record"]["handoff"]["id"])
        invalid = [dict(original, expected_revision=True), dict(original, expected_epoch=-1),
            dict(original, transition={**original["transition"], "principal_ref": "forged"}),
            dict(original, transition={"action": "unknown"}), dict(original, transition={"action": "pause", "reason": "x" * 513})]
        for args in invalid:
            self.fault("invalid_task_control", self.app.control_task, **args)
        args = self.write_arguments()
        for context in ({**args["task_context"], "principal_ref": "forged"}, {**args["task_context"], "task_revision": True}):
            self.fault("invalid_task_context", self.app.write_workspace_file, **dict(args, task_context=context))
        self.assertEqual(self.task_bytes(), raw)
        self.assertFalse((self.work / "result.txt").exists())

    def test_explicit_renew_recovers_same_expired_claim_without_handoff_or_replay(self):
        self.accept_plan()
        self.retained_local_operation()
        original = self.view()["record"]
        owner = original["control"]["owner"]
        now = owner["expires_at_ms"] + 1
        with patch.object(task_store.time, "time_ns", return_value=now * 1000000), \
                patch.object(self.app.machines, "start_execution") as start, \
                patch.object(self.app.machines, "write_workspace_file") as write:
            self.fault("task_handoff_conflict", self.control, "claim", handoff_id="f" * 32)
            renewed = self.control("renew", claim_id=owner["claim_id"])["record"]
            self.assertEqual(renewed["control"]["owner"], dict(owner, expires_at_ms=now + 300000))
            self.assertEqual(renewed["control"]["epoch"], original["control"]["epoch"])
            self.assertEqual(renewed["plan"], original["plan"])
            self.assertEqual(renewed["operations"], original["operations"])
            self.assertGreater(renewed["_facts"]["history_count"], original["_facts"]["history_count"])
            self.assertIsNone(renewed["handoff"])
            self.fault("task_effect_unresolved", self.app.write_workspace_file, **self.write_arguments())
            self.fault("task_effect_unresolved", self.control, "record_decision", stage_id="verify",
                       outcome="accepted", evidence=[], reason="A claim cannot resolve the original worker.")
            start.assert_not_called()
            write.assert_not_called()
        self.assertEqual(self.view()["record"]["operations"], original["operations"])

    def test_renew_retry_is_passive_and_does_not_extend_expiry_twice(self):
        record = self.accept_plan()["record"]
        args = self.control_arguments("renew", claim_id=record["control"]["owner"]["claim_id"])
        now = record["control"]["owner"]["expires_at_ms"] + 1
        with patch.object(task_store.time, "time_ns", return_value=now * 1000000):
            renewed = self.app.control_task(**args)["record"]
        raw = self.task_bytes()
        with patch.object(task_store.time, "time_ns", return_value=(now + 300001) * 1000000):
            self.view()
            self.app.find_tasks()
            same = self.app.control_task(**args)
            self.assertEqual((same["status"], same["record"]), ("unchanged", renewed))
            altered = dict(args, transition={"action": "renew", "claim_id": "f" * 32})
            self.fault("task_conflict", self.app.control_task, **altered)
        self.assertEqual(self.task_bytes(), raw)

    def test_renew_requires_exact_control_snapshot_and_retained_claim_identity(self):
        record = self.accept_plan()["record"]
        claim = record["control"]["owner"]["claim_id"]
        args = self.control_arguments("renew", claim_id=claim)
        raw = self.task_bytes()
        for key in ("expected_revision", "expected_plan_revision", "expected_epoch"):
            with self.subTest(guard=key):
                self.fault("task_conflict", self.app.control_task, **dict(args, **{key: args[key] + 1}))
        self.fault("task_owner_conflict", self.control, "renew", claim_id="f" * 32)
        self.assertEqual(self.task_bytes(), raw)
        self.control("release", reason="Release ends the original claim.")
        self.fault("task_owner_conflict", self.control, "renew", claim_id=claim)
        successor = self.control("claim", handoff_id=self.view()["record"]["handoff"]["id"])["record"]
        self.assertNotEqual(successor["control"]["owner"]["claim_id"], claim)
        self.fault("task_owner_conflict", self.control, "renew", claim_id=claim)
        stale = self.control_arguments("renew", claim_id=successor["control"]["owner"]["claim_id"])
        self.control("revise_plan", **self.revised_plan())
        self.fault("task_conflict", self.app.control_task, **stale)
        self.fault("task_owner_conflict", self.control, "renew", claim_id=successor["control"]["owner"]["claim_id"])

    def test_renew_control_rejects_an_observation_only_revision_change(self):
        self.accept_plan()
        self.retained_local_operation()
        original = self.view()["record"]
        claim = original["control"]["owner"]["claim_id"]
        request = self.control_arguments("renew", claim_id=claim)
        def observe(value, now):
            operation = value["operations"][0]
            operation["observed_at_ms"] = now
            operation["result"]["log_offset"] = 1
        observed = tasks.update(self.state, "structured-one", observe, observation=True)["record"]
        self.assertGreater(observed["revision"], original["revision"])
        self.assertEqual(observed["control_revision"], original["control_revision"])
        self.assertEqual(observed["plan"], original["plan"])
        self.assertEqual(observed["control"]["epoch"], original["control"]["epoch"])
        raw = self.task_bytes()
        self.fault("task_conflict", self.app.control_task, **request)
        self.assertEqual(self.task_bytes(), raw)
        renewed = self.control("renew", claim_id=claim)["record"]
        self.assertEqual(renewed["operations"], observed["operations"])

    def test_renew_respects_principal_authority_stop_and_current_grants(self):
        record = self.accept_plan()["record"]
        owner = record["control"]["owner"]
        tasks.update(self.state, "structured-one",
            lambda value, now: value["control"]["owner"].update(principal_ref="fixture:another-native-principal"))
        self.fault("task_owner_conflict", self.control, "renew", claim_id=owner["claim_id"])
        tasks.update(self.state, "structured-one",
            lambda value, now: value["control"]["owner"].update(principal_ref=owner["principal_ref"]))
        self.config.machines["desktop"].workspace_write_enabled = False
        self.fault("workspace_write_disabled", self.control, "renew", claim_id=owner["claim_id"])
        self.config.machines["desktop"].workspace_write_enabled = True
        expiry = record["contract"]["expires_at_ms"]
        with patch.object(task_store.time, "time_ns", return_value=(expiry - 1) * 1000000):
            renewed = self.control("renew", claim_id=owner["claim_id"])["record"]
            self.assertEqual(renewed["control"]["owner"]["expires_at_ms"], expiry)
        with patch.object(task_store.time, "time_ns", return_value=expiry * 1000000):
            self.fault("task_authority_expired", self.control, "renew", claim_id=owner["claim_id"])
        # The rejected expired renewal made no write; resume at the last saved time.
        with patch.object(task_store.time, "time_ns", return_value=(expiry - 1) * 1000000):
            self.control("pause", reason="An explicit pause also ends renewal eligibility.")
            self.fault("task_stopped", self.control, "renew", claim_id=owner["claim_id"])
            done = self.control("cancel", reason="No effect exists in this fixture.")["record"]
            self.assertEqual(done["terminal"]["outcome"], "cancelled")
            self.fault("task_terminal", self.control, "renew", claim_id=owner["claim_id"])

    def test_branch_change_allows_ownership_recovery_but_requires_explicit_new_scope(self):
        subprocess.run(["git", "init", "-q", str(self.work)], check=True, capture_output=True)
        self.context = self.app.read_workspace_file("desktop", "project", "AGENTS.md")["context"]
        self.request = task_contract(self.context)
        original = self.accept_plan()["record"]
        subprocess.run(["git", "-C", str(self.work), "checkout", "-q", "-b", "fixture-next"],
                       check=True, capture_output=True)
        current = self.app.read_workspace_file("desktop", "project", "AGENTS.md")["context"]
        self.assertNotEqual(current["workspace_identity"], self.context["workspace_identity"])
        now = original["control"]["owner"]["expires_at_ms"] + 1
        with patch.object(task_store.time, "time_ns", return_value=now * 1000000):
            claimed = self.control("claim", handoff_id=original["handoff"]["id"])["record"]
            renewed = self.control("renew", claim_id=claimed["control"]["owner"]["claim_id"])["record"]
            self.assertEqual(renewed["contract"], original["contract"])
            self.assertEqual(renewed["control"]["epoch"], claimed["control"]["epoch"])
            self.context = current
            self.fault("context_mismatch", self.app.write_workspace_file, **self.write_arguments())
            self.assertEqual(self.view()["record"]["operations"], [])
            revision = self.revised_plan()
            revision["contract"]["targets"] = task_contract(current)["contract"]["targets"]
            revision["invalidated_stage_ids"] = ["verify"]
            revised = self.control("revise_plan", **revision)["record"]
            self.control("claim", handoff_id=revised["handoff"]["id"])
            self.assertEqual(self.app.write_workspace_file(**self.write_arguments())["status"], "applied")
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")

    def test_expired_contract_requires_new_authority_and_retains_accepted_original_evidence(self):
        self.accept_plan()
        write = self.write_arguments()
        self.app.write_workspace_file(**write)
        original = self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[write["change_id"]],
                                reason="Verified the retained file receipt.")["record"]
        now = original["contract"]["expires_at_ms"] + 1
        finish = dict(outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": write["change_id"]}],
                      limitations="Original evidence is retained; no effect was repeated.")
        with patch.object(task_store.time, "time_ns", return_value=now * 1000000):
            self.fault("task_authority_expired", self.control, "renew", claim_id=original["control"]["owner"]["claim_id"])
            self.fault("task_authority_expired", self.control, "claim", handoff_id=original["handoff"]["id"])
            self.fault("task_authority_expired", self.control, "finish", **finish)
            revised = self.control("revise_plan", **self.revised_plan(
                expires_at_ms=now + 60000, authority_ref="fixture:fresh-explicit-authority"))["record"]
            self.assertEqual(revised["plan"]["stages"], original["plan"]["stages"])
            self.assertEqual(revised["operations"], original["operations"])
            self.assertGreater(revised["_facts"]["history_count"], original["_facts"]["history_count"])
            self.assertEqual(revised["format"], original["format"])
            self.assertIsNone(revised["control"]["owner"])
            self.control("claim", handoff_id=revised["handoff"]["id"])
            done = self.control("finish", **finish)["record"]
        self.assertEqual(done["terminal"]["outcome"], "completed")
        self.assertEqual(done["operations"], original["operations"])
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")

    def test_expired_contract_reauthorization_rejects_missing_authority_and_scope(self):
        record = self.accept_plan()["record"]
        now = record["contract"]["expires_at_ms"] + 1
        original = self.task_bytes()
        with patch.object(task_store.time, "time_ns", return_value=now * 1000000):
            missing = self.revised_plan(expires_at_ms=now + 60000)
            self.fault("task_authority_expired", self.control, "revise_plan", **missing)
            past = self.revised_plan(expires_at_ms=now, authority_ref="fixture:fresh-explicit-authority")
            self.fault("task_authority_expired", self.control, "revise_plan", **past)
            wrong_scope = self.revised_plan(expires_at_ms=now + 60000, authority_ref="fixture:fresh-explicit-authority")
            wrong_scope["contract"]["targets"][0]["workspace_identity"] = "0" * 64
            self.fault("context_mismatch", self.control, "revise_plan", **wrong_scope)
            self.config.machines["desktop"].workspace_write_enabled = False
            valid = self.revised_plan(expires_at_ms=now + 60000, authority_ref="fixture:fresh-explicit-authority")
            self.fault("workspace_write_disabled", self.control, "revise_plan", **valid)
        self.assertEqual(self.task_bytes(), original)

    def test_expired_paused_plan_can_be_reauthorized_without_automatic_resume(self):
        record = self.accept_plan()["record"]
        paused = self.control("pause", reason="Preserve the explicit pause across reauthorization.")["record"]
        now = record["contract"]["expires_at_ms"] + 1
        with patch.object(task_store.time, "time_ns", return_value=now * 1000000):
            revised = self.control("revise_plan", **self.revised_plan(
                expires_at_ms=now + 60000, authority_ref="fixture:reauthorize-paused-work"))["record"]
            self.assertEqual((revised["control"]["state"], revised["control"]["stop_intent"]), ("paused", "pause"))
            self.assertEqual(revised["control"]["pending_decision"], paused["control"]["pending_decision"])
            self.assertIsNone(revised["handoff"])
            self.assertIsNone(revised["control"]["owner"])
            self.fault("task_handoff_conflict", self.control, "claim", handoff_id=record["handoff"]["id"])
            resumed = self.control("resume", reason="Resume is a separate explicit control.")["record"]
            self.assertEqual(resumed["control"]["stop_intent"], "none")
            self.assertIsNone(resumed["control"]["owner"])

    def test_local_unknown_and_partial_results_block_expired_plan_reauthorization(self):
        record = self.accept_plan()["record"]
        self.retained_local_operation("unknown")
        now = record["contract"]["expires_at_ms"] + 1
        with patch.object(task_store.time, "time_ns", return_value=now * 1000000):
            for status in ("unknown", "partial"):
                with self.subTest(status=status):
                    if status == "partial":
                        def partial(value, at_ms):
                            operation = value["operations"][0]
                            operation["state"] = "observed"
                            operation["result"]["status"] = "partial"
                            operation["observed_at_ms"] = at_ms
                        tasks.update(self.state, "structured-one", partial)
                    original = self.task_bytes()
                    revision = self.revised_plan(expires_at_ms=now + 60000,
                                                 authority_ref="fixture:cannot-reclassify-original-local-work")
                    self.fault("task_effect_unresolved", self.control, "revise_plan", **revision)
                    self.assertEqual(self.task_bytes(), original)

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
        snapshot = self.task_bytes()
        self.fault("task_terminal", self.control, "resume", reason="Cannot reactivate history.")
        self.view()
        self.assertEqual(self.task_bytes(), snapshot)
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

    def test_uncertain_committed_admission_never_dispatches_or_replays(self):
        self.accept_plan()
        args = self.write_arguments()
        original = tasks.update
        def fail(*values, **options):
            original(*values, **options)
            raise Fault("task_write_unverified", "Injected uncertainty after the admission transaction committed.")
        with patch.object(tasks, "update", side_effect=fail), patch.object(self.app.machines, "write_workspace_file") as dispatch:
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
            dispatch.assert_not_called()
        record = self.view()["record"]
        self.assertEqual(len(record["operations"]), 1)
        self.assertEqual(record["operations"][0]["state"], "admitted")
        self.fault("change_not_found", self.app.write_workspace_file, **args)
        self.assertFalse((self.work / "result.txt").exists())
        self.assertEqual(len(self.view()["record"]["operations"]), 1)

    def test_storage_commit_rejection_prevents_dispatch_and_retains_original_task(self):
        self.accept_plan()
        args = self.write_arguments()
        before = self.task_bytes()
        for code, fault in ((sqlite3.SQLITE_FULL, "task_storage_full"),
                            (sqlite3.SQLITE_READONLY, "task_write_unverified")):
            error = sqlite3.OperationalError("Injected admission commit failure.")
            error.sqlite_errorcode = code
            with self.subTest(sqlite_errorcode=code), patch.object(database, "commit", side_effect=error) as commit, \
                    patch.object(self.app.machines, "write_workspace_file") as dispatch:
                self.fault(fault, self.app.write_workspace_file, **args)
                commit.assert_called_once()
                dispatch.assert_not_called()
            current = tasks.read(self.state, "structured-one")
            self.assertEqual(current["facts"]["operation_count"], 0)
            self.assertIsNone(tasks.operation(self.state, "structured-one", args["change_id"]))
            self.assertEqual(self.task_bytes(), before)
        self.assertFalse((self.work / "result.txt").exists())

    def test_lost_file_result_publication_recovers_original_change(self):
        self.accept_plan()
        args = self.write_arguments()
        original = tasks.update
        def fail(*values, **options):
            if options.get("observation"):
                raise OSError("injected result publication failure")
            return original(*values, **options)
        with patch.object(tasks, "update", side_effect=fail):
            self.fault("task_result_unverified", self.app.write_workspace_file, **args)
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")
        domain = self.app.read_workspace_change("desktop", args["change_id"])
        self.assertEqual(domain["status"], "applied")
        self.app = create_application(self.config, self.state)
        result = self.app.write_workspace_file(**args)
        self.assertFalse(result["task_operation_replayed"])
        self.assertEqual(self.app.read_workspace_change("desktop", args["change_id"])["created_at"], domain["created_at"])
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")

    def test_stale_foreground_result_cannot_replace_a_newer_historical_outcome(self):
        self.accept_plan()
        identity = self.retained_local_operation()
        original = tasks.read(self.state, "structured-one")["record"]["operations"][0]
        expected = {identity: records.digest(original)}
        verified = dict(original["result"], status="succeeded", receipt_sha256="d" * 64)
        self.app.task_admission._publish("structured-one", [(identity, verified, original["locator"])], expected=expected)
        self.retained_local_operation("failed")
        self.assertNotIn(identity, [item["id"] for item in tasks.read(self.state, "structured-one")["record"]["operations"]])
        before = self.task_bytes()
        stale = dict(original["result"], status="failed", receipt_sha256="e" * 64)
        result = self.app.task_admission._publish("structured-one", [(identity, stale, original["locator"])], expected=expected)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(self.task_bytes(), before)
        retained = tasks.read(self.state, "structured-one", operation_ids=[identity])["record"]["operations"]
        self.assertEqual(next(item for item in retained if item["id"] == identity)["result"], verified)

    def test_controls_and_cancel_keep_distinct_ids_for_one_original_execution(self):
        self.accept_plan()
        execution_id = self.retained_local_operation()
        observed = {"execution_id": execution_id, "status": "running", "created_at": 1,
                    "control": {"seq": 0, "last": None}}
        def control(**arguments):
            receipt = {"seq": arguments["expected_control_seq"] + 1, "status": "applied"}
            observed["control"] = {"seq": receipt["seq"], "last": receipt}
            return dict(observed, control_result=receipt)
        def cancel(**arguments):
            observed.update(status="cancelled", cleanup={"status": "verified"})
            return dict(observed)
        requests, operation_ids = [], [execution_id]
        with patch.object(self.app.machines, "read_execution", side_effect=lambda *args, **kwargs: dict(observed)), \
                patch.object(self.app.machines, "control_execution", side_effect=control) as controls, \
                patch.object(self.app.machines, "cancel_execution", side_effect=cancel) as cancellation:
            for sequence in range(2):
                identity = uuid.uuid4().hex
                request = dict(machine="desktop", execution_id=execution_id, expected_control_seq=sequence,
                               action="interrupt", task_context=self.context_for(identity))
                self.app.control_execution(**request)
                requests.append(request)
                operation_ids.append(identity)
            cancel_id = uuid.uuid4().hex
            with patch.object(database.shutil, "disk_usage",
                              return_value=database.shutil.disk_usage(self.state)._replace(free=0)):
                self.app.cancel_execution("desktop", execution_id, task_context=self.context_for(cancel_id))
            operation_ids.append(cancel_id)
            replay = self.app.control_execution(**requests[0])
            self.assertFalse(replay["task_operation_replayed"])
            self.assertEqual(controls.call_count, 2)
            self.assertEqual(cancellation.call_count, 1)
        retained = tasks.read(self.state, "structured-one", operation_ids=operation_ids)["record"]["operations"]
        self.assertEqual([item["id"] for item in retained], operation_ids)
        keys = {tasks.operation_key(item["locator"]) for item in retained}
        self.assertEqual(len(keys), 1)
        self.assertEqual(tasks.operation_binding(self.state, keys.pop())["task_id"], "structured-one")

    def test_failed_admission_commit_preserves_pause_and_original_evidence(self):
        self.accept_plan()
        self.app.write_workspace_file(**self.write_arguments())
        before = self.task_bytes()
        args = self.write_arguments(path="later.txt")
        with patch.object(tasks, "update", side_effect=Fault("task_write_unverified", "Admission transaction failed.")), \
                patch.object(self.app.machines, "write_workspace_file") as dispatch:
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
            dispatch.assert_not_called()
        self.assertEqual(self.task_bytes(), before)
        paused = self.control("pause", reason="Explicit stop remains available after a failed transaction.")
        self.assertEqual(paused["record"]["control"]["stop_intent"], "pause")
        self.assertEqual(tasks.read(self.state, "structured-one")["record"]["operations"][0]["result"]["status"], "succeeded")
        self.assertFalse((self.work / "later.txt").exists())
        self.assertEqual(self.view()["index_status"], "current")

    def test_current_file_drift_blocks_stage_acceptance(self):
        self.accept_plan()
        args = self.write_arguments()
        self.app.write_workspace_file(**args)
        (self.work / "result.txt").write_text("independent later modification\n")
        raw = self.task_bytes()
        self.fault("task_evidence_changed", self.control, "record_decision", stage_id="verify", outcome="accepted",
                   evidence=[args["change_id"]], reason="Stale evidence cannot pass.")
        self.assertEqual(self.task_bytes(), raw)

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
        raw = self.task_bytes()
        changed = {**args, "transition": {"action": "cancel", "reason": "Changed intent."}}
        self.fault("task_conflict", self.app.control_task, **changed)
        self.assertEqual(self.task_bytes(), raw)
        self.assertEqual(self.app.control_task(**args)["status"], "unchanged")

    def test_long_control_history_keeps_new_work_and_stop_available(self):
        owner = self.accept_plan()["record"]["control"]["owner"]
        for _ in range(70):
            self.control("renew", claim_id=owner["claim_id"])
        retained = tasks.read(self.state, "structured-one")["facts"]["history_count"]
        self.assertGreater(retained, 64)
        self.assertEqual(self.app.write_workspace_file(**self.write_arguments())["status"], "applied")
        self.control("release", reason="Retained history does not consume continuation capacity.")
        self.control("pause", reason="Capacity must not prevent stop.")
        done = self.control("cancel", reason="No operations remain to cancel.")
        self.assertEqual(done["record"]["control"]["state"], "cancelled")
        self.assertGreater(tasks.read(self.state, "structured-one")["facts"]["history_count"], retained)

    def test_corrupt_database_binding_lookup_is_nonmutating(self):
        self.accept_plan()
        database = self.state / "tasks.db"
        database.write_bytes(b"x" * (records.MAX_RECORD + 1))
        before = database.stat()
        locator = {"kind": "change", "target_identity": None, "change_id": "a" * 32}
        self.fault("corrupt_tasks", tasks.operation_binding, self.state, tasks.operation_key(locator))
        self.assertEqual(database.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(database.stat().st_size, records.MAX_RECORD + 1)

    def test_supervised_known_identity_cannot_be_stripped_or_rebound(self):
        self.accept_plan()
        args = self.write_arguments()
        original = tasks.update
        def fail(*values, **options):
            original(*values, **options)
            raise Fault("task_write_unverified", "Injected uncertainty after admission committed.")
        with patch.object(tasks, "update", side_effect=fail):
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
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
        retained = tasks.read(self.state, "structured-one", operation_ids=[first["change_id"], second, third])
        self.assertEqual([item["result"]["status"] for item in retained["record"]["operations"]], ["succeeded"] * 3)

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
