"""Tests for app behavior at the component layer."""

from types import SimpleNamespace
import copy
import unittest
from unittest.mock import Mock, patch
import uuid
from integrations import github
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.tasks import records
from test.support.tasks import TaskFixture
from test.support.goal_recovery import GoalRecoveryFixture
from test.support.github_cases import BASE, HEAD, MERGE, REPO, ROOT, TREE, GitHubCaseFixture, settings


class TaskAdmissionTests(TaskFixture, unittest.TestCase):
    def test_release_create_retains_legacy_arguments_used_by_task_fingerprint(self):
        with patch.object(self.app.task_admission, "run", return_value={"status": "fixture"}) as run:
            self.app.github_release("create", REPO, "v1.2.3", body="Notes")
            self.assertEqual(run.call_args.args[1], {
                "action": "create", "repository": REPO, "tag_name": "v1.2.3",
                "name": "", "body": "Notes", "draft": True, "prerelease": False})
            self.app.github_release("publish", REPO, "v1.2.3", release_id=91,
                                    expected_body_sha256="a" * 64)
            self.assertEqual(run.call_args.args[1], {
                "action": "publish", "repository": REPO, "tag_name": "v1.2.3",
                "name": "", "body": "", "draft": True, "prerelease": False,
                "release_id": 91, "expected_body_sha256": "a" * 64})

    def test_lost_merge_response_is_reconciled_after_restart_without_original_arguments(self):
        fixture = GitHubCaseFixture()
        fixture.setUp()
        fixture.metadata["permissions"] = {"push": True}
        self.config = self.config.model_copy(update={"github": settings().github})
        self.app = create_application(self.config, self.state)
        binding = {"connection_id": "d" * 32, "account_id": 1, "authorization_revision": 1}
        identity = uuid.uuid4().hex
        title, body = "Original merge title", "Original private merge body"

        def response(method, endpoint, payload=None):
            if method == "PUT":
                retained = tasks.read(self.state, "structured-one", operation_ids=[identity])["record"]["operations"][0]
                self.assertEqual(retained["locator"]["merge"]["head_sha"], HEAD)
                self.assertEqual(retained["locator"]["merge"]["base_ref"], "main")
                self.assertNotIn(body, records.canonical(retained).decode())
                fixture.pr.update(state="closed", merged=True, merge_commit_sha=MERGE)
                raise Fault("github_response_unknown", "Synthetic lost response after merge.")
            values = {f"{ROOT}/commits/{MERGE}": {"sha": MERGE, "commit": {"message": title + "\n\n" + body}},
                      f"{ROOT}/branches/main": {"commit": {"sha": TREE}},
                      f"{ROOT}/compare/{MERGE}...{TREE}": {"status": "ahead", "merge_base_commit": {"sha": MERGE}}}
            return copy.deepcopy(values[endpoint]) if endpoint in values else fixture.response(method, endpoint, payload)

        transport = Mock(side_effect=response)
        def connect(app):
            app.github = github
            app.connections.select = Mock(return_value=SimpleNamespace(binding=lambda: binding))
            app.connections.request = Mock(return_value=transport)
        connect(self.app)
        self.request["contract"].update(targets=[], repositories=[REPO])
        self.accept_plan(tools=["github_merge", "read_task", "control_task"])
        arguments = dict(repository=REPO, number=2, expected_head_sha=HEAD, expected_base_sha=BASE,
                         commit_title=title, commit_message=body, task_context=self.context_for(identity))
        with patch.dict(fixture.pr, base={"sha": BASE}):
            self.fault("github_evidence_unavailable", self.app.github_merge, **arguments)
            self.assertEqual(self.view()["record"]["operations"], [])
        result = self.app.github_merge(**arguments)
        self.assertEqual(result["status"], "merge_outcome_unknown")
        original = self.view()["record"]["operations"][0]
        self.assertEqual(original["result"]["effect"], "effect_unknown")
        self.app = create_application(self.config, self.state)
        connect(self.app)
        # Passive reads do not mutate history. Explicit acceptance observes the
        # original destination; a fresh revision is then required for the decision.
        self.assertEqual(self.view()["record"]["operations"][0], original)
        decision = dict(stage_id="verify", outcome="accepted", evidence=[identity], reason="Verified original merge.")
        with patch.dict(fixture.pr, state="open", merged=False):
            self.fault("task_evidence_invalid", self.control, "record_decision", **decision)
            self.fault("task_effect_unresolved", self.app.github_merge,
                       **dict(arguments, task_context=self.context_for(uuid.uuid4().hex)))
            self.assertEqual(self.view()["record"]["operations"][0], original)
        self.fault("task_conflict", self.control, "record_decision", **decision)
        verified = self.view()["record"]["operations"][0]
        self.assertEqual(verified["result"]["status"], "succeeded")
        for key in ("id", "request_sha256", "locator", "admitted_at_ms"):
            self.assertEqual(verified[key], original[key])
        self.control("record_decision", **decision)
        completed = self.control("finish", outcome="completed",
            acceptance=[{"stage_id": "verify", "operation_id": identity}], limitations="Synthetic GitHub transport.")
        self.assertEqual(completed["record"]["terminal"]["outcome"], "completed")
        self.assertEqual(sum(call.args[0] == "PUT" for call in transport.call_args_list), 1)
        self.fault("task_operation_conflict", self.app.github_merge, **dict(arguments, commit_message="Different intent"))

    def github_fixture(self, tools=None):
        provider = SimpleNamespace(DEFINITE_FAILURES=github.DEFINITE_FAILURES,
            read=Mock(return_value={"configured_write": True, "data": {"permissions": {"push": True}}}),
            issue=Mock(return_value={"status": "write_outcome_unknown"}),
            reconcile=Mock(return_value={"status": "write_outcome_unknown"}))
        self.app.github = provider
        # These tests isolate task admission; connection behavior has its own fixtures.
        self.app.connections.select = Mock(return_value=SimpleNamespace(binding=lambda: {
            "connection_id": "d" * 32, "account_id": 1, "authorization_revision": 1}))
        self.app.connections.request = Mock()
        self.request["contract"]["targets"] = []
        self.request["contract"]["repositories"] = ["Owner/repo"]
        self.accept_plan(tools=tools or ["github_issue", "read_task", "control_task"])
        return provider

    def test_supervised_comment_delete_remains_bodyless_and_keeps_exact_identity(self):
        provider = self.github_fixture(tools=["github_comment", "read_task", "control_task"])
        provider.comment = Mock(return_value={
            "status": "verified", "receipt": {"id": 9, "number": 1},
            "deleted": True, "fields_verified": True})
        identity = uuid.uuid4().hex
        args = dict(action="delete", repository=REPO, number=1, comment_id=9,
                    task_context=self.context_for(identity))
        outcome = self.app.github_comment(**args)
        self.assertEqual(outcome["status"], "verified")
        self.assertEqual(provider.comment.call_args.kwargs["body"], "")
        self.assertEqual(provider.comment.call_args.kwargs["action"], "delete")
        self.assertEqual(provider.comment.call_count, 1)
        stored = self.view()["record"]["operations"][0]
        self.assertEqual((stored["locator"]["number"], stored["locator"]["parent_number"]), (9, 1))
        self.assertEqual(stored["result"]["status"], "succeeded")

    def test_provider_unknown_write_keeps_original_identity_and_never_resubmits(self):
        provider = self.github_fixture()
        identity = uuid.uuid4().hex
        args = dict(action="create", repository="Owner/repo", title="Publication fixture", body="Do not copy this body into task history.",
                    task_context=self.context_for(identity))
        result = self.app.github_issue(**args)
        self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertIn("<!-- om-task:structured-one:" + identity + " -->", provider.issue.call_args.kwargs["body"])
        self.assertNotIn(args["body"], records.canonical(tasks.read(self.state, "structured-one")).decode())
        provider.reconcile.return_value = {"status": "verified", "receipt": {"id": 71, "number": 7, "updated_at": "fixture"}}
        recovered = self.app.github_issue(**args)
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual(provider.issue.call_count, 1)
        self.assertEqual(self.view()["record"]["operations"][0]["locator"]["number"], 7)
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")
        self.fault("task_operation_conflict", self.app.github_issue, **dict(args, title="Different request"))
        self.assertEqual(provider.issue.call_count, 1)

    def test_bodyless_provider_update_cannot_strip_known_pending_context(self):
        provider = self.github_fixture()
        identity = uuid.uuid4().hex
        args = dict(action="update", repository="Owner/repo", title="New title", number=7,
                    task_context=self.context_for(identity))
        self.app.github_issue(**args)
        self.assertEqual(self.view()["record"]["operations"][0]["state"], "unknown")
        bare = {key: value for key, value in args.items() if key != "task_context"}
        self.fault("task_effect_unresolved", self.app.github_issue, **bare)
        self.assertEqual(provider.issue.call_count, 1)
        recovered = self.app.github_issue(**args)
        self.assertEqual(recovered["status"], "write_outcome_unknown")
        self.assertEqual(provider.issue.call_count, 1)

    def test_known_failures_survive_inconclusive_reads_and_same_task_can_finish(self):
        provider = self.github_fixture()
        for code in ("github_network_unavailable", "github_rate_limited", "resource_busy", "github_access_denied"):
            for read_fails in (False, True):
                with self.subTest(code=code, read_fails=read_fails):
                    identity = uuid.uuid4().hex
                    args = dict(action="create", repository="Owner/repo", title="Retry fixture", body="Authorized work.",
                                task_context=self.context_for(identity))
                    provider.issue.side_effect = Fault(code, "Known no-effect failure.", effect="known_no_effect")
                    provider.reconcile.side_effect = Fault("github_response_unknown", "Read unavailable.") if read_fails else None
                    calls = provider.issue.call_count
                    self.fault(code, self.app.github_issue, **args)
                    record = self.view()["record"]
                    self.assertEqual(record["operations"][-1]["result"]["status"], "failed")
                    self.assertEqual(record["control"]["state"], "waiting_for_ai")
                    self.assertIsNone(record["terminal"])
                    provider.reconcile.side_effect = None
                    observed = self.app.github_issue(**args)
                    self.assertFalse(observed["task_operation_replayed"])
                    self.assertEqual(provider.issue.call_count, calls + 1)
                    self.assertEqual(self.view()["record"]["operations"][-1]["result"]["status"], "failed")

        provider.issue.side_effect = None
        provider.issue.return_value = provider.reconcile.return_value = {
            "status": "verified", "receipt": {"id": 71, "number": 7, "updated_at": "fixture"}}
        identity = uuid.uuid4().hex
        self.app.github_issue(**dict(args, task_context=self.context_for(identity)))
        self.control("record_decision", stage_id="verify", outcome="accepted",
                     evidence=[identity], reason="The corrected prerequisite allowed the original work to finish.")
        completed = self.control("finish", outcome="completed",
            acceptance=[{"stage_id": "verify", "operation_id": identity}], limitations="")
        self.assertEqual(completed["record"]["terminal"]["outcome"], "completed")
        self.assertEqual(tasks.read(self.state, "structured-one")["facts"]["operation_count"], 9)

    def test_long_history_keeps_original_lookup_deduplication_and_acceptance(self):
        provider = self.github_fixture()
        provider.issue.return_value = {"status": "verified", "receipt": {"id": 71, "number": 7}}
        identity = uuid.uuid4().hex
        first = dict(action="create", repository="Owner/repo", title="Original verified work", body="Fixture.",
                     task_context=self.context_for(identity))
        self.app.github_issue(**first)
        original = tasks.read(self.state, "structured-one")["record"]["operations"][0]
        provider.issue.side_effect = Fault("github_access_denied", "Known no-effect fixture.", effect="known_no_effect")
        for _ in range(70):
            self.fault("github_access_denied", self.app.github_issue,
                       **dict(first, task_context=self.context_for(uuid.uuid4().hex)))
        current = tasks.read(self.state, "structured-one")
        self.assertEqual(current["facts"]["operation_count"], 71)
        self.assertEqual(len(current["record"]["operations"]), 1)
        self.assertNotEqual(current["record"]["operations"][0]["id"], identity)
        calls = provider.issue.call_count
        replay = self.app.github_issue(**first)
        self.assertFalse(replay["task_operation_replayed"])
        self.fault("task_operation_conflict", self.app.github_issue, **dict(first, title="Changed original intent"))
        self.assertEqual(provider.issue.call_count, calls)
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[identity],
                     reason="Retain the original verified result beyond the bounded current view.")
        completed = self.control("finish", outcome="completed",
            acceptance=[{"stage_id": "verify", "operation_id": identity}], limitations="Synthetic provider only.")
        self.assertEqual(completed["record"]["terminal"]["outcome"], "completed")
        retained = tasks.read(self.state, "structured-one", operation_ids=[identity])
        self.assertEqual(next(item for item in retained["record"]["operations"] if item["id"] == identity), original)

    def test_rate_rejection_returns_wait_evidence_without_immediate_provider_read(self):
        provider = self.github_fixture()
        fault = Fault("github_rate_limited", "Wait before another request.", {"retry_after_seconds": 60}, effect="known_no_effect")
        provider.issue.side_effect = fault
        with self.assertRaises(Fault) as caught:
            self.app.github_issue(action="create", repository="Owner/repo", title="Fixture", body="Fixture",
                                  task_context=self.context_for(uuid.uuid4().hex))
        self.assertIs(caught.exception, fault)
        provider.reconcile.assert_not_called()
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "failed")
        self.assertIsNone(self.view()["record"]["terminal"])

    def test_no_effect_rejection_cannot_be_promoted_by_a_matching_destination(self):
        provider = self.github_fixture()
        provider.issue.side_effect = Fault("github_access_denied", "Rejected.", effect="known_no_effect")
        provider.reconcile.return_value = {"status": "verified", "receipt": {"id": 71, "number": 7}}
        self.fault("github_access_denied", self.app.github_issue, action="create",
                   repository="Owner/repo", title="Fixture", body="Fixture",
                   task_context=self.context_for(uuid.uuid4().hex))
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "failed")
        self.assertEqual(provider.issue.call_count, 1)
        provider.reconcile.assert_not_called()

    def test_all_provider_entrypoints_reject_stale_context_before_provider_access(self):
        self.request["contract"]["targets"] = []
        self.accept_plan(tools=["read_task", "control_task"])
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


