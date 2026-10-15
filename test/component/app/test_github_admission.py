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
from test.support.github_cases import BASE, HEAD, MERGE, REPO, ROOT, TREE, GitHubCaseFixture, settings


class TaskAdmissionTests(TaskFixture, unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
