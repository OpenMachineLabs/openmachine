"""GitHub provenance, scope, candidate gates and recovery; no live credentials."""

import base64
import copy
import hashlib
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from integrations import github
from integrations.github import client, connection, evidence, operations
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault

REPO = "Owner/repo"
ROOT = f"repos/{REPO}"
HEAD, BASE, MERGE, TREE = (char * 40 for char in "abcd")
ACTOR = {"id": 1, "login": "Operator"}
STAMP = "2026-01-01T00:00:00Z"
REQUEST = connection.request


def settings(attribution=None, **scopes):
    config = scopes or {"repositories": [REPO]}
    if attribution is not None:
        config["attribution"] = attribution
    return Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}}, "github": config})


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {"id": 100, "full_name": REPO, "private": False, "default_branch": "main"}
        self.pr = {"id": 202, "number": 2, "state": "open", "draft": False, "title": "old", "body": "legacy",
                   "head": {"sha": HEAD, "ref": "topic", "label": "Owner:topic"},
                   "base": {"sha": BASE, "ref": "main", "label": "Owner:main"},
                   "user": ACTOR, "updated_at": STAMP, "changed_files": 2}
        self.issue_value = {"id": 101, "number": 1, "title": "old", "body": "legacy", "state": "open", "updated_at": STAMP}
        self.comments = []
        self.reviews = [{"id": 9, "body": "review", "state": "COMMENTED", "commit_id": HEAD,
                         "submitted_at": STAMP, "user": ACTOR, "html_url": "https://example.invalid/review/9"}]
        self.runs, self.statuses, self.threads = [], [], []
        self.policy = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                       "reviewDecision": None, "mergeQueueEntry": None}
        self.files = [{"filename": "a.py", "status": "modified", "sha": TREE, "changes": 1, "patch": "@@ -1 +1 @@\n-a\n+b"},
                      {"filename": "b.py", "status": "modified", "sha": TREE, "changes": 1}]
        self.transport = patch.object(connection, "request", side_effect=self.response).start()
        self.addCleanup(patch.stopall)

    def response(self, method, endpoint, payload=None):
        parsed = urlsplit(endpoint)
        query = parse_qs(parsed.query)
        page, limit = int(query.get("page", [1])[0]), int(query.get("per_page", [100])[0])
        def portion(items):
            return copy.deepcopy(items[(page - 1) * limit:page * limit])
        if method == "GET":
            single = {"user": ACTOR, ROOT: self.metadata, f"{ROOT}/pulls/2": self.pr,
                      f"{ROOT}/issues/1": self.issue_value, f"{ROOT}/issues/2": self.pr,
                      f"{ROOT}/issues/comments/9": {"id": 9, "body": self.issue_value["body"], "updated_at": STAMP},
                      f"{ROOT}/branches/topic": {"commit": {"sha": self.pr["head"]["sha"]}}}
            if parsed.path in single:
                return copy.deepcopy(single[parsed.path])
            lists = {f"{ROOT}/pulls/2/reviews": self.reviews, f"{ROOT}/pulls/2/files": self.files,
                     f"{ROOT}/issues/1/comments": self.comments, f"{ROOT}/issues/2/comments": self.comments,
                     f"{ROOT}/pulls/2/comments": [], f"{ROOT}/issues": [self.issue_value], f"{ROOT}/pulls": [self.pr]}
            if parsed.path in lists:
                return portion(lists[parsed.path])
            if parsed.path == f"{ROOT}/commits/{HEAD}/check-runs":
                return {"total_count": len(self.runs), "check_runs": portion(self.runs)}
            if parsed.path == f"{ROOT}/commits/{HEAD}/status":
                return {"sha": HEAD, "state": "pending", "total_count": len(self.statuses), "statuses": portion(self.statuses)}
        if method == "POST" and endpoint == "graphql":
            value = {"headRefOid": self.pr["head"]["sha"], "baseRefOid": self.pr["base"]["sha"]}
            if "reviewThreads" in payload["query"]:
                start = int(payload["variables"].get("cursor") or 0)
                size = payload["variables"]["limit"]
                more = start + size < len(self.threads)
                value["reviewThreads"] = {"nodes": copy.deepcopy(self.threads[start:start + size]),
                                          "pageInfo": {"hasNextPage": more, "endCursor": str(start + size) if more else None}}
            else:
                value.update(copy.deepcopy(self.policy))
            return {"data": {"repository": {"pullRequest": value}}}
        raise AssertionError((method, endpoint))

    def assert_fault(self, code, operation, *args, **kwargs):
        with self.assertRaises(Fault) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def task_locator(self, kind, number=None, parent=None):
        return dict(kind="github", repository=REPO, object_type=kind, number=number,
                    parent_number=parent, anchor="om-task:fixture:" + "e" * 32)

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

    def test_public_default_uses_exact_created_and_posted_footers(self):
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            github.issue(settings(), "create", REPO, "title", "body")
            self.assertEqual(mutate.call_args.args[3]["body"], "body\n\n" + operations.CREATED_FOOTER)
            github.pull_request(settings(), "create", REPO, "pr", "body", head="topic", base="main")
            self.assertEqual(mutate.call_args.args[3]["body"], "body\n\n" + operations.CREATED_FOOTER)
            github.comment(settings(), "create", REPO, "comment", number=1)
            self.assertEqual(mutate.call_args.args[3]["body"], "comment\n\n" + operations.POSTED_FOOTER)

    def test_private_default_uses_attribution_and_explicit_override_still_works(self):
        self.assertTrue(settings().github.attribution)
        self.assertFalse(settings(False).github.attribution)
        self.metadata["private"] = True
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            github.issue(settings(), "create", REPO, "title", "body")
            self.assertEqual(mutate.call_args.args[3]["body"], "body\n\n" + operations.CREATED_FOOTER)
            github.issue(settings(False), "create", REPO, "title", "body")
            self.assertEqual(mutate.call_args.args[3]["body"], "body")
        with patch.object(connection, "request") as api:
            self.assertTrue(operations._enabled(settings(), REPO))
            self.assertFalse(operations._enabled(settings(False), REPO))
            api.assert_not_called()

    def test_exact_footer_is_idempotent_and_whitespace_is_preserved(self):
        body = "body\n\n" + operations.CREATED_FOOTER
        self.assertEqual(operations._append(body, operations.CREATED_FOOTER), body)
        self.assertEqual(operations._append("body\n", operations.CREATED_FOOTER), "body\n\n" + operations.CREATED_FOOTER)
        self.assertEqual(operations._append("body\n\n", operations.CREATED_FOOTER), "body\n\n" + operations.CREATED_FOOTER)

    def test_updates_preserve_existing_footer_without_branding_legacy_content(self):
        self.issue_value["body"] = "old\n\n" + operations.CREATED_FOOTER
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            github.issue(settings(True), "update", REPO, None, "replacement", number=1)
            self.assertEqual(mutate.call_args.args[3]["body"], "replacement\n\n" + operations.CREATED_FOOTER)
            github.pull_request(settings(True), "update", REPO, None, "edited", number=2)
            self.assertEqual(mutate.call_args.args[3]["body"], "edited")
            github.comment(settings(True), "update", REPO, operations.POSTED_FOOTER, comment_id=9)
            self.assertIn(operations.CREATED_FOOTER, mutate.call_args.args[3]["body"])

    def test_review_summary_is_decorated_and_bound_to_exact_head(self):
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            github.review(settings(), "create", REPO, "review", number=2, expected_head_sha=HEAD)
        payload = mutate.call_args.args[3]
        self.assertEqual(payload["body"], "review\n\n" + operations.POSTED_FOOTER)
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

    def test_transport_diagnostics_are_bounded_and_do_not_expose_credentials(self):
        with patch.object(connection.subprocess, "run", side_effect=FileNotFoundError()):
            self.assert_fault("github_unavailable", REQUEST, "GET", "user")
        with patch.object(connection.subprocess, "run", side_effect=subprocess.TimeoutExpired(["gh"], 30)):
            self.assert_fault("github_timeout", REQUEST, "POST", "endpoint", {"body": "x"})
        for status, code in ((401, "github_authentication_required"), (403, "github_access_denied"), (409, "github_conflict")):
            def reject(argv, **kwargs):
                self.assertIn("--hostname", argv)
                self.assertNotIn("GH_DEBUG", kwargs["env"])
                kwargs["stderr"].write(f"gh: synthetic-private-detail (HTTP {status})".encode())
                return SimpleNamespace(returncode=1)
            with self.subTest(status=status), patch.object(connection.subprocess, "run", side_effect=reject):
                with self.assertRaises(Fault) as caught:
                    REQUEST("GET", "user")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("synthetic-private-detail", str(caught.exception))

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

    def test_capture_io_failure_preserves_uncertain_write_outcome(self):
        with patch.object(connection.subprocess, "run", side_effect=OSError("synthetic I/O failure")):
            self.assert_fault("github_transport_failed", REQUEST, "POST", "endpoint", {"body": "x"})
        with patch.object(connection, "request", side_effect=Fault("github_transport_failed", "I/O failure")) as api:
            result = operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback")
        self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertEqual(api.call_count, 1)

    def test_public_redaction_and_response_limit_do_not_claim_complete_evidence(self):
        value = operations._public({"body": "ghp_" + "x" * 32})
        self.assertTrue(value["content_redacted"])
        self.assertIn("[REDACTED]", value["body"])
        self.assert_fault("github_output_too_large", operations._public, {"body": "x" * operations.MAX_RESULT_BYTES})