class GoalRecoveryTests(GoalRecoveryFixture, unittest.TestCase):
    def test_recovered_goal_allows_one_verified_merge_and_terminal_acceptance(self):
        arguments, original = self.lose_ack()
        self.recover(arguments)
        self.provider.runs = [{"id": 31, "name": "ci", "head_sha": HEAD, "app": {"id": 3},
                               "status": "completed", "conclusion": "success"}]
        def merged(method, endpoint, payload=None):
            if method == "PUT":
                self.assertEqual(endpoint, f"{ROOT}/pulls/2/merge")
                self.assertEqual(payload["sha"], HEAD)
                self.provider.pr.update(state="closed", merged=True, merge_commit_sha=MERGE)
                self.provider.pr["base"]["sha"] = MERGE
                return {"merged": True, "sha": MERGE}
            values = {f"{ROOT}/commits/{MERGE}": {"sha": MERGE, "commit": {"message": "Merged fixture"}},
                      f"{ROOT}/branches/main": {"commit": {"sha": MERGE}},
                      f"{ROOT}/compare/{MERGE}...{MERGE}": {"status": "identical", "merge_base_commit": {"sha": MERGE}}}
            return copy.deepcopy(values[endpoint]) if endpoint in values else self.response(method, endpoint, payload)
        self.transport.side_effect = merged
        identity = uuid.uuid4().hex
        self.assertEqual(self.merge(task_context=self.context_for(identity))["status"], "merged_verified")
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[original["id"], identity],
                     reason="Current resolved goal and verified merge; original resolve attribution remains unknown.")
        terminal = self.control("finish", outcome="completed", acceptance=[
            {"stage_id": "verify", "operation_id": original["id"]},
            {"stage_id": "verify", "operation_id": identity}], limitations="Original resolve write is unproven.")
        self.assertEqual(terminal["record"]["terminal"]["outcome"], "completed")
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)
        self.assertEqual(sum(call.args[0] == "PUT" for call in self.transport.call_args_list), 1)

    def test_expired_task_authority_cannot_publish_a_goal_witness(self):
        arguments, original = self.lose_ack()
        expiry = self.view()["record"]["contract"]["expires_at_ms"]
        with patch("openmachine.app.task_attention.now_ms", return_value=expiry):
            self.fault("task_authority_expired", self.recover, arguments)
        self.assertNotIn("goal_witnesses", self.view()["record"])
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)

    def test_changed_source_does_not_reuse_a_goal_for_new_acceptance(self):
        arguments, original = self.lose_ack()
        self.recover(arguments)
        record = self.view()["record"]
        contract = copy.deepcopy(record["contract"])
        contract["sources"] = [{"locator": "request:changed-goal", "sha256": "f" * 64}]
        stage = {key: value for key, value in record["plan"]["stages"][0].items()
                 if key not in {"state", "evidence"}}
        stage["source_refs"] = ["request:changed-goal"]
        stage["source_sha256"] = records.digest(contract["sources"])
        revised = self.control("revise_plan", reason="A changed source requires fresh acceptance.",
                               contract=contract, stages=[stage], invalidated_stage_ids=["verify"])
        self.control("claim", handoff_id=revised["record"]["handoff"]["id"])
        self.fault("task_evidence_invalid", self.accept_goal, original)
        self.assertIsNone(self.view()["record"]["terminal"])
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)

    def test_passive_reads_and_changed_arguments_cannot_invent_original_intent(self):
        arguments, original = self.lose_ack()
        self.app.read_task("structured-one")
        self.assertNotIn("goal_witnesses", self.view()["record"])
        for changed in ({"action": "unresolve"}, {"thread_id": "PRRT_other"},
                        {"number": 3}, {"expected_head_sha": "f" * 40}, {"expected_base_sha": ""}):
            with self.subTest(changed=changed):
                self.fault("task_operation_conflict", self.recover, arguments | changed)
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)

    def test_unresolve_unknown_never_becomes_a_monotonic_resolve_goal(self):
        self.provider.threads[0]["isResolved"] = True
        arguments, original = self.lose_ack(action="unresolve")
        self.provider.threads[0]["isResolved"] = True
        observed = self.recover(arguments)
        self.assertEqual(observed["status"], "write_outcome_unknown")
        self.assertNotIn("goal_witness", observed)
        self.assertNotIn("goal_witnesses", self.view()["record"])
        self.fault("task_effect_unresolved", self.merge)
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)

    def test_independent_thread_and_local_work_proceed_but_same_thread_stays_excluded(self):
        arguments, original = self.lose_ack()
        self.provider.threads.append({"id": "PRRT_independent", "isResolved": False,
                                      "isOutdated": False, "path": "b.py", "line": 2})
        self.assertEqual(self.app.write_workspace_file(**self.write_arguments())["status"], "applied")
        self.lose_ack(thread_id="PRRT_independent")
        self.recover(arguments)
        identity = uuid.uuid4().hex
        self.fault("task_effect_unresolved", self.app.github_review_thread,
                   **(arguments | {"action": "unresolve", "task_context": self.context_for(identity)}))
        self.fault("task_effect_unresolved", self.merge)
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 2)

    def test_current_target_membership_and_head_are_not_inferred_from_old_locator(self):
        arguments, original = self.lose_ack()
        self.provider.threads[0]["id"] = "PRRT_other"
        self.fault("github_not_found", self.recover, arguments)
        self.provider.threads[0]["id"] = "PRRT_goal"
        self.provider.pr["head"]["sha"] = "f" * 40
        self.fault("github_candidate_changed", self.recover, arguments)
        self.assertNotIn("goal_witnesses", self.view()["record"])
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)

    def test_witness_never_replaces_current_threads_native_policy_head_or_base_gate(self):
        arguments, original = self.lose_ack()
        self.recover(arguments)
        self.provider.threads[0]["isResolved"] = False
        self.assertEqual(self.merge()["status"], "merge_blocked")
        self.provider.threads[0]["isResolved"] = True
        self.provider.policy["reviewDecision"] = "REVIEW_REQUIRED"
        self.assertEqual(self.merge()["status"], "merge_blocked")
        self.provider.policy["reviewDecision"] = None
        self.provider.pr["head"]["sha"] = "f" * 40
        self.fault("github_candidate_changed", self.merge)
        self.provider.pr["head"]["sha"] = HEAD
        self.provider.pr["base"]["sha"] = "e" * 40
        self.fault("github_candidate_changed", self.merge)
        self.provider.runs = [{"id": 31, "name": "ci", "head_sha": HEAD, "app": {"id": 3},
                               "status": "completed", "conclusion": "failure"}]
        self.assertEqual(self.merge(expected_base_sha="e" * 40)["status"], "merge_blocked")
        self.provider.pr["base"]["sha"] = BASE
        self.provider.pr["base"]["ref"] = "another-destination"
        self.fault("task_effect_unresolved", self.merge)
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertFalse(any(call.args[0] == "PUT" for call in self.transport.call_args_list))

    def test_reopened_goal_is_rejected_at_both_decision_and_terminal_acceptance(self):
        arguments, original = self.lose_ack()
        self.recover(arguments)
        self.provider.threads[0]["isResolved"] = False
        self.fault("task_evidence_changed", self.accept_goal, original)
        self.provider.threads[0]["isResolved"] = True
        self.accept_goal(original)
        self.provider.threads[0]["isResolved"] = False
        self.fault("task_evidence_changed", self.control, "finish", outcome="completed",
                   acceptance=[{"stage_id": "verify", "operation_id": original["id"]}], limitations="")
        self.assertIsNone(self.view()["record"]["terminal"])

    def test_same_claim_renewal_can_recover_but_stop_cannot(self):
        arguments, original = self.lose_ack()
        owner = self.view()["record"]["control"]["owner"]
        tasks.update(self.state, "structured-one", lambda value, now:
                     value["control"]["owner"].update(expires_at_ms=now - 1))
        self.fault("task_renewal_required", self.recover, arguments)
        self.control("renew", claim_id=owner["claim_id"])
        self.assertIn("goal_witness", self.recover(arguments))
        self.assertEqual(self.thread_writes, 1)
        self.control("pause", reason="Explicit stop before acceptance.")
        self.fault("task_stopped", self.accept_goal, original)

    def test_pause_between_read_and_witness_commit_wins_without_replay(self):
        arguments, original = self.lose_ack()
        read = self.app.task_receipts.read
        def paused(*args, **kwargs):
            observed = read(*args, **kwargs)
            self.control("pause", reason="Concurrent explicit stop.")
            return observed
        with patch.object(self.app.task_receipts, "read", side_effect=paused):
            self.fault("task_conflict", self.recover, arguments)
        self.assertNotIn("goal_witnesses", self.view()["record"])
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)

    def test_lost_thread_acknowledgement_reaches_fresh_merge_gate_without_replay(self):
        arguments, original = self.lose_ack()
        self.assertTrue(self.provider.threads[0]["isResolved"])
        self.fault("task_effect_unresolved", self.merge)
        self.assertEqual(self.recover(arguments)["status"], "write_outcome_unknown")
        self.provider.runs = [{"id": 31, "name": "ci", "head_sha": HEAD, "app": {"id": 3},
                               "status": "completed", "conclusion": "failure"}]
        # Recovery removes only the proven resolve dependency, not the current gate.
        self.assertEqual(self.merge()["status"], "merge_blocked")
        self.assertEqual(tasks.operation(self.state, "structured-one", original["id"]), original)
        self.assertEqual(self.thread_writes, 1)
        self.assertFalse(any(call.args[0] == "PUT" for call in self.transport.call_args_list))


if __name__ == "__main__":
    unittest.main()
