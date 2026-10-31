"""Retained scope, owner failures, historical evidence and observation admission."""

from copy import deepcopy
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from integrations import github
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.tasks import records
from test.support.tasks import TaskFixture, replace_task_record, task_contract


class ScopedSupervisionTests(TaskFixture, unittest.TestCase):
    def test_thread_success_after_acknowledgement_freezes_original_result(self):
        args = dict(action="resolve", repository="Owner/repo", number=7, thread_id="PRRT_fixture",
                    expected_head_sha="e" * 40, task_context=self.context_for(uuid.uuid4().hex))
        def writer(*_, on_acknowledgement, **kwargs):
            on_acknowledgement({"acknowledged": "fixture"})
            return {"status": "verified", "receipt": {"number": 7, "thread_id": "PRRT_fixture",
                    "requested_isResolved": True, "isResolved": True}}
        self.provider.review_thread.side_effect = writer
        self.assertEqual(self.app.github_review_thread(**args)["status"], "verified")
        original = deepcopy(self.view()["record"]["operations"][0])
        self.assertEqual(original["result"]["status"], "succeeded")
        self.assertEqual(self.app.github_review_thread(**args)["status"], "retained_task_evidence")
        self.assertEqual(self.view()["record"]["operations"][0], original)
        self.provider.reconcile.assert_not_called()
        self.assertEqual(self.provider.review_thread.call_count, 1)

    def test_thread_acknowledgement_persists_before_lost_reply_and_new_reader(self):
        args = dict(action="resolve", repository="Owner/repo", number=7,
                    thread_id="PRRT_fixture", expected_head_sha="e" * 40,
                    task_context=self.context_for(uuid.uuid4().hex))
        receipt = {"kind": "github_review_thread_ack_v1", "repository": "owner/repo", "number": 7,
                   "thread_id": "PRRT_fixture", "head_sha": "e" * 40, "actor_id": 1, "isResolved": True}
        def interrupted(*_, on_acknowledgement, **kwargs):
            on_acknowledgement(receipt)
            self.assertEqual(self.view()["record"]["operations"][0]["result"]["receipt_sha256"], records.digest(receipt))
            raise SystemExit("Caller lost before final reply")
        self.provider.review_thread.side_effect = interrupted
        with self.assertRaises(SystemExit):
            self.app.github_review_thread(**args)
        original = deepcopy(self.view()["record"]["operations"][0])
        self.assertEqual(original["state"], "unknown")
        self.app = create_application(self.config, self.state)
        self.app.github = self.provider
        self.app.connections.request = Mock()
        self.assertFalse(self.app.github_review_thread(**args)["task_operation_replayed"])
        self.assertEqual(self.view()["record"]["operations"][0], original)
        self.assertEqual(self.provider.reconcile.call_args.kwargs["acknowledgement_sha256"], records.digest(receipt))
        self.provider.reconcile.return_value = {"status": "verified", "receipt": receipt}
        self.assertEqual(self.app.github_review_thread(**args)["status"], "verified")
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")
        self.assertEqual(self.provider.review_thread.call_count, 1)

    def test_thread_acknowledgement_excludes_concurrent_replay(self):
        entered, finish = threading.Event(), threading.Event()
        args = dict(action="resolve", repository="Owner/repo", number=7, thread_id="PRRT_fixture",
                    expected_head_sha="e" * 40, task_context=self.context_for(uuid.uuid4().hex))
        failures = []
        def writer(*_, on_acknowledgement, **kwargs):
            on_acknowledgement({"acknowledged": "fixture"})
            entered.set()
            if not finish.wait(5):
                raise AssertionError("Test controller did not release writer")
            return {"status": "write_unverified"}
        def invoke():
            try:
                self.app.github_review_thread(**args)
            except Exception as error:
                failures.append(error)
        self.provider.review_thread.side_effect = writer
        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.fault("resource_busy", self.app.github_review_thread, **args)
        finally:
            finish.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.provider.review_thread.call_count, 1)
        self.assertFalse(self.app.github_review_thread(**args)["task_operation_replayed"])

    def test_failed_acknowledgement_save_keeps_unknown_and_does_not_replay(self):
        args = dict(action="resolve", repository="Owner/repo", number=7, thread_id="PRRT_fixture",
                    expected_head_sha="e" * 40, task_context=self.context_for(uuid.uuid4().hex))
        def writer(*_, on_acknowledgement, **kwargs):
            on_acknowledgement({"acknowledged": "fixture"})
            self.fail("A failed save must stop post-write work")
        self.provider.review_thread.side_effect = writer
        with patch.object(self.app.task_admission, "_publish", side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                self.app.github_review_thread(**args)
        self.assertFalse(self.app.github_review_thread(**args)["task_operation_replayed"])
        self.assertEqual(self.provider.review_thread.call_count, 1)
        self.assertEqual(self.view()["record"]["operations"][0]["state"], "unknown")

    def setUp(self):
        super().setUp()
        self.provider = SimpleNamespace(DEFINITE_FAILURES=github.DEFINITE_FAILURES,
            read=Mock(return_value={"configured_write": True, "data": {"permissions": {"push": True}, "base": {"ref": "main"}}}),
            reconcile=Mock(return_value={"status": "write_outcome_unknown"}))
        for name in ("issue", "pull_request", "comment", "review", "review_thread", "merge", "project", "release"):
            setattr(self.provider, name, Mock(return_value={"status": "write_outcome_unknown"}))
        self.app.github = self.provider
        self.app.connections.select = Mock(return_value=SimpleNamespace(binding=lambda: {
            "connection_id": "d" * 32, "account_id": 1, "authorization_revision": 1}))
        self.app.connections.request = Mock()
        self.request["contract"]["repositories"] = ["Owner/repo"]
        self.accept_plan(tools=["github_issue", "github_pull_request", "github_comment", "github_review", "github_review_thread", "github_merge", "github_project", "github_release",
                               "write_workspace_file", "start_execution", "read_task", "control_task"])

    def update_pr(self, **values):
        return self.app.github_pull_request(action="update", repository="Owner/repo", number=7,
            expected_head_sha="e" * 40, task_context=self.context_for(uuid.uuid4().hex), **values)

    def verified(self, number=7):
        return {"status": "verified", "receipt": {"id": 71, "number": number, "updated_at": "original"}}

    def historical_record(self):
        record = deepcopy(self.view()["record"])
        self.assertEqual(len(record["operations"]), record["_facts"]["operation_count"])
        record.pop("_facts")
        record.pop("_decisions", None)
        return record

    def proof_result(self, request):
        result = self.app.control_task(**request)
        return {**result, **tasks.read(self.state, result["task_id"],
                                      operation_ids=[request["transition"]["operation_id"]])}

    def second_task(self):
        request = task_contract(self.context, "second-task")
        request["contract"] = deepcopy(self.request["contract"])
        created = self.app.create_task(**request)["record"]
        claimed = self.app.control_task(task_id="second-task", expected_revision=created["revision"],
            expected_plan_revision=created["plan"]["revision"], expected_epoch=created["control"]["epoch"],
            update_id=uuid.uuid4().hex, transition={"action": "claim", "handoff_id": created["handoff"]["id"]})["record"]
        return {"task_id": "second-task", "task_revision": claimed["revision"], "plan_revision": claimed["plan"]["revision"],
                "continuation_epoch": claimed["control"]["epoch"], "operation_id": uuid.uuid4().hex}

    def test_release_requires_original_nonempty_notes_before_task_marker(self):
        self.fault("invalid_github_request", self.app.github_release,
                   action="create", repository="Owner/repo", tag_name="v1.2.3",
                   body=" ", task_context=self.context_for(uuid.uuid4().hex))
        self.provider.release.assert_not_called()

    def test_release_creation_replays_original_id_and_scopes_exact_tag(self):
        identity = uuid.uuid4().hex
        args = dict(action="create", repository="Owner/repo", tag_name="v1.2.3",
                    body="Original notes", task_context=self.context_for(identity))
        self.assertEqual(self.app.github_release(**args)["status"], "write_outcome_unknown")
        first = deepcopy(self.view()["record"]["operations"][0])
        self.assertEqual(first["locator"]["tag_name"], "v1.2.3")
        self.assertEqual((first["effect_scope"]["resource"], first["effect_scope"]["fields"]),
                         ("release", ["create"]))
        self.assertIn("<!-- om-task:", self.provider.release.call_args.kwargs["body"])
        self.assertFalse(self.app.github_release(**args)["task_operation_replayed"])
        self.assertEqual(self.provider.release.call_count, 1)
        self.assertEqual(self.view()["record"]["operations"][0], first)
        self.fault("task_effect_unresolved", self.app.github_release,
                   action="create", repository="Owner/repo", tag_name="v1.2.3",
                   body="Changed", task_context=self.context_for(uuid.uuid4().hex))
        other = self.app.github_release(action="create", repository="Owner/repo",
                                        tag_name="v1.2.4", body="Other notes",
                                        task_context=self.context_for(uuid.uuid4().hex))
        self.assertEqual(other["status"], "write_outcome_unknown")
        self.assertEqual(self.provider.release.call_count, 2)

    def test_project_reconciliation_keeps_original_intent_and_never_repeats_write(self):
        identity = uuid.uuid4().hex
        args = dict(action="update_field", repository="Owner/repo", project_number=12,
                    item_id="PVTI_fixture", field_id="PVTF_fixture", value={"text": "done"},
                    task_context=self.context_for(identity))
        self.assertEqual(self.app.github_project(**args)["status"], "write_outcome_unknown")
        self.fault("task_operation_conflict", self.app.github_project, **dict(args, value={"text": "changed"}))
        self.assertFalse(self.app.github_project(**args)["task_operation_replayed"])
        self.assertEqual(self.view()["record"]["operations"][0]["state"], "unknown")
        self.provider.reconcile.return_value = {"status": "verified", "receipt": {
            "id": "PVTI_fixture", "project_number": 12}, "fields_verified": True}
        result = self.app.github_project(**args)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["task_operation_replayed"])
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")
        self.assertEqual(self.provider.project.call_count, 1)

    def test_unknown_project_write_blocks_alias_across_same_organization(self):
        result = self.app.github_project(
            action="update_field", repository="Owner/repo", project_number=12,
            item_id="PVTI_fixture", field_id="PVTF_fixture", value={"text": "done"},
            task_context=self.context_for(uuid.uuid4().hex))
        self.assertEqual(result["status"], "write_outcome_unknown")
        original = deepcopy(self.view()["record"]["operations"][0])
        proposal = deepcopy(original)
        proposal["id"] = uuid.uuid4().hex
        proposal["locator"]["repository"] = "Owner/other"
        self.assertTrue(tasks.effect_conflicts(self.state, {}, proposal))
        proposal["effect_scope"]["number"] = proposal["locator"]["number"] = 13
        self.assertFalse(tasks.effect_conflicts(self.state, {}, proposal))
        proposal["effect_scope"]["number"] = proposal["locator"]["number"] = 12
        proposal["locator"]["repository"] = "Elsewhere/other"
        self.assertFalse(tasks.effect_conflicts(self.state, {}, proposal))
        self.assertEqual(self.view()["record"]["operations"][0], original)

    def test_review_thread_unknown_keeps_precise_identity_and_blocks_merge(self):
        op_id = uuid.uuid4().hex
        args = dict(action="resolve", repository="Owner/repo", number=7,
                    thread_id="PRRT_fixture", expected_head_sha="e" * 40,
                    task_context=self.context_for(op_id))
        self.assertEqual(self.app.github_review_thread(**args)["status"], "write_outcome_unknown")
        original = deepcopy(self.view()["record"]["operations"][0])
        self.assertEqual(original["effect_scope"]["fields"], ["threads"])
        self.assertEqual((original["locator"]["thread_id"], original["locator"]["expected_head_sha"]),
                         ("PRRT_fixture", "e" * 40))
        replay = self.app.github_review_thread(**args)
        self.assertFalse(replay["task_operation_replayed"])
        self.assertEqual(self.provider.review_thread.call_count, 1)
        self.assertEqual(original, self.view()["record"]["operations"][0])
        self.fault("task_effect_unresolved", self.app.github_merge, repository="Owner/repo",
                   number=7, expected_head_sha="e" * 40,
                   task_context=self.context_for(uuid.uuid4().hex))
        self.provider.merge.assert_not_called()

    def test_explicit_no_effect_dominates_matching_snapshot_and_retains_failure(self):
        self.provider.pull_request.side_effect = Fault("github_object_changed", "Preflight rejection.", effect="known_no_effect")
        self.provider.reconcile.return_value = self.verified()
        self.fault("github_object_changed", self.update_pr, labels=["bug"])
        operation = self.view()["record"]["operations"][0]
        self.assertEqual((operation["result"]["status"], operation["result"]["failure_code"], operation["result"]["effect"]),
                         ("failed", "github_object_changed", "known_no_effect"))
        self.provider.reconcile.assert_not_called()
        self.assertEqual(operation["effect_scope"]["fields"], ["labels"])

    def test_same_code_after_dispatch_is_unknown_and_never_repeated(self):
        self.provider.pull_request.side_effect = Fault("github_object_changed", "Dispatched request lost its reply.", effect="effect_unknown")
        self.fault("github_object_changed", self.update_pr, labels=["bug"])
        operation = self.view()["record"]["operations"][0]
        self.assertEqual((operation["state"], operation["result"]["effect"]), ("unknown", "effect_unknown"))
        self.fault("task_effect_unresolved", self.update_pr, labels=["another"])
        self.assertEqual(self.provider.pull_request.call_count, 1)

    def test_explicit_unknown_is_not_promoted_by_immediate_matching_snapshot(self):
        self.provider.pull_request.side_effect = Fault("github_object_changed", "The dispatched reply was lost.", effect="effect_unknown")
        self.provider.reconcile.return_value = self.verified()
        self.fault("github_object_changed", self.update_pr, labels=["bug"])
        operation = self.view()["record"]["operations"][0]
        self.assertEqual(operation["state"], "unknown")
        self.assertEqual((operation["result"]["status"], operation["result"]["failure_code"], operation["result"]["effect"]),
                         ("unknown", "github_object_changed", "effect_unknown"))
        self.provider.reconcile.assert_not_called()
        self.assertEqual(self.provider.pull_request.call_count, 1)

    def test_connection_preparation_failure_after_admission_is_known_no_effect(self):
        for error, code in ((Fault("integration_disconnected", "Binding changed."), "integration_disconnected"),
                            (OSError("Local connection store unavailable."), "integration_store_unavailable")):
            with self.subTest(code=code):
                def prepare(*args, **kwargs):
                    if kwargs.get("write"):
                        raise error
                    return Mock()
                self.app.connections.request.side_effect = prepare
                self.fault(code, self.update_pr, labels=[])
                result = self.view()["record"]["operations"][-1]["result"]
                self.assertEqual((result["status"], result["failure_code"], result["effect"]),
                                 ("failed", code, "known_no_effect"))
                self.provider.pull_request.assert_not_called()
                self.provider.reconcile.assert_not_called()

    def test_returned_owner_unknown_retains_phase_and_code_across_passive_reads(self):
        self.provider.pull_request.return_value = {"status": "write_unverified", "error_code": "github_access_denied"}
        self.update_pr(labels=[])
        original = deepcopy(self.view()["record"]["operations"][0])
        self.assertEqual((original["result"]["status"], original["result"]["effect"], original["result"]["failure_code"]),
                         ("unknown", "effect_unknown", "github_access_denied"))
        self.provider.reconcile.return_value = {"status": "observed", "error_code": "github_not_found"}
        self.app.task_admission.reconcile(self.view())
        self.assertEqual(self.view()["record"]["operations"][0], original)

    def test_unknown_labels_allow_local_write_and_independent_body_review_merge(self):
        self.update_pr(labels=["bug"], assignees=["person"])
        original = deepcopy(self.view()["record"]["operations"][0])
        written = self.app.write_workspace_file(**self.write_arguments())
        self.assertEqual(written["status"], "applied")
        self.provider.pull_request.return_value = self.verified()
        self.update_pr(body="Reviewed candidate details.")
        self.provider.review.return_value = {"status": "verified", "receipt": {"id": 22}}
        self.app.github_review(action="create", repository="Owner/repo", number=7, body="Complete review.",
            expected_head_sha="e" * 40, task_context=self.context_for(uuid.uuid4().hex))
        self.provider.merge.return_value = {"status": "merged_verified", "number": 7,
                                            "head_sha": "e" * 40, "merge_commit_sha": "f" * 40}
        merged = self.app.github_merge(repository="Owner/repo", number=7, expected_head_sha="e" * 40,
            task_context=self.context_for(uuid.uuid4().hex))
        self.assertEqual(merged["status"], "merged_verified")
        self.assertIn(original["id"], merged["task"]["unresolved_operation_ids"])
        self.assertEqual(self.view()["record"]["operations"][0], original)
        self.fault("task_effect_unresolved", self.control, "finish", outcome="failed", acceptance=[], limitations="Unresolved metadata retained.")

    def test_cross_task_account_alias_and_bare_routes_cannot_bypass_same_fields(self):
        self.update_pr(labels=[])
        context = self.second_task()
        calls = self.provider.issue.call_count
        self.fault("task_effect_unresolved", self.app.github_issue, action="update", repository="Owner/repo",
                   number=7, labels=["bug"], task_context=context)
        self.fault("task_effect_unresolved", self.app.github_issue, action="update", repository="owner/REPO",
                   number=7, labels=["bug"], connection_id="a" * 32)
        self.assertEqual(self.provider.issue.call_count, calls)
        self.provider.pull_request.return_value = self.verified()
        self.app.github_pull_request(action="update", repository="Owner/repo", number=7, body="Independent.",
                                     task_context=context)
        self.assertEqual(len(tasks.read(self.state, "second-task")["record"]["operations"]), 1)

    def test_unverified_pr_creation_excludes_duplicates_not_unrelated_branches(self):
        self.app.github_pull_request(action="create", repository="Owner/repo", title="Original", body="Original",
            head="topic", base="main", task_context=self.context_for(uuid.uuid4().hex))
        for head in ("topic", "Owner:topic", "OWNER:topic"):
            self.fault("task_effect_unresolved", self.app.github_pull_request, action="create", repository="Owner/repo",
                       title="Duplicate", body="Different title", head=head, base="main")
        created = self.app.github_pull_request(action="create", repository="Owner/repo", title="Independent",
            body="Independent", head="another", base="main")
        self.assertEqual(created["status"], "write_outcome_unknown")
        self.assertEqual(self.provider.pull_request.call_count, 2)

    def test_abandon_unknown_provider_effect_preserves_cross_task_field_guards(self):
        self.update_pr(labels=[])
        original = deepcopy(self.view()["record"]["operations"][0])
        abandoned = self.control("abandon", reason="Continue only disjoint work.")
        self.assertEqual(abandoned["record"]["terminal"]["outcome"], "abandoned")
        self.assertEqual(self.view()["record"]["operations"][0], original)
        other = self.second_task()
        self.fault("task_effect_unresolved", self.app.github_issue, action="update",
                   repository="Owner/repo", number=7, labels=[], task_context=other)
        self.provider.pull_request.return_value = self.verified()
        applied = self.app.github_pull_request(action="update", repository="Owner/repo",
                   number=7, body="Independent review.", task_context=other)
        self.assertEqual(applied["status"], "verified")
        self.assertEqual(self.view()["record"]["operations"][0], original)

    def test_old_pr_unknown_does_not_block_new_pr_in_another_task(self):
        self.update_pr(body="Historical uncertainty.")
        original = deepcopy(self.view()["record"]["operations"][0])
        created = self.app.github_pull_request(
            action="create", repository="Owner/repo", title="New PR", body="Independent change",
            head="fresh", base="main", task_context=self.second_task())
        self.assertEqual(created["status"], "write_outcome_unknown")
        self.assertEqual(self.view()["record"]["operations"][0], original)
        self.provider.pull_request.assert_called()
        self.assertEqual(self.provider.pull_request.call_count, 2)

    def test_repo_lease_covers_bare_and_supervised_writers_until_publication(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def writer(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Fixture writer was not released.")
            return {"status": "write_outcome_unknown"}
        self.provider.pull_request.side_effect = writer
        def run():
            try:
                self.update_pr(labels=[])
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.fault("resource_busy", self.app.github_issue, action="update", repository="owner/REPO", number=7, body="Concurrent.")
            self.provider.issue.assert_not_called()
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.app.github_issue(action="update", repository="Owner/repo", number=7, body="Independent after publication.")
        self.assertEqual(self.provider.issue.call_count, 1)

    def test_historical_scope_witness_is_exact_additive_and_never_reclassifies(self):
        with patch.object(self.app.task_admission, "_fingerprint", wraps=self.app.task_admission._fingerprint) as fingerprint:
            self.update_pr(labels=["bug"], assignees=["person"])
            original_arguments = deepcopy(fingerprint.call_args.args[1])
        historical = self.historical_record()
        historical["format"] = 3
        historical.pop("control_revision")
        historical.pop("effect_scopes")
        historical["operations"][0].pop("effect_scope")
        historical["operations"][0]["result"].pop("effect", None)
        historical["operations"][0]["result"].pop("failure_code", None)
        # A complete old-format reader fixture, not a production recovery path.
        replace_task_record(self.state, historical)
        original = deepcopy(self.view()["record"]["operations"][0])
        self.fault("task_effect_unresolved", self.update_pr, body="Not yet scoped.")
        wrong = deepcopy(original_arguments)
        wrong["labels"] = ["different"]
        self.fault("task_operation_conflict", self.control, "bind_effect_scope", operation_id=original["id"],
                   method="github_pull_request", arguments=wrong, reason="Wrong original request is rejected.")
        malformed = [({}, "task_operation_conflict"),
            (dict(original_arguments, repository=7), "task_operation_conflict"),
            ({key: value for key, value in original_arguments.items() if key != "repository"}, "task_operation_conflict"),
            ({key: value for key, value in original_arguments.items() if key != "action"}, "task_operation_conflict"),
            (dict(original_arguments, body=7), "invalid_task_control"),
            (dict(original_arguments, body={"text": "Invalid body type."}), "invalid_task_control"),
            (dict(original_arguments, body="x" * 60000), "invalid_github_request")]
        before = self.task_bytes()
        reconciliations = self.provider.reconcile.call_count
        for arguments, code in malformed:
            with self.subTest(arguments=list(arguments), code=code):
                unchanged = deepcopy(arguments)
                self.fault(code, self.control, "bind_effect_scope", operation_id=original["id"],
                           method="github_pull_request", arguments=arguments, reason="Reject malformed original arguments.")
                self.assertEqual(arguments, unchanged)
                self.assertEqual(self.task_bytes(), before)
                self.assertEqual(self.view()["record"]["operations"][0], original)
        self.assertEqual(self.provider.pull_request.call_count, 1)
        self.assertEqual(self.provider.reconcile.call_count, reconciliations)
        proof = self.control_arguments("bind_effect_scope", operation_id=original["id"], method="github_pull_request",
                                       arguments=original_arguments, reason="Exact retained request scope; outcome remains unknown.")
        bound = self.proof_result(proof)["record"]
        self.assertEqual(bound["format"], 4)
        self.assertEqual(bound["operations"][0], original)
        self.assertEqual(bound["effect_scopes"][0]["scope"]["fields"], ["assignees", "labels"])
        self.assertEqual(self.proof_result(proof)["record"], bound)
        self.fault("task_effect_unresolved", self.update_pr, labels=[])
        self.provider.pull_request.return_value = self.verified()
        self.update_pr(body="Now independent.")
        self.assertEqual(self.view()["record"]["operations"][0], original)
        self.assertNotIn("arguments", json.loads(self.task_bytes())["effect_scopes"][0])

    def test_plan_revision_cannot_hide_an_account_switch_behind_repository_aliases(self):
        self.update_pr(body="Original unknown body.")
        record = self.view()["record"]
        original = deepcopy(record["operations"][0])
        binding = original["locator"]["connection"]
        other = dict(binding, connection_id="e" * 32, account_id=2)
        definitions = [{key: value for key, value in stage.items() if key not in {"state", "evidence"}}
                       for stage in record["plan"]["stages"]]
        before = self.task_bytes()
        for aliases in ([("Owner/repo", other), ("owner/repo", binding)],
                        [("Owner/repo", binding), ("owner/repo", other)]):
            with self.subTest(first_binding=aliases[0][1]):
                contract = deepcopy(record["contract"])
                contract.update(repositories=[key for key, _ in aliases], github_connections=dict(aliases))
                self.fault("task_effect_unresolved", self.control, "revise_plan", contract=contract,
                    stages=definitions, invalidated_stage_ids=["verify"],
                    reason="An alias cannot substitute another account for unresolved work.")
                self.assertEqual(self.task_bytes(), before)
                self.assertEqual(self.provider.pull_request.call_count, 1)

        contract["github_connections"] = {key: deepcopy(binding) for key in contract["repositories"]}
        revised = self.control("revise_plan", contract=contract, stages=definitions,
            invalidated_stage_ids=["verify"], reason="Identical aliases retain the original account.")["record"]
        self.assertEqual(revised["operations"][0], original)
        self.control("claim", handoff_id=revised["handoff"]["id"])
        self.provider.pull_request.return_value = self.verified()
        self.update_pr(title="Independent title under the retained account.")
        self.assertEqual(self.app.connections.request.call_args.kwargs["binding"], binding)
        self.assertEqual(self.provider.pull_request.call_count, 2)
        self.assertEqual(self.view()["record"]["operations"][0], original)

    def test_historical_body_timeout_scope_recovery_and_independent_stages(self):
        # Reproduce the 0.2.11 shape: the request has no persisted scope and
        # retains an unknown result after the provider loses its response.
        self.provider.pull_request.side_effect = Fault("github_response_unknown", "Response connection closed after dispatch.", effect="effect_unknown")
        with patch.object(self.app.task_admission, "_fingerprint", wraps=self.app.task_admission._fingerprint) as fingerprint:
            self.fault("github_response_unknown", self.update_pr, body="Original PR body.")
            arguments = deepcopy(fingerprint.call_args.args[1])
        historical = self.historical_record()
        historical["format"] = 3
        historical.pop("control_revision")
        historical.pop("effect_scopes")
        operation = historical["operations"][0]
        operation.pop("effect_scope")
        operation["result"].pop("effect", None)
        operation["result"].pop("failure_code", None)
        records.Record.model_validate(historical)
        replace_task_record(self.state, historical)
        original = deepcopy(operation)
        self.control("pause", reason="Retain the original unknown request.")
        self.control("resume", reason="Continue explicitly authorized independent work.")
        self.control("claim", handoff_id=self.view()["record"]["handoff"]["id"])
        self.fault("task_effect_unresolved", self.update_pr, title="Independent title before scope proof.")

        read = self.app.machines.read_workspace_file
        def changed_head(*args, **kwargs):
            value = read(*args, **kwargs)
            return dict(value, context=dict(value["context"], head_sha="f" * 40))

        with patch.object(self.app.machines, "read_workspace_file", side_effect=changed_head):
            proof = self.control_arguments("bind_effect_scope", operation_id=original["id"],
                method="github_pull_request", arguments=arguments,
                reason="Bind exact original intent while an unrelated local HEAD has changed.")
            bound = self.proof_result(proof)["record"]
            self.assertEqual(self.proof_result(proof)["record"], bound)
            self.fault("context_mismatch", self.app.write_workspace_file, **self.write_arguments())
        self.assertEqual(bound["effect_scopes"][0]["scope"]["fields"], ["body"])
        self.assertEqual(bound["operations"][0], original)
        self.assertEqual(bound["format"], 4)
        self.assertEqual(self.provider.pull_request.call_count, 1)

        self.provider.pull_request.side_effect = None
        self.provider.pull_request.return_value = self.verified()
        self.provider.reconcile.side_effect = Fault("github_connection_failed", "Original destination is temporarily offline.")
        self.update_pr(title="Independent title after scope proof.")
        self.fault("task_effect_unresolved", self.update_pr, body="Do not overwrite the unknown body.")
        self.fault("task_effect_unresolved", self.app.github_merge, repository="Owner/repo", number=7,
            expected_head_sha="e" * 40, task_context=self.context_for(uuid.uuid4().hex))
        self.provider.merge.assert_not_called()

        record = self.view()["record"]
        definitions = [{key: value for key, value in stage.items() if key not in {"state", "evidence"}}
                       for stage in record["plan"]["stages"]]
        definitions.append(dict(definitions[0], id="next", depends_on=["verify"]))
        revised = self.control("revise_plan", contract=record["contract"], stages=definitions,
            invalidated_stage_ids=[], reason="Continue independently verified local stages.")["record"]
        self.control("claim", handoff_id=revised["handoff"]["id"])
        for stage_id in ("verify", "next"):
            written = self.app.write_workspace_file(**self.write_arguments(path=stage_id + ".txt"))
            self.control("record_decision", stage_id=stage_id, outcome="accepted",
                evidence=[written["change_id"]], reason="Accept independent verified local work.")
        self.fault("task_effect_unresolved", self.control, "finish", outcome="completed",
            acceptance=[{"stage_id": stage["id"], "operation_id": stage["evidence"][0]}
                        for stage in self.view()["record"]["plan"]["stages"]], limitations="")
        self.assertEqual(self.view()["record"]["operations"][0], original)

        # The original method and ID remain an observation route, never replay.
        self.provider.reconcile.side_effect = None
        self.provider.reconcile.return_value = {"status": "write_outcome_unknown"}
        retried = self.app.github_pull_request(**arguments, task_context=self.context_for(original["id"]))
        self.assertFalse(retried["task_operation_replayed"])
        self.assertEqual(self.provider.pull_request.call_count, 2)
        self.provider.reconcile.return_value = self.verified()
        recovered = self.app.github_pull_request(**arguments, task_context=self.context_for(original["id"]))
        self.assertEqual(recovered["status"], "verified")
        self.assertEqual(self.provider.pull_request.call_count, 2)
        final = self.control("finish", outcome="completed",
            acceptance=[{"stage_id": stage["id"], "operation_id": stage["evidence"][0]}
                        for stage in self.view()["record"]["plan"]["stages"]], limitations="")["record"]
        self.assertEqual(final["terminal"]["outcome"], "completed")
        retained = tasks.read(self.state, "structured-one", operation_ids=[original["id"]])["record"]
        self.assertEqual(next(item for item in retained["operations"] if item["id"] == original["id"])["request_sha256"], original["request_sha256"])
        self.assertEqual(retained["effect_scopes"], bound["effect_scopes"])

    def test_matching_malformed_historical_scope_proof_has_a_bounded_error(self):
        with patch.object(self.app.task_admission, "_fingerprint", wraps=self.app.task_admission._fingerprint) as fingerprint:
            self.update_pr(labels=["bug"])
            original_arguments = deepcopy(fingerprint.call_args.args[1])
        historical = self.historical_record()
        operation = historical["operations"][0]
        operation.pop("effect_scope")
        context = {"task_id": historical["task_id"], "operation_id": operation["id"]}
        malformed = [{key: value for key, value in original_arguments.items() if key != "repository"},
                     dict(original_arguments, repository=[])]
        for arguments in malformed:
            with self.subTest(repository=arguments.get("repository"), number=arguments.get("number")):
                # Synthetic retained opaque hashes exercise failures after the
                # exact fingerprint guard; no real historical record is changed.
                normalized = self.app.task_admission._normalize("github_pull_request", arguments, context)
                operation["request_sha256"] = self.app.task_admission._fingerprint("github_pull_request", normalized, operation)
                replace_task_record(self.state, historical)
                before = self.task_bytes()
                original = deepcopy(operation)
                self.fault("invalid_task_control", self.control, "bind_effect_scope", operation_id=operation["id"],
                           method="github_pull_request", arguments=arguments, reason="Reject a malformed retained proof.")
                self.assertEqual(self.task_bytes(), before)
                self.assertEqual(self.view()["record"]["operations"][0], original)
        self.assertEqual(self.provider.pull_request.call_count, 1)
        self.provider.reconcile.assert_not_called()

    def test_historical_scope_retry_is_idempotent_after_verified_observation(self):
        with patch.object(self.app.task_admission, "_fingerprint", wraps=self.app.task_admission._fingerprint) as fingerprint:
            self.update_pr(labels=["bug"])
            original_arguments = deepcopy(fingerprint.call_args.args[1])
        historical = self.historical_record()
        historical["format"] = 3
        historical.pop("control_revision")
        historical.pop("effect_scopes")
        historical["operations"][0].pop("effect_scope")
        historical["operations"][0]["result"].pop("effect", None)
        historical["operations"][0]["result"].pop("failure_code", None)
        replace_task_record(self.state, historical)
        operation_id = historical["operations"][0]["id"]
        proof = self.control_arguments("bind_effect_scope", operation_id=operation_id, method="github_pull_request",
                                       arguments=original_arguments, reason="Retain exact historical scope evidence.")
        bound = self.proof_result(proof)["record"]
        self.provider.reconcile.return_value = self.verified()
        resolved = self.app.task_admission.reconcile(self.view())["record"]
        self.assertEqual(resolved["operations"][0]["result"]["status"], "succeeded")
        self.assertEqual(resolved["effect_scopes"], bound["effect_scopes"])
        self.assertEqual(resolved["last_update"], bound["last_update"])
        self.provider.reconcile.assert_called_once()
        self.provider.reconcile.reset_mock()
        before = self.task_bytes()
        retry = self.proof_result(proof)
        self.assertEqual((retry["status"], retry["record"]), ("unchanged", resolved))
        changed = deepcopy(proof)
        changed["transition"]["arguments"]["labels"] = ["different"]
        self.fault("task_conflict", self.app.control_task, **changed)
        self.fault("task_evidence_invalid", self.control, "bind_effect_scope", operation_id=operation_id,
                   method="github_pull_request", arguments=original_arguments, reason="A resolved original cannot acquire new scope.")
        self.assertEqual(self.task_bytes(), before)
        self.provider.reconcile.assert_not_called()
        self.assertEqual(self.provider.pull_request.call_count, 1)

    def test_verified_history_does_not_follow_provider_timestamps_or_read_again(self):
        self.provider.pull_request.return_value = self.verified()
        self.update_pr(body="Original verified body.")
        record = self.view()["record"]
        original = deepcopy(record["operations"][0])
        self.provider.reconcile.return_value = {"status": "verified", "receipt": {"number": 7, "updated_at": "bot-later"}}
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[original["id"]],
                     reason="Original provider receipt is sufficient historical evidence.")
        self.assertEqual(self.view()["record"]["operations"][0], original)
        self.provider.reconcile.assert_not_called()

    def test_unproven_current_snapshot_preserves_original_failure_receipt(self):
        self.provider.pull_request.side_effect = Fault("github_response_unknown", "Dispatched reply lost.", effect="effect_unknown")
        self.fault("github_response_unknown", self.update_pr, labels=[])
        original = deepcopy(self.view()["record"]["operations"][0])
        for status in ("observed", "unverified", "write_outcome_unknown", "merge_outcome_unknown"):
            with self.subTest(status=status):
                self.provider.reconcile.return_value = {"status": status, "receipt": {"number": 7, "updated_at": "bot-later"}}
                self.app.task_admission.reconcile(self.view())
                self.assertEqual(self.view()["record"]["operations"][0], original)

    def test_observation_revision_allows_independent_admission_but_control_change_does_not(self):
        self.update_pr(labels=[])
        context = self.context_for(uuid.uuid4().hex)
        operation = self.view()["record"]["operations"][0]
        result = dict(operation["result"], summary="Original uncertainty observed with durable receipt evidence.")
        self.app.task_admission._publish("structured-one", [(operation["id"], result, operation["locator"])])
        observed = self.view()["record"]
        self.assertGreater(observed["revision"], context["task_revision"])
        self.assertLessEqual(observed["control_revision"], context["task_revision"])
        self.provider.pull_request.return_value = self.verified()
        self.app.github_pull_request(action="update", repository="Owner/repo", number=7, body="Independent.", task_context=context)
        stale = self.context_for(uuid.uuid4().hex)
        self.control("pause", reason="Material control supersedes admission.")
        self.fault("task_conflict", self.app.github_pull_request, action="update", repository="Owner/repo", number=8,
                   body="Not admitted.", task_context=stale)

    def test_cancellation_can_publish_partial_workspace_evidence_without_false_completion(self):
        with patch.object(self.app.machines, "write_workspace_file", return_value={"status": "outcome_unknown"}):
            self.app.write_workspace_file(**self.write_arguments())
        operation = self.view()["record"]["operations"][0]
        self.control("cancel", reason="Retain the original uncertain file outcome.")
        partial = {"status": "partial", "change_id": operation["locator"]["change_id"], "effects": [{"path": "result.txt"}]}
        with patch.object(self.app.machines, "read_workspace_change", return_value=partial):
            observed = self.app.task_admission.reconcile(self.view())["record"]
        self.assertEqual(observed["operations"][0]["result"]["status"], "partial")
        self.assertEqual(observed["control"]["stop_intent"], "cancel")
        self.assertIsNone(observed["terminal"])


if __name__ == "__main__":
    unittest.main()
