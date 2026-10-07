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
from openmachine.foundation.sanitize import Fault
from openmachine.tasks import records
from test.support.tasks import TaskFixture, task_contract


class ScopedSupervisionTests(TaskFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.provider = SimpleNamespace(DEFINITE_FAILURES=github.DEFINITE_FAILURES,
            read=Mock(return_value={"configured_write": True, "data": {"permissions": {"push": True}}}),
            reconcile=Mock(return_value={"status": "write_outcome_unknown"}))
        for name in ("issue", "pull_request", "comment", "review", "merge"):
            setattr(self.provider, name, Mock(return_value={"status": "write_outcome_unknown"}))
        self.app.github = self.provider
        self.app.connections.select = Mock(return_value=SimpleNamespace(binding=lambda: {
            "connection_id": "d" * 32, "account_id": 1, "authorization_revision": 1}))
        self.app.connections.request = Mock()
        self.request["contract"]["repositories"] = ["Owner/repo"]
        self.accept_plan(tools=["github_issue", "github_pull_request", "github_comment", "github_review", "github_merge",
                               "write_workspace_file", "start_execution", "read_task", "control_task"])

    def update_pr(self, **values):
        return self.app.github_pull_request(action="update", repository="Owner/repo", number=7,
            expected_head_sha="e" * 40, task_context=self.context_for(uuid.uuid4().hex), **values)

    def verified(self, number=7):
        return {"status": "verified", "receipt": {"id": 71, "number": number, "updated_at": "original"}}

    def second_task(self):
        request = task_contract(self.context, "second-task")
        request["contract"] = deepcopy(self.request["contract"])
        created = self.app.create_task(**request)["record"]
        claimed = self.app.control_task(task_id="second-task", expected_revision=created["revision"],
            expected_plan_revision=created["plan"]["revision"], expected_epoch=created["control"]["epoch"],
            update_id=uuid.uuid4().hex, transition={"action": "claim", "handoff_id": created["handoff"]["id"]})["record"]
        return {"task_id": "second-task", "task_revision": claimed["revision"], "plan_revision": claimed["plan"]["revision"],
                "continuation_epoch": claimed["control"]["epoch"], "operation_id": uuid.uuid4().hex}

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
        self.assertEqual(len(self.app.read_task("second-task")["record"]["operations"]), 1)

    def test_bare_creation_with_missing_locator_number_still_scans_pending(self):
        self.app.github_pull_request(action="create", repository="Owner/repo", title="Original", body="Original",
            head="topic", base="main", task_context=self.context_for(uuid.uuid4().hex))
        self.fault("task_effect_unresolved", self.app.github_pull_request, action="create", repository="Owner/repo",
                   title="Duplicate", body="Duplicate", head="another", base="main")
        self.assertEqual(self.provider.pull_request.call_count, 1)

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
        historical = self.view()["record"]
        historical["format"] = 3
        historical.pop("control_revision")
        historical.pop("effect_scopes")
        historical["operations"][0].pop("effect_scope")
        historical["operations"][0]["result"].pop("effect", None)
        historical["operations"][0]["result"].pop("failure_code", None)
        # A complete old-format reader fixture, not a production recovery path.
        self.path.write_bytes(records.canonical(historical))
        self.app.rebuild_task_index()
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
        before = self.path.read_bytes()
        reconciliations = self.provider.reconcile.call_count
        for arguments, code in malformed:
            with self.subTest(arguments=list(arguments), code=code):
                unchanged = deepcopy(arguments)
                self.fault(code, self.control, "bind_effect_scope", operation_id=original["id"],
                           method="github_pull_request", arguments=arguments, reason="Reject malformed original arguments.")
                self.assertEqual(arguments, unchanged)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.view()["record"]["operations"][0], original)
        self.assertEqual(self.provider.pull_request.call_count, 1)
        self.assertEqual(self.provider.reconcile.call_count, reconciliations)
        proof = self.control_arguments("bind_effect_scope", operation_id=original["id"], method="github_pull_request",
                                       arguments=original_arguments, reason="Exact retained request scope; outcome remains unknown.")
        bound = self.app.control_task(**proof)["record"]
        self.assertEqual(bound["format"], 4)
        self.assertEqual(bound["operations"][0], original)
        self.assertEqual(bound["effect_scopes"][0]["scope"]["fields"], ["assignees", "labels"])
        self.assertEqual(self.app.control_task(**proof)["record"], bound)
        self.fault("task_effect_unresolved", self.update_pr, labels=[])
        self.provider.pull_request.return_value = self.verified()
        self.update_pr(body="Now independent.")
        self.assertEqual(self.view()["record"]["operations"][0], original)
        self.assertNotIn("arguments", json.loads(self.path.read_bytes())["effect_scopes"][0])

    def test_historical_body_timeout_scope_recovery_and_independent_stages(self):
        # Reproduce the 0.2.11 shape: the request has no persisted scope and
        # retains an unknown result after the provider loses its response.
        self.provider.pull_request.side_effect = Fault("github_response_unknown", "Response connection closed after dispatch.", effect="effect_unknown")
        with patch.object(self.app.task_admission, "_fingerprint", wraps=self.app.task_admission._fingerprint) as fingerprint:
            self.fault("github_response_unknown", self.update_pr, body="Original PR body.")
            arguments = deepcopy(fingerprint.call_args.args[1])
        historical = self.view()["record"]
        historical["format"] = 3
        historical.pop("control_revision")
        historical.pop("effect_scopes")
        operation = historical["operations"][0]
        operation.pop("effect_scope")
        operation["result"].pop("effect", None)
        operation["result"].pop("failure_code", None)
        records.Record.model_validate(historical)
        self.path.write_bytes(records.canonical(historical))
        self.app.rebuild_task_index()
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
            bound = self.app.control_task(**proof)["record"]
            self.assertEqual(self.app.control_task(**proof)["record"], bound)
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
        self.assertEqual(final["operations"][0]["request_sha256"], original["request_sha256"])
        self.assertEqual(final["effect_scopes"], bound["effect_scopes"])

    def test_matching_malformed_historical_scope_proof_has_a_bounded_error(self):
        with patch.object(self.app.task_admission, "_fingerprint", wraps=self.app.task_admission._fingerprint) as fingerprint:
            self.update_pr(labels=["bug"])
            original_arguments = deepcopy(fingerprint.call_args.args[1])
        historical = self.view()["record"]
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
                self.path.write_bytes(records.canonical(historical))
                self.app.rebuild_task_index()
                before = self.path.read_bytes()
                original = deepcopy(operation)
                self.fault("invalid_task_control", self.control, "bind_effect_scope", operation_id=operation["id"],
                           method="github_pull_request", arguments=arguments, reason="Reject a malformed retained proof.")
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.view()["record"]["operations"][0], original)
        self.assertEqual(self.provider.pull_request.call_count, 1)
        self.provider.reconcile.assert_not_called()

    def test_historical_scope_retry_is_idempotent_after_verified_observation(self):
        with patch.object(self.app.task_admission, "_fingerprint", wraps=self.app.task_admission._fingerprint) as fingerprint:
            self.update_pr(labels=["bug"])
            original_arguments = deepcopy(fingerprint.call_args.args[1])
        historical = self.view()["record"]
        historical["format"] = 3
        historical.pop("control_revision")
        historical.pop("effect_scopes")
        historical["operations"][0].pop("effect_scope")
        historical["operations"][0]["result"].pop("effect", None)
        historical["operations"][0]["result"].pop("failure_code", None)
        self.path.write_bytes(records.canonical(historical))
        self.app.rebuild_task_index()
        operation_id = historical["operations"][0]["id"]
        proof = self.control_arguments("bind_effect_scope", operation_id=operation_id, method="github_pull_request",
                                       arguments=original_arguments, reason="Retain exact historical scope evidence.")
        bound = self.app.control_task(**proof)["record"]
        self.provider.reconcile.return_value = self.verified()
        resolved = self.app.task_admission.reconcile(self.view())["record"]
        self.assertEqual(resolved["operations"][0]["result"]["status"], "succeeded")
        self.assertEqual(resolved["effect_scopes"], bound["effect_scopes"])
        self.assertEqual(resolved["last_update"], bound["last_update"])
        self.provider.reconcile.assert_called_once()
        self.provider.reconcile.reset_mock()
        before = self.path.read_bytes()
        retry = self.app.control_task(**proof)
        self.assertEqual((retry["status"], retry["record"]), ("unchanged", resolved))
        changed = deepcopy(proof)
        changed["transition"]["arguments"]["labels"] = ["different"]
        self.fault("task_conflict", self.app.control_task, **changed)
        self.fault("task_evidence_invalid", self.control, "bind_effect_scope", operation_id=operation_id,
                   method="github_pull_request", arguments=original_arguments, reason="A resolved original cannot acquire new scope.")
        self.assertEqual(self.path.read_bytes(), before)
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
