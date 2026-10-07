"""Tests for github behavior at the unit layer."""

import base64
import copy
import hashlib
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from integrations import github
from integrations.github import client, connection, evidence, operations
from openmachine.foundation.sanitize import Fault
from test.support.github_cases import ACTOR, BASE, CREATED, GitHubCaseFixture, HEAD, MERGE, POSTED, REPO, ROOT, STAMP, TREE, settings


class GitHubTests(GitHubCaseFixture, unittest.TestCase):
    def test_task_publication_reconciliation_reads_original_destination_only(self):
        locator = self.task_locator("issue")
        body = "Retained fixture request.\n\n<!-- " + locator["anchor"] + " -->"
        self.issue_value.update(title="Fixture", body=body + "\n\n" + operations.CREATED_FOOTER, user=ACTOR)
        request = dict(action="create", title="Fixture", body=body, state=None)
        result = github.reconcile(settings(), locator, request)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["receipt"]["number"], 1)
        self.assertTrue(result["fields_verified"])
        self.assertTrue(all(call.args[0] == "GET" for call in self.transport.call_args_list))
        self.assertEqual(github.reconcile(settings(), locator, dict(request, title="Changed"))["status"], "observed")
        self.assertEqual(github.reconcile(settings(), dict(locator, number=1))["status"], "observed")

    def test_task_publication_search_is_bounded_and_duplicate_or_partial_coverage_is_unknown(self):
        locator = self.task_locator("issue")
        found = dict(self.issue_value, body="<!-- " + locator["anchor"] + " -->", user=ACTOR)
        with patch.object(client, "list_page", return_value=([found, dict(found, number=3)], None)):
            result = github.reconcile(settings(), locator)
            self.assertEqual(result["status"], "write_outcome_unknown")
            self.assertEqual(result["matching_destinations"], 2)
        with patch.object(client, "list_page", side_effect=[([found], 2), ([], 3), ([], 4)]) as pages:
            result = github.reconcile(settings(), locator)
            self.assertEqual(pages.call_count, 3)
            self.assertEqual(result["coverage"], "incomplete")
            self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertTrue(all(call.args[0] == "GET" for call in self.transport.call_args_list))

    def test_task_comment_review_and_pr_reconciliation_keep_type_parent_and_head(self):
        for kind, number, parent in (("comment", 9, 1), ("review", 9, 2), ("pull_request", 2, None)):
            locator = self.task_locator(kind, number, parent)
            body = "Fixture.\n\n<!-- " + locator["anchor"] + " -->"
            request = dict(action="create", body=body)
            if kind == "pull_request":
                self.pr.update(body=body, title="Fixture")
                request.update(title="Fixture", head="topic", base="main", draft=False, expected_head_sha=HEAD)
                result = github.reconcile(settings(), locator, request)
            else:
                value = dict(self.reviews[0], body=body, issue_url=f"https://api.github.com/{ROOT}/issues/1")
                endpoint = f"{ROOT}/issues/comments/9" if kind == "comment" else f"{ROOT}/pulls/2/reviews/9"
                if kind == "review":
                    request.update(expected_head_sha=HEAD, event="COMMENT")
                def response(method, target, payload=None):
                    if method == "GET" and target == endpoint:
                        return copy.deepcopy(value)
                    return self.response(method, target, payload)
                with patch.object(connection, "request", side_effect=response) as transport:
                    result = github.reconcile(settings(), locator, request)
                    self.assertTrue(all(call.args[0] == "GET" for call in transport.call_args_list))
                    if kind == "comment":
                        value["issue_url"] = f"https://api.github.com/{ROOT}/issues/99"
                        self.assert_fault("github_wrong_object_type", github.reconcile, settings(), locator, request)
            self.assertEqual(result["status"], "verified", kind)
        unknown = github.reconcile(settings(), self.task_locator("review", 9))
        self.assertEqual(unknown["status"], "write_outcome_unknown")
        self.assertFalse(any("/None/" in call.args[1] for call in self.transport.call_args_list))
        self.assertEqual(github.reconcile(settings(), self.task_locator("pull_request", 2),
            dict(request, expected_head_sha=BASE))["status"], "observed")

    def test_task_merge_reconciliation_proves_original_head_and_destination_without_put(self):
        self.pr.update(merged=True, merge_commit_sha=MERGE)
        locator = self.task_locator("merge", 2)
        request = dict(expected_head_sha=HEAD, commit_title="Fixture merge", commit_message="Fixture body")
        comparison = {"status": "ahead", "merge_base_commit": {"sha": MERGE}}
        def response(method, target, payload=None):
            if method == "GET":
                values = {f"{ROOT}/commits/{MERGE}": {"sha": MERGE, "commit": {"message": "Fixture merge\n\nFixture body\n"}},
                    f"{ROOT}/branches/main": {"commit": {"sha": TREE}},
                    f"{ROOT}/compare/{MERGE}...{TREE}": comparison}
                if target in values:
                    return copy.deepcopy(values[target])
            return self.response(method, target, payload)
        with patch.object(connection, "request", side_effect=response) as transport:
            result = github.reconcile(settings(), locator, request)
            self.assertEqual(result["status"], "merged_verified")
            self.assertEqual(result["receipt"], {"number": 2, "head_sha": HEAD, "merge_commit_sha": MERGE})
            self.assertEqual(github.reconcile(settings(), locator)["status"], "observed")
            comparison["merge_base_commit"]["sha"] = BASE
            self.assertEqual(github.reconcile(settings(), locator, request)["status"], "write_outcome_unknown")
            self.assertTrue(all(call.args[0] == "GET" for call in transport.call_args_list))
        self.assert_fault("github_candidate_changed", github.reconcile, settings(), locator, dict(request, expected_head_sha=BASE))

    def test_task_reconciliation_rejects_forged_locator_and_changed_account(self):
        for invalid in (self.task_locator("issue", True), dict(self.task_locator("issue", 1), anchor="../../unsafe")):
            self.assert_fault("invalid_github_request", github.reconcile, settings(), invalid)
        self.assertEqual(self.transport.call_count, 0)
        locator = self.task_locator("issue", 1)
        actor = dict(ACTOR, id=999)
        with patch.object(connection, "read_actor", side_effect=[ACTOR, actor]):
            self.assert_fault("github_identity_changed", github.reconcile, settings(), locator)

    def test_attribution_has_only_the_two_fixed_footer_literals(self):
        self.assertEqual(operations.ATTRIBUTION_URL, "https://github.com/OpenMachineLabs/openmachine")
        self.assertEqual(operations.CREATED_FOOTER, CREATED)
        self.assertEqual(operations.POSTED_FOOTER, POSTED)
        self.assertEqual(operations.FOOTERS, (CREATED, POSTED))

    def test_all_new_bodies_share_the_default_on_boolean_policy(self):
        calls = (
            (github.issue, ("title", "body"), {}, CREATED),
            (github.pull_request, ("pr", "body"), {"head": "topic", "base": "main"}, CREATED),
            (github.comment, ("body",), {"number": 1}, POSTED),
            (github.review, ("body",), {"number": 2, "expected_head_sha": HEAD}, POSTED),
        )
        for private in (False, True):
            self.metadata["private"] = private
            for enabled in (None, True, False):
                config = settings(enabled)
                self.assertIs(config.github.attribution, enabled is not False)
                for operation, args, kwargs, footer in calls:
                    with self.subTest(private=private, enabled=enabled, operation=operation.__name__):
                        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
                            operation(config, "create", REPO, *args, **kwargs)
                        expected = "body" if enabled is False else "body\n\n" + footer
                        self.assertEqual(mutate.call_args.args[3]["body"], expected)

    def test_all_updates_preserve_provenance_without_retroactive_branding(self):
        calls = (
            (github.issue, (None, "edited"), {"number": 1}, CREATED, self.issue_value),
            (github.pull_request, (None, "edited"), {"number": 2}, CREATED, self.pr),
            (github.comment, ("edited",), {"comment_id": 9}, POSTED, self.issue_value),
            (github.review, ("edited",), {"number": 2, "review_id": 9}, POSTED, self.reviews[0]),
        )
        for enabled in (True, False):
            for attributed in (False, True):
                for operation, args, kwargs, footer, current in calls:
                    current["body"] = "old\n\n" + footer if attributed else "old"
                    with self.subTest(enabled=enabled, attributed=attributed, operation=operation.__name__):
                        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
                            operation(settings(enabled), "update", REPO, *args, **kwargs)
                        expected = "edited\n\n" + footer if attributed else "edited"
                        self.assertEqual(mutate.call_args.args[3]["body"], expected)

    def test_exact_footer_is_idempotent_and_whitespace_is_preserved(self):
        body = "body\n\n" + operations.CREATED_FOOTER
        self.assertEqual(operations._append(body, operations.CREATED_FOOTER), body)
        self.assertEqual(operations._append("body\n", operations.CREATED_FOOTER), "body\n\n" + operations.CREATED_FOOTER)
        self.assertEqual(operations._append("body\n\n", operations.CREATED_FOOTER), "body\n\n" + operations.CREATED_FOOTER)

    def test_update_preserves_existing_footer_when_replacement_has_another(self):
        self.issue_value["body"] = "old\n\n" + CREATED
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            github.comment(settings(True), "update", REPO, POSTED, comment_id=9)
        self.assertEqual(mutate.call_args.args[3]["body"], POSTED + "\n\n" + CREATED)

    def test_review_summary_is_bound_to_exact_head_without_inline_comments(self):
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            github.review(settings(), "create", REPO, "review", number=2, expected_head_sha=HEAD)
        payload = mutate.call_args.args[3]
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertNotIn("comments", payload)

    def test_mutation_requires_matching_destination_readback(self):
        expected = "body\n\n" + operations.CREATED_FOOTER
        with patch.object(connection, "request", side_effect=[{"number": 3}, {"number": 3, "body": expected}]):
            result = operations._mutate(REPO, "POST", f"{ROOT}/issues", {"body": expected}, lambda value: f"{ROOT}/issues/{value['number']}")
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["body_verified"])
        with patch.object(connection, "request", side_effect=[{"number": 3}, {"number": 3, "body": "different"}]):
            result = operations._mutate(REPO, "POST", f"{ROOT}/issues", {"body": expected}, lambda value: f"{ROOT}/issues/{value['number']}")
        self.assertEqual(result["status"], "write_unverified")

    def test_unconfigured_repository_fails_closed(self):
        self.assert_fault("github_not_allowed", github.issue, settings(True), "create", "Other/repo", "title", "body")
        self.transport.assert_not_called()

    def test_read_scope_never_mutates_or_widens_write_scope(self):
        config = settings(read_owners=["Owner"], write_repositories=["Other/project"])
        before = config.model_dump()
        for _ in range(2):
            connection.authorize(config, "owner/repo", write=False)
            self.assert_fault("github_not_allowed", connection.authorize, config, REPO)
        self.assertEqual(config.model_dump(), before)
        connection.authorize(config, "Other/project")
        connection.authorize(settings(), REPO, write=False)
        connection.authorize(settings(), REPO)
        for bad in ("Owner/../repo", "Owner/..", "Owner/repo?x=y"):
            self.assert_fault("invalid_github_request", connection.authorize, config, bad)
        self.transport.assert_not_called()

    def test_legacy_and_explicit_grants_cannot_be_mixed(self):
        with self.assertRaises(ValidationError):
            settings(repositories=[REPO], read_repositories=[REPO])
        config = settings(write_owners=["Owner"])
        connection.authorize(config, REPO)
        connection.authorize(config, REPO, write=False)
        self.assert_fault("github_not_allowed", connection.authorize, config, "Owner-other/repo")

    def test_account_and_repository_identity_do_not_fall_back(self):
        config = settings(repositories=[REPO], account="SomeoneElse")
        self.assert_fault("github_identity_changed", github.read, config, "status")
        self.assertEqual(self.transport.call_count, 1)
        self.metadata["full_name"] = "Other/transferred"
        self.assert_fault("github_repository_changed", github.read, settings(), "repository", REPO)

    def test_stale_update_and_pushed_head_reject_before_write(self):
        with patch.object(operations, "_mutate") as mutate:
            self.assert_fault("github_object_changed", github.issue, settings(), "update", REPO, "new", None,
                              number=1, expected_updated_at="old")
            self.assert_fault("github_candidate_changed", github.pull_request, settings(), "create", REPO,
                              "new", "body", head="topic", base="main", expected_head_sha=BASE)
            self.assert_fault("invalid_github_request", github.pull_request, settings(), "create", REPO,
                              "new", "body", head="topic", base="main", expected_head_sha="invalid")
            mutate.assert_not_called()
            github.issue(settings(), "update", REPO, None, None, number=1, state="closed", expected_updated_at=STAMP)
            self.assertEqual(mutate.call_args.args[3], {"state": "closed"})

    def test_issue_update_rejects_pull_request_before_mutation(self):
        self.issue_value["pull_request"] = {"url": "https://api.github.com/repos/Owner/repo/pulls/1"}
        with patch.object(operations, "_mutate") as mutate:
            self.assert_fault("github_wrong_object_type", github.issue, settings(), "update", REPO,
                              None, None, number=1, state="closed", expected_updated_at=STAMP)
            mutate.assert_not_called()

    def test_issue_and_pr_metadata_use_bounded_verified_issue_fields(self):
        def mutating(method, endpoint, payload=None):
            if method == "PATCH" and endpoint == f"{ROOT}/issues/1":
                self.assertEqual(payload, {"labels": [], "assignees": ["Operator"]})
                self.issue_value.update(labels=[], assignees=[ACTOR])
                return copy.deepcopy(self.issue_value)
            if method == "PATCH" and endpoint == f"{ROOT}/issues/2":
                self.assertEqual(payload, {"labels": ["maintenance"], "assignees": ["Operator"]})
                self.pr.update(labels=[{"id": 7, "name": "maintenance"}], assignees=[ACTOR])
                return copy.deepcopy(self.pr)
            return self.response(method, endpoint, payload)
        with patch.object(connection, "request", side_effect=mutating) as api:
            issue = github.issue(settings(), "update", REPO, None, None, number=1,
                                 expected_updated_at=STAMP, labels=[], assignees=["Operator"])
            pr = github.pull_request(settings(), "update", REPO, None, None, number=2,
                                     expected_updated_at=STAMP, expected_head_sha=HEAD,
                                     labels=["maintenance"], assignees=["Operator"])
            observed = github.read(settings(), "pull_request", REPO, number=2)
        self.assertEqual((issue["status"], pr["status"]), ("verified", "verified"))
        writes = [call for call in api.call_args_list if call.args[0] == "PATCH"]
        self.assertEqual([call.args[1] for call in writes], [f"{ROOT}/issues/1", f"{ROOT}/issues/2"])
        self.assertEqual(observed["data"]["labels"], ["maintenance"])
        self.assertEqual(observed["data"]["assignees"], [ACTOR])

    def test_pr_metadata_stays_one_effect_and_validates_names(self):
        with patch.object(operations, "_mutate") as mutate:
            self.assert_fault("invalid_github_request", github.pull_request, settings(), "create", REPO,
                              "new", "body", head="topic", base="main", labels=["maintenance"])
            self.assert_fault("invalid_github_request", github.pull_request, settings(), "update", REPO,
                              "new", None, number=2, expected_head_sha=HEAD, labels=["maintenance"])
            self.assert_fault("invalid_github_request", github.issue, settings(), "create", REPO,
                              "new", "body", labels=["duplicate", "DUPLICATE"])
            mutate.assert_not_called()

    def test_metadata_readback_and_reconciliation_compare_named_sets(self):
        observed = {"id": 9, "number": 2, "labels": [{"name": "Maintenance"}],
                    "assignees": [{"login": "operator"}]}
        with patch.object(connection, "request", side_effect=[observed, observed]):
            result = operations._mutate(REPO, "PATCH", "endpoint",
                {"labels": ["maintenance"], "assignees": ["Operator"]}, lambda _: "readback")
        self.assertEqual(result["status"], "verified")
        observed["labels"] = [{"name": "bug"}]
        with patch.object(connection, "request", side_effect=[observed, observed]):
            result = operations._mutate(REPO, "PATCH", "endpoint",
                {"labels": ["maintenance"]}, lambda _: "readback")
        self.assertEqual(result["status"], "write_unverified")

        locator = self.task_locator("pull_request", 2)
        self.pr.update(labels=[{"name": "maintenance"}], assignees=[ACTOR])
        request = dict(action="update", labels=["maintenance"], assignees=["operator"],
                       expected_head_sha=HEAD)
        reconciled = github.reconcile(settings(), locator, request)
        self.assertEqual(reconciled["status"], "verified")
        self.assertTrue(reconciled["fields_verified"])

    def test_readback_verifies_all_fields_review_commit_and_actor(self):
        cases = [({"title": "new", "body": "same"}, {"title": "old", "body": "same"}),
                 ({"state": "closed"}, {"state": "open"}),
                 ({"commit_id": HEAD, "event": "COMMENT"}, {"commit_id": BASE, "state": "COMMENTED"})]
        for payload, observed in cases:
            with self.subTest(payload=payload), patch.object(connection, "request", side_effect=[{"id": 9}, {"id": 9, **observed}]):
                result = operations._mutate(REPO, "POST", "endpoint", payload, lambda _: "readback")
                self.assertEqual(result["status"], "write_unverified")
        with patch.object(connection, "request", side_effect=[{"id": 9}, {"id": 9, "body": "same"}, {"id": 2, "login": "Other"}]):
            result = operations._mutate(REPO, "POST", "endpoint", {"body": "same"}, lambda _: "readback", settings=settings(), actor=ACTOR)
        self.assertEqual(result["status"], "write_unverified")

    def test_lost_comment_acknowledgement_is_read_not_replayed(self):
        writes = []
        def lossy(method, endpoint, payload=None):
            if method == "POST" and endpoint == f"{ROOT}/issues/1/comments":
                writes.append(endpoint)
                self.comments.append({"id": 9, "body": payload["body"], "user": ACTOR})
                raise Fault("github_timeout", "synthetic lost acknowledgement")
            return self.response(method, endpoint, payload)
        with patch.object(connection, "request", side_effect=lossy):
            result = github.comment(settings(False), "create", REPO, "unique-test-comment", number=1)
            self.assertEqual(result["status"], "write_outcome_unknown")
            observed = github.read(settings(), "comments", REPO, number=1)
        self.assertEqual(len(writes), 1)
        self.assertEqual([item["body"] for item in observed["data"]], ["unique-test-comment"])

    def test_known_rejection_is_not_an_unknown_write_or_retry(self):
        with patch.object(connection, "request", side_effect=Fault("github_access_denied", "denied")) as api:
            self.assert_fault("github_access_denied", operations._mutate, REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback")
        self.assertEqual(api.call_count, 1)

    def test_candidate_pages_require_both_revisions_and_report_missing_patch(self):
        first = github.read(settings(), "files", REPO, number=2, expected_head_sha=HEAD, limit=1)
        self.assertEqual(first["next_page"], 2)
        self.assertFalse(first["data"][0]["patch_is_complete_evidence"])
        self.assert_fault("invalid_github_request", github.read, settings(), "files", REPO, number=2,
                          expected_head_sha=HEAD, page=2, limit=1)
        second = github.read(settings(), "files", REPO, number=2, expected_head_sha=HEAD, expected_base_sha=BASE, page=2, limit=1)
        self.assertFalse(second["has_more"])
        self.assertFalse(second["data"][0]["patch_available"])
        self.pr["head"]["sha"] = MERGE
        self.assert_fault("github_candidate_changed", github.read, settings(), "files", REPO, number=2,
                          expected_head_sha=HEAD, expected_base_sha=BASE, page=2, limit=1)

    def test_missing_file_page_and_gate_capacity_are_explicit(self):
        self.files = self.files[:1]
        self.assert_fault("github_evidence_unavailable", github.read, settings(), "files", REPO, number=2, expected_head_sha=HEAD)
        self.reviews = [dict(self.reviews[0], id=i + 1) for i in range(1001)]
        self.assert_fault("github_evidence_unavailable", evidence.merge_gate, REPO, 2, HEAD)

    def test_zero_statuses_are_no_checks_not_pending(self):
        observed = evidence.merge_gate(REPO, 2, HEAD)
        self.assertEqual(observed["checks"]["state"], "no_checks")
        self.assertEqual(observed["blockers"], [])
        self.assertFalse(observed["self_review_is_independent_approval"])

    def test_superseded_cancelled_check_does_not_block_current_success(self):
        run = {"id": 1, "name": "test", "app": {"id": 42}, "head_sha": HEAD, "status": "completed", "conclusion": "cancelled"}
        self.runs = [run, dict(run, id=2, conclusion="success")]
        observed = evidence.merge_gate(REPO, 2, HEAD)
        self.assertEqual(observed["checks"]["state"], "passed")
        self.assertEqual([item["id"] for item in observed["checks"]["check_runs"]], [2])
        self.runs[1]["conclusion"] = "cancelled"
        self.assertIn("current_checks_not_passed", evidence.merge_gate(REPO, 2, HEAD)["blockers"])

    def test_review_pagination_and_unresolved_outdated_thread_block_merge(self):
        self.reviews = [dict(self.reviews[0], id=i + 1) for i in range(101)]
        self.threads = [{"id": "thread", "isResolved": False, "isOutdated": True, "path": "a.py", "line": 1}]
        observed = evidence.merge_gate(REPO, 2, HEAD)
        self.assertEqual(observed["review_count"], 101)
        self.assertIn("unresolved_review_threads", observed["blockers"])
        result = github.merge(settings(), REPO, 2, HEAD, BASE)
        self.assertEqual(result["status"], "merge_blocked")
        self.assertFalse(any(call.args[0] == "PUT" for call in self.transport.call_args_list))

    def test_comment_does_not_clear_prior_change_request(self):
        self.reviews = [dict(self.reviews[0], id=8, state="CHANGES_REQUESTED"), self.reviews[0]]
        self.assertIn("review_requirements_not_met", evidence.merge_gate(REPO, 2, HEAD)["blockers"])
        self.reviews.append(dict(self.reviews[0], id=10, state="DISMISSED"))
        self.assertNotIn("review_requirements_not_met", evidence.merge_gate(REPO, 2, HEAD)["blockers"])
        self.policy["reviewDecision"] = "REVIEW_REQUIRED"
        self.assertIn("review_requirements_not_met", evidence.merge_gate(REPO, 2, HEAD)["blockers"])

    def test_missing_graphql_evidence_rejects_review_without_submission(self):
        def partial(method, endpoint, payload=None):
            if endpoint == "graphql":
                return {"data": {}, "errors": [{"message": "not available"}]}
            return self.response(method, endpoint, payload)
        with patch.object(connection, "request", side_effect=partial), patch.object(operations, "_mutate") as mutate:
            self.assert_fault("github_evidence_unavailable", github.review, settings(), "create", REPO, "review",
                              number=2, expected_head_sha=HEAD)
            mutate.assert_not_called()

    def test_changed_candidate_self_approval_and_absent_review_cannot_merge(self):
        with patch.object(operations, "_mutate") as mutate:
            self.assert_fault("github_candidate_changed", github.review, settings(), "create", REPO, "review",
                              number=2, expected_head_sha=BASE)
            self.assert_fault("github_self_review", github.review, settings(), "create", REPO, "review",
                              number=2, expected_head_sha=HEAD, event="APPROVE")
            mutate.assert_not_called()
        self.reviews = []
        self.assertIn("no_submitted_current_head_review", github.merge(settings(), REPO, 2, HEAD)["gate"]["blockers"])
        self.reviews = [{"id": 9, "user": ACTOR, "state": "COMMENTED", "commit_id": HEAD, "submitted_at": STAMP}]
        self.policy["mergeQueueEntry"] = {"id": "queue"}
        self.assertIn("native_policy_not_clean_or_queue_unsupported", github.merge(settings(), REPO, 2, HEAD)["gate"]["blockers"])

    def test_merge_verifies_actual_destination_and_never_bypasses(self):
        writes = []
        def merging(method, endpoint, payload=None):
            if method == "PUT":
                self.assertEqual(endpoint, f"{ROOT}/pulls/2/merge")
                self.assertEqual(payload, {"sha": HEAD, "merge_method": "squash"})
                writes.append(payload)
                self.pr.update(merged=True, merge_commit_sha=MERGE, state="closed")
                return {"merged": True, "sha": MERGE}
            if endpoint == f"{ROOT}/commits/{MERGE}":
                return {"sha": MERGE}
            if endpoint == f"{ROOT}/branches/main":
                return {"commit": {"sha": MERGE}}
            if endpoint == f"{ROOT}/compare/{MERGE}...{MERGE}":
                return {"status": "identical", "merge_base_commit": {"sha": MERGE}}
            return self.response(method, endpoint, payload)
        with patch.object(connection, "request", side_effect=merging):
            result = github.merge(settings(), REPO, 2, HEAD, BASE)
        self.assertEqual(result["status"], "merged_verified")
        self.assertEqual(result["merge_commit_sha"], MERGE)
        self.assertEqual(len(writes), 1)

    def test_merge_unknown_and_unverified_are_not_success(self):
        for behavior, expected in (("timeout", "merge_outcome_unknown"), ("wrong_destination", "merge_unverified")):
            self.pr.update(state="open", merged=False)
            writes = []
            def uncertain(method, endpoint, payload=None):
                if method == "PUT":
                    writes.append(payload)
                    if behavior == "timeout":
                        raise Fault("github_timeout", "lost acknowledgement")
                    self.pr.update(merged=True, merge_commit_sha=BASE, state="closed")
                    return {"merged": True, "sha": MERGE}
                return self.response(method, endpoint, payload)
            with self.subTest(behavior=behavior), patch.object(connection, "request", side_effect=uncertain):
                result = github.merge(settings(), REPO, 2, HEAD)
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(writes), 1)

    def test_fixed_blob_unicode_pages_and_symlink_rejection(self):
        raw = ("x" * 16383 + "\u00e9tail").encode()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        values = [{"sha": HEAD, "commit": {"tree": {"sha": TREE}}},
                  {"sha": TREE, "truncated": False, "tree": [{"path": "code.py", "mode": "100644", "sha": blob}]},
                  {"sha": blob, "encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode()}]
        with patch.object(connection, "request", side_effect=copy.deepcopy(values) * 2):
            first = evidence.read_file(REPO, HEAD, "code.py", 0)
            second = evidence.read_file(REPO, HEAD, "code.py", first["next_offset"])
        self.assertEqual((first["text"] + second["text"]).encode(), raw)
        self.assertEqual(first["sha256"], hashlib.sha256(raw).hexdigest())
        values[1]["tree"][0]["mode"] = "120000"
        with patch.object(connection, "request", side_effect=values) as api:
            self.assert_fault("github_unsupported_file", evidence.read_file, REPO, HEAD, "code.py", 0)
            self.assertEqual(api.call_count, 2)
        with patch.object(connection, "request") as api:
            self.assert_fault("invalid_github_request", evidence.read_file, REPO, HEAD, "../code.py", 0)
            for path in (".env", ".ENV", "Secrets.toml", "ID_RSA", "config/.EnV.local"):
                self.assert_fault("invalid_github_request", evidence.read_file, REPO, HEAD, path, 0)
            api.assert_not_called()

    def test_short_counted_page_and_unknown_review_evidence_fail_closed(self):
        for response in ({"total_count": 2, "check_runs": [{"id": 1}]}, {"check_runs": []}):
            with self.subTest(response=response), patch.object(connection, "request", return_value=response):
                with self.assertRaises(Fault):
                    client.list_page("endpoint", 1, 100, "check_runs")
        for malformed in ({"id": None}, {"state": "UNRECOGNIZED"}, {"user": "unavailable"}):
            original = self.reviews
            self.reviews = original + [dict(original[0], **malformed)]
            self.assert_fault("github_evidence_unavailable", evidence.merge_gate, REPO, 2, HEAD)
            self.reviews = original
        self.policy["reviewDecision"] = "UNRECOGNIZED"
        self.assert_fault("github_evidence_unavailable", evidence.merge_gate, REPO, 2, HEAD)

    def test_public_redaction_and_response_limit_do_not_claim_complete_evidence(self):
        value = operations._public({"body": "ghp_" + "x" * 32})
        self.assertTrue(value["content_redacted"])
        self.assertIn("[REDACTED]", value["body"])
        self.assert_fault("github_output_too_large", operations._public, {"body": "x" * operations.MAX_RESULT_BYTES})


if __name__ == "__main__":
    unittest.main()
