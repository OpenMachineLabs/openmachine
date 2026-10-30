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
    def test_review_thread_resolve_and_unresolve_verify_exact_destination(self):
        thread_id = "PRRT_fixture"
        for action, initial, expected in (("resolve", False, True), ("unresolve", True, False)):
            with self.subTest(action=action):
                self.threads = [{"id": thread_id, "isResolved": initial, "isOutdated": True}]
                mutation = "resolveReviewThread" if expected else "unresolveReviewThread"
                def api(method, endpoint, payload=None):
                    if method == "POST" and endpoint == "graphql" and payload["query"].startswith("mutation("):
                        self.assertEqual(payload["variables"], {"threadId": thread_id})
                        self.assertIn(mutation, payload["query"])
                        self.threads[0]["isResolved"] = expected
                        return {"data": {mutation: {"thread": {"id": thread_id, "isResolved": expected}}}}
                    return self.response(method, endpoint, payload)
                with patch.object(self, "transport", side_effect=api) as transport:
                    result = github.review_thread(settings(), action, REPO, 2, thread_id, HEAD, BASE, transport=self.transport)
                self.assertEqual((result["status"], result["fields_verified"]), ("verified", True))
                self.assertEqual(result["receipt"], {"number": 2, "thread_id": thread_id, "requested_isResolved": expected, "isResolved": expected})
                self.assertEqual(sum("mutation(" in call.args[2]["query"] for call in transport.call_args_list
                                     if call.args[1] == "graphql"), 1)

    def test_review_thread_preflight_rejects_wrong_target_state_and_head(self):
        self.threads = [{"id": "PRRT_fixture", "isResolved": True, "isOutdated": True}]
        cases = [("github_object_changed", "resolve", "PRRT_fixture", HEAD, BASE),
                 ("github_not_found", "resolve", "PRRT_other", HEAD, BASE),
                 ("github_candidate_changed", "resolve", "PRRT_fixture", BASE, BASE),
                 ("github_candidate_changed", "resolve", "PRRT_fixture", HEAD, HEAD),
                 ("invalid_github_request", "resolve", "invalid", HEAD, BASE)]
        for code, action, thread_id, head, base in cases:
            with self.subTest(code=code, thread_id=thread_id):
                self.assert_fault(code, github.review_thread, settings(), action, REPO, 2,
                                  thread_id, head, base, transport=self.transport)
        self.assertFalse(any(call.args[1] == "graphql" and "mutation(" in call.args[2]["query"]
                             for call in self.transport.call_args_list))

    def test_review_thread_unknown_and_unverified_results_never_replay(self):
        thread_id = "PRRT_fixture"
        self.threads = [{"id": thread_id, "isResolved": False, "isOutdated": False}]
        def lost_reply(method, endpoint, payload=None):
            if method == "POST" and endpoint == "graphql" and payload["query"].startswith("mutation("):
                self.threads[0]["isResolved"] = True
                raise OSError("Response lost after mutation")
            return self.response(method, endpoint, payload)
        with patch.object(self, "transport", side_effect=lost_reply) as transport:
            result = github.review_thread(settings(), "resolve", REPO, 2, thread_id, HEAD, transport=self.transport)
            locator = dict(self.task_locator("review_thread", 2), thread_id=thread_id, expected_head_sha=HEAD)
            observed = github.reconcile(settings(), locator, transport=self.transport)
        self.assertEqual((result["status"], observed["status"]), ("write_outcome_unknown", "write_outcome_unknown"))
        self.assertTrue(observed["receipt"]["isResolved"])
        self.assertNotIn("isResolved", result["receipt"])
        self.assertEqual(sum(call.args[1] == "graphql" and "mutation(" in call.args[2]["query"]
                             for call in transport.call_args_list), 1)

        self.threads[0]["isResolved"] = False
        def false_ack(method, endpoint, payload=None):
            if method == "POST" and endpoint == "graphql" and payload["query"].startswith("mutation("):
                return {"data": {"resolveReviewThread": {"thread": {"id": thread_id, "isResolved": True}}}}
            return self.response(method, endpoint, payload)
        with patch.object(self, "transport", side_effect=false_ack):
            unverified = github.review_thread(settings(), "resolve", REPO, 2, thread_id, HEAD, transport=self.transport)
        self.assertEqual(unverified["status"], "write_unverified")

    def test_task_publication_reconciliation_reads_original_destination_only(self):
        locator = self.task_locator("issue")
        body = "Retained fixture request.\n\n<!-- " + locator["anchor"] + " -->"
        self.issue_value.update(title="Fixture", body=body + "\n\n" + operations.CREATED_FOOTER, user=ACTOR)
        request = dict(action="create", title="Fixture", body=body, state=None)
        result = github.reconcile(settings(), locator, request, transport=self.transport)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["receipt"]["number"], 1)
        self.assertTrue(result["fields_verified"])
        self.assertTrue(all(call.args[0] == "GET" for call in self.transport.call_args_list))
        self.assertEqual(github.reconcile(settings(), locator, dict(request, title="Changed"), transport=self.transport)["status"], "observed")
        self.assertEqual(github.reconcile(settings(), dict(locator, number=1), transport=self.transport)["status"], "observed")

    def test_task_publication_search_is_bounded_and_duplicate_or_partial_coverage_is_unknown(self):
        locator = self.task_locator("issue")
        found = dict(self.issue_value, body="<!-- " + locator["anchor"] + " -->", user=ACTOR)
        with patch.object(client, "list_page", return_value=([found, dict(found, number=3)], None)):
            result = github.reconcile(settings(), locator, transport=self.transport)
            self.assertEqual(result["status"], "write_outcome_unknown")
            self.assertEqual(result["matching_destinations"], 2)
        with patch.object(client, "list_page", side_effect=[([found], 2), ([], 3), ([], 4)]) as pages:
            result = github.reconcile(settings(), locator, transport=self.transport)
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
                result = github.reconcile(settings(), locator, request, transport=self.transport)
            else:
                value = dict(self.reviews[0], body=body, issue_url=f"https://api.github.com/{ROOT}/issues/1")
                endpoint = f"{ROOT}/issues/comments/9" if kind == "comment" else f"{ROOT}/pulls/2/reviews/9"
                if kind == "review":
                    request.update(expected_head_sha=HEAD, event="COMMENT")
                def response(method, target, payload=None):
                    if method == "GET" and target == endpoint:
                        return copy.deepcopy(value)
                    return self.response(method, target, payload)
                with patch.object(self, "transport", side_effect=response) as transport:
                    result = github.reconcile(settings(), locator, request, transport=self.transport)
                    self.assertTrue(all(call.args[0] == "GET" for call in transport.call_args_list))
                    if kind == "comment":
                        value["issue_url"] = f"https://api.github.com/{ROOT}/issues/99"
                        self.assert_fault("github_wrong_object_type", github.reconcile, settings(), locator, request, transport=self.transport)
            self.assertEqual(result["status"], "verified", kind)
        unknown = github.reconcile(settings(), self.task_locator("review", 9), transport=self.transport)
        self.assertEqual(unknown["status"], "write_outcome_unknown")
        self.assertFalse(any("/None/" in call.args[1] for call in self.transport.call_args_list))
        self.assertEqual(github.reconcile(settings(), self.task_locator("pull_request", 2),
            dict(request, expected_head_sha=BASE), transport=self.transport)["status"], "observed")

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
        with patch.object(self, "transport", side_effect=response) as transport:
            result = github.reconcile(settings(), locator, request, transport=self.transport)
            self.assertEqual(result["status"], "merged_verified")
            self.assertEqual(result["receipt"], {"number": 2, "head_sha": HEAD, "merge_commit_sha": MERGE})
            self.assertEqual(github.reconcile(settings(), locator, transport=self.transport)["status"], "observed")
            comparison["merge_base_commit"]["sha"] = BASE
            self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "write_outcome_unknown")
            self.assertTrue(all(call.args[0] == "GET" for call in transport.call_args_list))
        self.assert_fault("github_candidate_changed", github.reconcile, settings(), locator, dict(request, expected_head_sha=BASE), transport=self.transport)

    def test_task_reconciliation_rejects_forged_locator_and_changed_account(self):
        for invalid in (self.task_locator("issue", True), dict(self.task_locator("issue", 1), anchor="../../unsafe")):
            self.assert_fault("invalid_github_request", github.reconcile, settings(), invalid, transport=self.transport)
        self.assertEqual(self.transport.call_count, 0)
        locator = self.task_locator("issue", 1)
        actor = dict(ACTOR, id=999)
        with patch.object(connection, "read_actor", side_effect=[ACTOR, actor]):
            self.assert_fault("github_identity_changed", github.reconcile, settings(), locator, transport=self.transport)

    def test_retained_merge_binding_verifies_head_destination_and_message(self):
        self.pr.update(merged=True, merge_commit_sha=MERGE)
        binding = {"head_sha": HEAD, "base_ref": "main",
                   "commit_title_sha256": hashlib.sha256(b"Fixture merge").hexdigest(),
                   "commit_message_sha256": hashlib.sha256(b"Fixture body").hexdigest()}
        locator = dict(self.task_locator("merge", 2), merge=binding)
        commit = {"sha": MERGE, "commit": {"message": "Fixture merge\n\nFixture body\n"}}
        comparison = {"status": "ahead", "merge_base_commit": {"sha": MERGE}}
        def response(method, target, payload=None):
            values = {f"{ROOT}/commits/{MERGE}": commit, f"{ROOT}/branches/main": {"commit": {"sha": TREE}},
                      f"{ROOT}/compare/{MERGE}...{TREE}": comparison}
            return copy.deepcopy(values[target]) if target in values else self.response(method, target, payload)
        with patch.object(self, "transport", side_effect=response) as transport:
            self.assertEqual(github.reconcile(settings(), locator, transport=transport)["status"], "merged_verified")
            for key, changed in (("merged", False), ("base", dict(self.pr["base"], ref="other"))):
                with self.subTest(key=key), patch.dict(self.pr, {key: changed}):
                    self.assertEqual(github.reconcile(settings(), locator, transport=transport)["status"], "write_outcome_unknown")
            with patch.dict(self.pr["head"], sha=BASE):
                self.assert_fault("github_candidate_changed", github.reconcile, settings(), locator, transport=transport)
            for message in ("Wrong title\n\nFixture body", "Fixture merge\n\nWrong body", None):
                with self.subTest(message=message), patch.dict(commit["commit"], message=message):
                    self.assertEqual(github.reconcile(settings(), locator, transport=transport)["status"], "write_outcome_unknown")
            comparison["merge_base_commit"]["sha"] = BASE
            self.assertEqual(github.reconcile(settings(), locator, transport=transport)["status"], "write_outcome_unknown")
            self.assertTrue(all(call.args[0] == "GET" for call in transport.call_args_list))

    def test_merge_rejects_destination_change_before_mutation(self):
        self.pr["base"]["ref"] = "other"
        with self.assertRaises(Fault) as caught:
            github.merge(settings(), REPO, 2, HEAD, BASE, expected_base_ref="main", transport=self.transport)
        self.assertEqual((caught.exception.code, caught.exception.effect), ("github_candidate_changed", "known_no_effect"))
        self.assertFalse(any(call.args[0] == "PUT" for call in self.transport.call_args_list))

    def test_rebase_rejects_custom_merge_text_without_dispatch(self):
        for text in ({"commit_title": "Custom title"}, {"commit_message": "Custom body"},
                     {"commit_title": "Custom title", "commit_message": "Custom body"}):
            with self.subTest(text=text), self.assertRaises(Fault) as caught:
                github.merge(settings(), REPO, 2, HEAD, method="rebase", **text, transport=self.transport)
            self.assertEqual((caught.exception.code, caught.exception.effect), ("invalid_github_request", "known_no_effect"))
        self.assertFalse(any(call.args[0] == "PUT" for call in self.transport.call_args_list))

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
                            operation(config, "create", REPO, *args, **kwargs, transport=self.transport)
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
                            operation(settings(enabled), "update", REPO, *args, **kwargs, transport=self.transport)
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
            github.comment(settings(True), "update", REPO, POSTED, comment_id=9, transport=self.transport)
        self.assertEqual(mutate.call_args.args[3]["body"], POSTED + "\n\n" + CREATED)

    def test_review_summary_is_bound_to_exact_head_without_inline_comments(self):
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            github.review(settings(), "create", REPO, "review", number=2, expected_head_sha=HEAD, transport=self.transport)
        payload = mutate.call_args.args[3]
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertNotIn("comments", payload)

    def test_mutation_requires_matching_destination_readback(self):
        expected = "body\n\n" + operations.CREATED_FOOTER
        with patch.object(self, "transport", side_effect=[{"number": 3}, {"number": 3, "body": expected}]):
            result = operations._mutate(REPO, "POST", f"{ROOT}/issues", {"body": expected}, lambda value: f"{ROOT}/issues/{value['number']}", transport=self.transport)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["body_verified"])
        with patch.object(self, "transport", side_effect=[{"number": 3}, {"number": 3, "body": "different"}]):
            result = operations._mutate(REPO, "POST", f"{ROOT}/issues", {"body": expected}, lambda value: f"{ROOT}/issues/{value['number']}", transport=self.transport)
        self.assertEqual(result["status"], "write_unverified")

    def test_delete_comment_requires_exact_parent_and_confirms_204_and_absence(self):
        endpoint = f"{ROOT}/issues/comments/9"
        comment = {"id": 9, "body": "Old progress", "updated_at": STAMP,
                   "issue_url": f"https://api.github.com/{ROOT}/issues/1"}
        removed = False

        def api(method, target, payload=None):
            nonlocal removed
            if method == "GET" and target == f"{ROOT}/issues/comments/8":
                return dict(comment)
            if target == endpoint:
                if method == "GET":
                    if removed:
                        raise Fault("github_not_found", "Deleted comment.")
                    return dict(comment)
                if method == "DELETE":
                    self.assertIsNone(payload)
                    removed = True
                    return {"http_status": 204}
            return self.response(method, target, payload)

        with patch.object(self, "transport", side_effect=api) as transport:
            for number, comment_id, stamp, code in (
                (2, 9, STAMP, "github_wrong_object_type"),
                (1, 8, STAMP, "github_wrong_object_type"),
                (1, 9, "stale", "github_object_changed"),
            ):
                self.assert_fault(code, github.comment, settings(), "delete", REPO,
                                  number=number, comment_id=comment_id,
                                  expected_updated_at=stamp, transport=self.transport)
            for extra in ({"body": "accidental"}, {"expected_fields": {"body": "Old progress"}}):
                self.assert_fault("invalid_github_request", github.comment, settings(), "delete",
                                  REPO, number=1, comment_id=9, transport=self.transport, **extra)
            self.assertFalse(any(call.args[0] == "DELETE" for call in transport.call_args_list))
            result = github.comment(settings(), "delete", REPO, number=1, comment_id=9,
                                    expected_updated_at=STAMP, transport=self.transport)
        self.assertEqual((result["status"], result["deleted"], result["fields_verified"]),
                         ("verified", True, True))
        self.assertEqual(result["receipt"], {"id": 9, "number": 1})
        self.assertEqual(sum(call.args[0] == "DELETE" for call in transport.call_args_list), 1)

    def test_delete_unverified_or_lost_reply_is_never_reported_success(self):
        endpoint = f"{ROOT}/issues/comments/9"
        comment = {"id": 9, "body": "Old progress", "updated_at": STAMP,
                   "issue_url": f"https://api.github.com/{ROOT}/issues/1"}
        for mode, expected in (("wrong_status", "write_unverified"),
                               ("still_visible", "write_unverified"),
                               ("lost_reply", "write_outcome_unknown"),
                               ("changed_actor", "write_unverified")):
            removed = False
            def api(method, target, payload=None):
                nonlocal removed
                if method == "GET" and target == endpoint:
                    if removed and mode != "still_visible":
                        raise Fault("github_not_found", "Deleted.")
                    return dict(comment)
                if method == "DELETE" and target == endpoint:
                    removed = True
                    if mode == "lost_reply":
                        raise OSError("Lost response after DELETE.")
                    return {"http_status": 200 if mode == "wrong_status" else 204}
                if target == "user" and removed and mode == "changed_actor":
                    return {"id": 12, "login": "Other"}
                return self.response(method, target, payload)
            with self.subTest(mode=mode), patch.object(self, "transport", side_effect=api) as transport:
                result = github.comment(settings(), "delete", REPO, number=1, comment_id=9,
                                        transport=self.transport)
                self.assertEqual(result["status"], expected)
                self.assertNotEqual(result.get("deleted"), True)
                self.assertEqual(sum(call.args[0] == "DELETE" for call in transport.call_args_list), 1)

    def test_unconfigured_repository_fails_closed(self):
        self.assert_fault("github_not_allowed", github.issue, settings(True), "create", "Other/repo", "title", "body", transport=self.transport)
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
        self.assert_fault("github_identity_changed", github.read, config, "status", transport=self.transport)
        self.assertEqual(self.transport.call_count, 1)
        self.metadata["full_name"] = "Other/transferred"
        self.assert_fault("github_repository_changed", github.read, settings(), "repository", REPO, transport=self.transport)

    def test_stale_update_and_pushed_head_reject_before_write(self):
        with patch.object(operations, "_mutate") as mutate:
            self.assert_fault("github_object_changed", github.issue, settings(), "update", REPO, "new", None,
                              number=1, expected_updated_at="old", transport=self.transport)
            self.assert_fault("github_candidate_changed", github.pull_request, settings(), "create", REPO,
                              "new", "body", head="topic", base="main", expected_head_sha=BASE, transport=self.transport)
            self.assert_fault("invalid_github_request", github.pull_request, settings(), "create", REPO,
                              "new", "body", head="topic", base="main", expected_head_sha="invalid", transport=self.transport)
            mutate.assert_not_called()
            github.issue(settings(), "update", REPO, None, None, number=1, state="closed", expected_updated_at=STAMP, transport=self.transport)
            self.assertEqual(mutate.call_args.args[3], {"state": "closed"})

    def test_each_writer_marks_preflight_failure_without_a_mutation(self):
        calls = [
            (github.issue, (settings(), "create", REPO, "Title", "Body"), {}),
            (github.pull_request, (settings(), "create", REPO, "Title", "Body"), {"head": "topic", "base": "main"}),
            (github.comment, (settings(), "create", REPO, "Body"), {"number": 1}),
            (github.review, (settings(), "create", REPO, "Body"), {"number": 2, "expected_head_sha": HEAD}),
            (github.merge, (settings(), REPO, 2, HEAD), {}),
        ]
        for writer, args, kwargs in calls:
            failure = Fault("github_invalid_response", "The preliminary read was incomplete.", effect="effect_unknown")
            with self.subTest(writer=writer.__name__), patch.object(self, "transport", side_effect=failure) as api:
                with self.assertRaises(Fault) as caught:
                    writer(*args, **kwargs, transport=self.transport)
                self.assertEqual(caught.exception.effect, "known_no_effect")
                self.assertEqual([call.args[0] for call in api.call_args_list], ["GET"])

    def test_preflight_rejections_cover_object_type_and_graphql_query(self):
        self.issue_value["pull_request"] = {"url": f"https://api.github.com/{ROOT}/pulls/1"}
        with self.assertRaises(Fault) as caught:
            github.issue(settings(), "update", REPO, "Title", None, number=1, transport=self.transport)
        self.assertEqual((caught.exception.code, caught.exception.effect), ("github_wrong_object_type", "known_no_effect"))
        self.assertTrue(all(call.args[0] == "GET" for call in self.transport.call_args_list))

        def unavailable_query(method, endpoint, payload=None):
            if endpoint == "graphql":
                self.assertTrue(payload["query"].startswith("query("))
                raise Fault("github_response_unknown", "The evidence query reply was lost.", effect="effect_unknown")
            return self.response(method, endpoint, payload)

        with patch.object(self, "transport", side_effect=unavailable_query) as api, self.assertRaises(Fault) as caught:
            github.review(settings(), "create", REPO, "Review", number=2, expected_head_sha=HEAD, transport=self.transport)
        self.assertEqual(caught.exception.effect, "known_no_effect")
        self.assertFalse(any(call.args[1].endswith("/reviews") and call.args[0] == "POST" for call in api.call_args_list))

    def test_same_failure_code_after_acknowledgment_stays_unverified(self):
        for code in ("github_network_unavailable", "github_rate_limited", "github_identity_changed",
                     "github_candidate_changed", "github_invalid_response"):
            writes = []

            def unavailable_readback(method, endpoint, payload=None):
                if method == "POST" and endpoint == f"{ROOT}/issues":
                    writes.append(copy.deepcopy(payload))
                    return {"id": 101, "number": 1, **payload}
                if writes and endpoint == f"{ROOT}/issues/1":
                    raise Fault(code, "Destination read failed.", effect="known_no_effect")
                return self.response(method, endpoint, payload)

            with self.subTest(code=code), patch.object(self, "transport", side_effect=unavailable_readback):
                result = github.issue(settings(), "create", REPO, "Title", "Body", transport=self.transport)
            self.assertEqual(result["status"], "write_unverified")
            self.assertEqual(result["error_code"], code)
            self.assertEqual(result["receipt"]["number"], 1)
            self.assertEqual(len(writes), 1)

    def test_explicit_unknown_is_not_reclassified_from_a_legacy_failure_code(self):
        for code in ("github_rate_limited", "github_network_unavailable", "resource_busy", "github_access_denied"):
            failure = Fault(code, "The enclosing request may have taken effect.", effect="effect_unknown")
            with self.subTest(code=code), patch.object(self, "transport", side_effect=failure) as api:
                result = operations._mutate(REPO, "POST", "endpoint", {"body": "Body"}, lambda _: "readback", transport=self.transport)
            self.assertEqual(result["status"], "write_outcome_unknown")
            self.assertEqual(result["error_code"], code)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(failure.effect, "effect_unknown")

    def test_oserror_after_dispatch_or_acknowledgment_preserves_uncertainty(self):
        cases = [([OSError("Lost response")], "write_outcome_unknown", ["POST"]),
                 ([{"id": 9}, OSError("Lost readback")], "write_unverified", ["POST", "GET"])]
        for responses, status, methods in cases:
            with self.subTest(status=status), patch.object(self, "transport", side_effect=responses) as api:
                result = operations._mutate(REPO, "POST", "endpoint", {"body": "Body"}, lambda _: "readback", transport=self.transport)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["error_code"], "github_response_unknown")
            self.assertEqual([call.args[0] for call in api.call_args_list], methods)

    def test_unverified_receipts_keep_failure_codes_for_incomplete_readback(self):
        cases = (
            ([{"body": "Body"}], "github_response_unknown"),
            ([{"id": 9}, {}], "github_response_unknown"),
            ([{"id": 9}, {"id": 10, "body": "Body"}], "github_response_unknown"),
            ([{"id": 9}, {"id": 9, "body": "Changed"}], "github_response_unknown"),
            ([{"id": 9}, KeyError("Missing readback identity")], "github_response_unknown"),
            ([{"id": 9}, None], "github_invalid_response"),
        )
        for responses, code in cases:
            with self.subTest(responses=responses), patch.object(self, "transport", side_effect=responses) as api:
                result = operations._mutate(REPO, "POST", "endpoint", {"body": "Body"}, lambda _: "readback", transport=self.transport)
            self.assertEqual((result["status"], result["error_code"]), ("write_unverified", code))
            self.assertEqual(sum(call.args[0] == "POST" for call in api.call_args_list), 1)

    def test_head_pinned_pr_creation_keeps_final_observation_failure_code(self):
        for error in (Fault("github_candidate_changed", "Candidate changed after publication.", effect="known_no_effect"),
                      OSError("Final candidate read failed")):
            writes = []

            def created(method, endpoint, payload=None):
                if method == "POST" and endpoint == f"{ROOT}/pulls":
                    writes.append(copy.deepcopy(payload))
                    self.pr.update(title=payload["title"], body=payload["body"], draft=payload["draft"])
                    return copy.deepcopy(self.pr)
                return self.response(method, endpoint, payload)

            with self.subTest(error=type(error).__name__), patch.object(self, "transport", side_effect=created), \
                    patch.object(evidence, "pull_request", side_effect=error):
                result = github.pull_request(settings(), "create", REPO, "Title", "Body", head="topic", base="main",
                                             expected_head_sha=HEAD, transport=self.transport)
            self.assertEqual(result["status"], "write_unverified")
            self.assertEqual(result["error_code"], error.code if isinstance(error, Fault) else "github_response_unknown")
            self.assertEqual(result["receipt"]["number"], 2)
            self.assertEqual(len(writes), 1)

    def test_preflight_oserror_proves_no_business_mutation(self):
        with patch.object(self, "transport", side_effect=OSError("Unavailable preliminary read")) as api:
            with self.assertRaises(Fault) as caught:
                github.issue(settings(), "create", REPO, "Title", "Body", transport=self.transport)
        self.assertEqual(caught.exception.effect, "known_no_effect")
        self.assertEqual([call.args[0] for call in api.call_args_list], ["GET"])

    def test_merge_and_ready_acknowledgments_keep_readback_uncertainty(self):
        for kind in ("merge", "ready"):
            for error in (Fault("github_candidate_changed", "Readback changed.", effect="known_no_effect"),
                          OSError("Readback unavailable")):
                self.pr.update(draft=kind == "ready", merged=False, state="open")
                writes = []

                def unavailable_readback(method, endpoint, payload=None):
                    if kind == "merge" and method == "PUT":
                        writes.append(copy.deepcopy(payload))
                        self.pr.update(merged=True, merge_commit_sha=MERGE, state="closed")
                        return {"merged": True, "sha": MERGE}
                    if kind == "ready" and method == "POST" and endpoint == "graphql":
                        self.assertIn("markPullRequestReadyForReview", payload["query"])
                        writes.append(copy.deepcopy(payload))
                        return self.response(method, endpoint, payload)
                    if writes and endpoint == f"{ROOT}/pulls/2":
                        raise error
                    return self.response(method, endpoint, payload)

                with self.subTest(kind=kind, error=type(error).__name__), patch.object(self, "transport", side_effect=unavailable_readback):
                    result = (github.merge(settings(), REPO, 2, HEAD, BASE, transport=self.transport) if kind == "merge"
                              else github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                                                       expected_head_sha=HEAD, transport=self.transport))
                self.assertEqual(result["status"], "merge_unverified" if kind == "merge" else "write_unverified")
                self.assertEqual(result["error_code"], error.code if isinstance(error, Fault) else "github_response_unknown")
                self.assertEqual(len(writes), 1)

    def test_merge_and_ready_keep_uncertain_dispatch_failure_codes(self):
        for kind in ("merge", "ready"):
            for error in (Fault("github_rate_limited", "Uncertain request.", effect="effect_unknown"),
                          Fault("github_response_unknown", "Reply was lost.", effect="effect_unknown"),
                          OSError("Reply unavailable")):
                self.pr.update(draft=kind == "ready", merged=False, state="open")
                writes = []

                def lost(method, endpoint, payload=None):
                    if kind == "merge" and method == "PUT" or kind == "ready" and method == "POST" and endpoint == "graphql":
                        writes.append(copy.deepcopy(payload))
                        raise error
                    if writes and endpoint == f"{ROOT}/pulls/2":
                        raise Fault("github_candidate_changed", "Readback also failed.", effect="known_no_effect")
                    return self.response(method, endpoint, payload)

                with self.subTest(kind=kind, error=type(error).__name__), patch.object(self, "transport", side_effect=lost):
                    result = (github.merge(settings(), REPO, 2, HEAD, BASE, transport=self.transport) if kind == "merge"
                              else github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                                                       expected_head_sha=HEAD, transport=self.transport))
                self.assertEqual(result["status"], "merge_outcome_unknown" if kind == "merge" else "write_outcome_unknown")
                self.assertEqual(result["error_code"], error.code if isinstance(error, Fault) else "github_response_unknown")
                self.assertEqual(len(writes), 1)

    def test_field_guards_allow_unrelated_timestamp_activity_and_compare_sets(self):
        self.pr.update(updated_at="later", labels=[{"name": "Maintenance"}], assignees=[ACTOR])
        writes = []

        def update_metadata(method, endpoint, payload=None):
            if method == "PATCH":
                writes.append((endpoint, copy.deepcopy(payload)))
                self.pr.update(labels=[{"name": value} for value in payload["labels"]],
                               assignees=[{"login": value} for value in payload["assignees"]])
                return copy.deepcopy(self.pr)
            return self.response(method, endpoint, payload)

        with patch.object(self, "transport", side_effect=update_metadata):
            result = github.pull_request(settings(), "update", REPO, None, None, number=2,
                expected_head_sha=HEAD, labels=["bug"], assignees=[],
                expected_fields={"labels": ["maintenance"], "assignees": ["operator"]}, transport=self.transport)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(writes, [(f"{ROOT}/issues/2", {"labels": ["bug"], "assignees": []})])

    def test_field_guards_reject_only_changed_replaced_fields_before_mutation(self):
        self.pr.update(body="Changed by another editor", labels=[{"name": "bug"}])
        cases = [(dict(body="Replacement", expected_fields={"body": "legacy"}), "github_object_changed"),
                 (dict(body=None, labels=[], expected_fields={"labels": []}), "github_object_changed")]
        with patch.object(operations, "_mutate") as mutate:
            for kwargs, code in cases:
                with self.subTest(kwargs=kwargs), self.assertRaises(Fault) as caught:
                    github.pull_request(settings(), "update", REPO, None, number=2,
                        expected_head_sha=HEAD, **kwargs, transport=self.transport)
                self.assertEqual((caught.exception.code, caught.exception.effect), (code, "known_no_effect"))
            mutate.assert_not_called()

    def test_field_guards_require_exact_complete_bounded_values(self):
        invalid = [{}, {"labels": [], "body": "legacy"}, {"labels": None}, {"labels": "bug"},
                   {"labels": ["bug", "BUG"]}, {"labels": [None]}, {"labels": ["x"] * 101}, []]
        with patch.object(operations, "_mutate") as mutate:
            for expected in invalid:
                with self.subTest(expected=expected), self.assertRaises(Fault) as caught:
                    github.issue(settings(), "update", REPO, None, None, number=1,
                        labels=["bug"], expected_fields=expected, transport=self.transport)
                self.assertEqual((caught.exception.code, caught.exception.effect), ("invalid_github_request", "known_no_effect"))
            for expected in ({"state": []}, {"body": "x" * 65537}, {"title": 3}):
                kwargs = {next(iter(expected)): "closed" if "state" in expected else "new"}
                with self.subTest(field=next(iter(expected))), self.assertRaises(Fault) as caught:
                    github.issue(settings(), "update", REPO, kwargs.get("title"), kwargs.get("body"), number=1,
                        state=kwargs.get("state"), expected_fields=expected, transport=self.transport)
                self.assertEqual((caught.exception.code, caught.exception.effect), ("invalid_github_request", "known_no_effect"))
            mutate.assert_not_called()

    def test_field_guards_keep_timestamp_and_head_preconditions(self):
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            for head, stamp, code in ((HEAD, "stale", "github_object_changed"), (BASE, STAMP, "github_candidate_changed")):
                with self.subTest(head=head, stamp=stamp), self.assertRaises(Fault) as caught:
                    github.pull_request(settings(), "update", REPO, None, None, number=2, labels=["bug"],
                        expected_head_sha=head, expected_updated_at=stamp, expected_fields={"labels": []}, transport=self.transport)
                self.assertEqual((caught.exception.code, caught.exception.effect), (code, "known_no_effect"))
            mutate.assert_not_called()
            github.pull_request(settings(), "update", REPO, None, None, number=2, labels=["bug"],
                expected_head_sha=HEAD, expected_updated_at=STAMP, expected_fields={"labels": []}, transport=self.transport)
            self.assertEqual(mutate.call_count, 1)

    def test_comment_and_review_body_guards_preserve_original_provenance(self):
        original = "Original\n\n" + POSTED
        self.issue_value["body"] = original
        self.reviews[0]["body"] = original
        calls = [(github.comment, {"comment_id": 9}), (github.review, {"number": 2, "review_id": 9})]
        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            for writer, kwargs in calls:
                with self.subTest(writer=writer.__name__):
                    writer(settings(), "update", REPO, "Replacement", expected_fields={"body": original},
                           **kwargs, transport=self.transport)
                    self.assertEqual(mutate.call_args.args[3], {"body": "Replacement\n\n" + POSTED})
                    with self.assertRaises(Fault) as caught:
                        writer(settings(), "update", REPO, "Replacement", expected_fields={"body": "Original"},
                               **kwargs, transport=self.transport)
                    self.assertEqual(caught.exception.effect, "known_no_effect")
            self.assertEqual(mutate.call_count, 2)

    def test_field_guards_distinguish_missing_body_from_observed_null(self):
        def missing_body(method, endpoint, payload=None):
            response = self.response(method, endpoint, payload)
            if method == "GET" and endpoint == f"{ROOT}/issues/1":
                response.pop("body")
            return response

        with patch.object(operations, "_mutate", return_value={"status": "verified"}) as mutate:
            with patch.object(self, "transport", side_effect=missing_body), self.assertRaises(Fault) as caught:
                github.issue(settings(), "update", REPO, None, "New", number=1,
                    expected_fields={"body": None}, transport=self.transport)
            self.assertEqual((caught.exception.code, caught.exception.effect), ("github_object_changed", "known_no_effect"))
            mutate.assert_not_called()
            self.issue_value["body"] = None
            github.issue(settings(), "update", REPO, None, "New", number=1,
                expected_fields={"body": None}, transport=self.transport)
            self.assertEqual(mutate.call_count, 1)

    def test_creations_reject_field_guards_without_dispatch(self):
        calls = [
            (github.issue, (settings(), "create", REPO, "Title", "Body"), {}),
            (github.pull_request, (settings(), "create", REPO, "Title", "Body"), {"head": "topic", "base": "main"}),
            (github.comment, (settings(), "create", REPO, "Body"), {"number": 1}),
            (github.review, (settings(), "create", REPO, "Body"), {"number": 2, "expected_head_sha": HEAD}),
        ]
        with patch.object(operations, "_mutate") as mutate:
            for writer, args, kwargs in calls:
                with self.subTest(writer=writer.__name__), self.assertRaises(Fault) as caught:
                    writer(*args, **kwargs, expected_fields={}, transport=self.transport)
                self.assertEqual((caught.exception.code, caught.exception.effect), ("invalid_github_request", "known_no_effect"))
            mutate.assert_not_called()

    def test_ready_for_review_accepts_only_the_current_draft_field(self):
        self.pr.update(draft=True, updated_at="later")
        result = github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
            expected_head_sha=HEAD, expected_fields={"draft": True}, transport=self.transport)
        self.assertEqual(result["status"], "verified")
        self.pr["draft"] = True
        for expected, code in (({"draft": False}, "github_object_changed"), ({"body": "legacy"}, "invalid_github_request"),
                               ({"draft": 1}, "invalid_github_request")):
            with self.subTest(expected=expected), self.assertRaises(Fault) as caught:
                github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                    expected_head_sha=HEAD, expected_fields=expected, transport=self.transport)
            self.assertEqual((caught.exception.code, caught.exception.effect), (code, "known_no_effect"))
        mutations = [call for call in self.transport.call_args_list
                     if call.args[0] == "POST" and call.args[1] == "graphql"]
        self.assertEqual(len(mutations), 1)

    def test_ready_for_review_uses_fixed_mutation_and_verified_readback(self):
        self.pr["draft"] = True
        with patch.object(self, "transport", side_effect=self.response) as api:
            result = github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                                         expected_updated_at=STAMP, expected_head_sha=HEAD, transport=self.transport)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["verified_fields"], ["draft"])
        self.assertTrue(result["provider_response_verified"])
        self.assertFalse(self.pr["draft"])
        mutations = [call for call in api.call_args_list
                     if call.args[0] == "POST" and call.args[1] == "graphql"]
        self.assertEqual(len(mutations), 1)
        self.assertIn("markPullRequestReadyForReview", mutations[0].args[2]["query"])
        reconciled = github.reconcile(settings(), self.task_locator("pull_request", 2),
                                      {"action": "ready_for_review", "expected_head_sha": HEAD}, transport=self.transport)
        self.assertEqual(reconciled["status"], "verified")
        self.assertTrue(reconciled["fields_verified"])

    def test_ready_for_review_rejects_stale_wrong_or_non_draft_candidates_before_mutation(self):
        with patch.object(self, "transport", side_effect=self.response) as api:
            self.assert_fault("github_conflict", github.pull_request, settings(), "ready_for_review",
                              REPO, None, None, number=2, expected_updated_at=STAMP,
                              expected_head_sha=HEAD, transport=self.transport)
            self.pr["draft"] = True
            self.assert_fault("github_object_changed", github.pull_request, settings(), "ready_for_review",
                              REPO, None, None, number=2, expected_updated_at="stale",
                              expected_head_sha=HEAD, transport=self.transport)
            self.assert_fault("github_candidate_changed", github.pull_request, settings(), "ready_for_review",
                              REPO, None, None, number=2, expected_head_sha=BASE, transport=self.transport)
            self.assert_fault("invalid_github_request", github.pull_request, settings(), "ready_for_review",
                              REPO, None, None, number=2, expected_head_sha="", transport=self.transport)
            self.assert_fault("invalid_github_request", github.pull_request, settings(), "ready_for_review",
                              REPO, "changed", None, number=2, expected_head_sha=HEAD, transport=self.transport)
        self.assertFalse(any(call.args[0] == "POST" and call.args[1] == "graphql"
                             for call in api.call_args_list))

    def test_ready_for_review_unknown_and_unverified_results_are_never_replayed(self):
        self.pr["draft"] = True
        mutations = []
        def timeout(method, endpoint, payload=None):
            if method == "POST" and endpoint == "graphql":
                mutations.append(payload)
                raise Fault("github_response_unknown", "fixture lost acknowledgement")
            return self.response(method, endpoint, payload)
        with patch.object(self, "transport", side_effect=timeout):
            result = github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                                         expected_head_sha=HEAD, transport=self.transport)
            self.assertEqual(result["status"], "write_outcome_unknown")
            self.pr["draft"] = False
            reconciled = github.reconcile(settings(), self.task_locator("pull_request", 2),
                                          {"action": "ready_for_review", "expected_head_sha": HEAD}, transport=self.transport)
            self.assertEqual(reconciled["status"], "verified")
        self.assertEqual(len(mutations), 1)

        self.pr["draft"] = True
        def unverified(method, endpoint, payload=None):
            if method == "POST" and endpoint == "graphql":
                return {"data": {"markPullRequestReadyForReview": {"pullRequest": {
                    "id": self.pr["node_id"], "number": self.pr["number"], "isDraft": False}}}}
            return self.response(method, endpoint, payload)
        with patch.object(self, "transport", side_effect=unverified):
            result = github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                                         expected_head_sha=HEAD, transport=self.transport)
        self.assertEqual(result["status"], "write_unverified")
        self.assertTrue(self.pr["draft"])

    def test_ready_for_review_applied_mutation_lost_reply_stays_unknown(self):
        for error in (Fault("github_response_unknown", "The applied mutation reply was lost."),
                      Fault("github_rate_limited", "The dispatched mutation has no conclusive reply.", effect="effect_unknown"),
                      OSError("The applied mutation reply was unavailable.")):
            self.pr["draft"] = True
            mutations = []

            def lost(method, endpoint, payload=None):
                if method == "POST" and endpoint == "graphql":
                    mutations.append(copy.deepcopy(payload))
                    self.response(method, endpoint, payload)
                    raise error
                return self.response(method, endpoint, payload)

            with self.subTest(error=type(error).__name__, code=getattr(error, "code", None)), \
                    patch.object(self, "transport", side_effect=lost):
                result = github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                                             expected_head_sha=HEAD, transport=self.transport)
            self.assertFalse(self.pr["draft"], "The fixture mutation applied before its reply was lost.")
            self.assertEqual(result["status"], "write_outcome_unknown")
            self.assertEqual(result["error_code"], error.code if isinstance(error, Fault) else "github_response_unknown")
            self.assertFalse(result.get("provider_response_verified", False))
            self.assertFalse(result.get("fields_verified", False))
            self.assertEqual(len(mutations), 1)

    def test_ready_for_review_invalid_acknowledgment_cannot_use_ready_readback_as_proof(self):
        def acknowledgement(**fields):
            return {"data": {"markPullRequestReadyForReview": {"pullRequest": {
                "id": self.pr["node_id"], "number": 2, "isDraft": False, **fields}}}}

        cases = (
            (None, "write_outcome_unknown", "github_invalid_response"),
            ({}, "write_unverified", "github_response_unknown"),
            (acknowledgement(id="another_pr"), "write_unverified", "github_response_unknown"),
            (acknowledgement(number=3), "write_unverified", "github_response_unknown"),
            (acknowledgement(number=2.0), "write_unverified", "github_response_unknown"),
            (acknowledgement(isDraft=True), "write_unverified", "github_response_unknown"),
            (dict(acknowledgement(), errors=[{"message": "Fixture GraphQL failure."}]),
             "write_outcome_unknown", "github_graphql_error"),
        )
        for response, status, code in cases:
            self.pr["draft"] = True
            mutations = []

            def invalid(method, endpoint, payload=None):
                if method == "POST" and endpoint == "graphql":
                    mutations.append(copy.deepcopy(payload))
                    self.response(method, endpoint, payload)
                    return copy.deepcopy(response)
                return self.response(method, endpoint, payload)

            with self.subTest(response=response), patch.object(self, "transport", side_effect=invalid):
                result = github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                                             expected_head_sha=HEAD, transport=self.transport)
            self.assertFalse(self.pr["draft"], "Current ready state alone cannot prove the acknowledged operation.")
            self.assertEqual((result["status"], result["error_code"]), (status, code))
            self.assertFalse(result.get("provider_response_verified", False))
            self.assertFalse(result.get("fields_verified", False))
            self.assertEqual(len(mutations), 1)

    def test_ready_for_review_acknowledged_mutation_with_malformed_readback_stays_unverified(self):
        self.pr["draft"] = True
        mutations = []

        def malformed_readback(method, endpoint, payload=None):
            if method == "POST" and endpoint == "graphql":
                mutations.append(copy.deepcopy(payload))
            response = self.response(method, endpoint, payload)
            if mutations and method == "GET" and endpoint == f"{ROOT}/pulls/2":
                response["head"]["sha"] = None
            return response

        with patch.object(self, "transport", side_effect=malformed_readback):
            result = github.pull_request(settings(), "ready_for_review", REPO, None, None, number=2,
                                         expected_head_sha=HEAD, transport=self.transport)
        self.assertFalse(self.pr["draft"], "The provider acknowledged the applied transition.")
        self.assertEqual((result["status"], result["error_code"]),
                         ("write_unverified", "github_response_unknown"))
        self.assertFalse(result.get("fields_verified", False))
        self.assertEqual(len(mutations), 1)

    def test_ready_for_review_concurrent_push_keeps_completed_transition_unverified(self):
        self.pr["draft"] = True
        mutations = []

        def concurrent_push(method, endpoint, payload=None):
            if method == "POST" and endpoint == "graphql":
                mutations.append(payload)
                self.pr["head"]["sha"] = BASE
            return self.response(method, endpoint, payload)

        with patch.object(self, "transport", side_effect=concurrent_push):
            result = github.pull_request(settings(), "ready_for_review", REPO, None, None,
                                         number=2, expected_head_sha=HEAD, transport=self.transport)
            self.assertEqual(result["status"], "write_unverified")
            self.assertFalse(self.pr["draft"])
            reconciled = github.reconcile(settings(), self.task_locator("pull_request", 2),
                                          {"action": "ready_for_review", "expected_head_sha": HEAD}, transport=self.transport)
            self.assertEqual(reconciled["status"], "observed")
            self.assertFalse(reconciled["fields_verified"])
        self.assertEqual(len(mutations), 1)

    def test_issue_update_rejects_pull_request_before_mutation(self):
        self.issue_value["pull_request"] = {"url": "https://api.github.com/repos/Owner/repo/pulls/1"}
        with patch.object(operations, "_mutate") as mutate:
            self.assert_fault("github_wrong_object_type", github.issue, settings(), "update", REPO,
                              None, None, number=1, state="closed", expected_updated_at=STAMP, transport=self.transport)
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
        with patch.object(self, "transport", side_effect=mutating) as api:
            issue = github.issue(settings(), "update", REPO, None, None, number=1,
                                 expected_updated_at=STAMP, labels=[], assignees=["Operator"], transport=self.transport)
            pr = github.pull_request(settings(), "update", REPO, None, None, number=2,
                                     expected_updated_at=STAMP, expected_head_sha=HEAD,
                                     labels=["maintenance"], assignees=["Operator"], transport=self.transport)
            observed = github.read(settings(), "pull_request", REPO, number=2, transport=self.transport)
        self.assertEqual((issue["status"], pr["status"]), ("verified", "verified"))
        writes = [call for call in api.call_args_list if call.args[0] == "PATCH"]
        self.assertEqual([call.args[1] for call in writes], [f"{ROOT}/issues/1", f"{ROOT}/issues/2"])
        self.assertEqual(observed["data"]["labels"], ["maintenance"])
        self.assertEqual(observed["data"]["assignees"], [ACTOR])

    def test_pr_metadata_stays_one_effect_and_validates_names(self):
        with patch.object(operations, "_mutate") as mutate:
            self.assert_fault("invalid_github_request", github.pull_request, settings(), "create", REPO,
                              "new", "body", head="topic", base="main", labels=["maintenance"], transport=self.transport)
            self.assert_fault("invalid_github_request", github.pull_request, settings(), "update", REPO,
                              "new", None, number=2, expected_head_sha=HEAD, labels=["maintenance"], transport=self.transport)
            self.assert_fault("invalid_github_request", github.issue, settings(), "create", REPO,
                              "new", "body", labels=["duplicate", "DUPLICATE"], transport=self.transport)
            mutate.assert_not_called()

    def test_metadata_readback_and_reconciliation_compare_named_sets(self):
        observed = {"id": 9, "number": 2, "labels": [{"name": "Maintenance"}],
                    "assignees": [{"login": "operator"}]}
        with patch.object(self, "transport", side_effect=[observed, observed]):
            result = operations._mutate(REPO, "PATCH", "endpoint",
                {"labels": ["maintenance"], "assignees": ["Operator"]}, lambda _: "readback", transport=self.transport)
        self.assertEqual(result["status"], "verified")
        observed["labels"] = [{"name": "bug"}]
        with patch.object(self, "transport", side_effect=[observed, observed]):
            result = operations._mutate(REPO, "PATCH", "endpoint",
                {"labels": ["maintenance"]}, lambda _: "readback", transport=self.transport)
        self.assertEqual(result["status"], "write_unverified")

        locator = self.task_locator("pull_request", 2)
        self.pr.update(labels=[{"name": "maintenance"}], assignees=[ACTOR])
        request = dict(action="update", labels=["maintenance"], assignees=["operator"],
                       expected_head_sha=HEAD)
        reconciled = github.reconcile(settings(), locator, request, transport=self.transport)
        self.assertEqual(reconciled["status"], "verified")
        self.assertTrue(reconciled["fields_verified"])

    def test_readback_verifies_all_fields_review_commit_and_actor(self):
        cases = [({"title": "new", "body": "same"}, {"title": "old", "body": "same"}),
                 ({"state": "closed"}, {"state": "open"}),
                 ({"commit_id": HEAD, "event": "COMMENT"}, {"commit_id": BASE, "state": "COMMENTED"})]
        for payload, observed in cases:
            with self.subTest(payload=payload), patch.object(self, "transport", side_effect=[{"id": 9}, {"id": 9, **observed}]):
                result = operations._mutate(REPO, "POST", "endpoint", payload, lambda _: "readback", transport=self.transport)
                self.assertEqual(result["status"], "write_unverified")
        with patch.object(self, "transport", side_effect=[{"id": 9}, {"id": 9, "body": "same"}, {"id": 2, "login": "Other"}]):
            result = operations._mutate(REPO, "POST", "endpoint", {"body": "same"}, lambda _: "readback", settings=settings(), actor=ACTOR, transport=self.transport)
        self.assertEqual(result["status"], "write_unverified")

    def test_lost_comment_acknowledgement_is_read_not_replayed(self):
        writes = []
        def lossy(method, endpoint, payload=None):
            if method == "POST" and endpoint == f"{ROOT}/issues/1/comments":
                writes.append(endpoint)
                self.comments.append({"id": 9, "body": payload["body"], "user": ACTOR})
                raise Fault("github_response_unknown", "synthetic lost acknowledgement")
            return self.response(method, endpoint, payload)
        with patch.object(self, "transport", side_effect=lossy):
            result = github.comment(settings(False), "create", REPO, "unique-test-comment", number=1, transport=self.transport)
            self.assertEqual(result["status"], "write_outcome_unknown")
            observed = github.read(settings(), "comments", REPO, number=1, transport=self.transport)
        self.assertEqual(len(writes), 1)
        self.assertEqual([item["body"] for item in observed["data"]], ["unique-test-comment"])

    def test_known_rejection_is_not_an_unknown_write_or_retry(self):
        for code in ("github_access_denied", "github_network_unavailable", "github_rate_limited", "resource_busy"):
            fault = Fault(code, "Known rejection.", {"retry_after_seconds": 60})
            with self.subTest(code=code), patch.object(self, "transport", side_effect=fault) as api:
                with self.assertRaises(Fault) as caught:
                    operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback", transport=self.transport)
                self.assertIs(caught.exception, fault)
                self.assertEqual(api.call_count, 1)

    def test_readback_failure_preserves_acknowledged_write_without_replay(self):
        for code in ("github_network_unavailable", "github_rate_limited", "resource_busy"):
            with self.subTest(code=code), patch.object(self, "transport",
                    side_effect=[{"id": 9}, Fault(code, "Read unavailable.")]) as api:
                result = operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback", transport=self.transport)
                self.assertEqual(result["status"], "write_unverified")
                self.assertEqual(result["receipt"]["id"], 9)
                self.assertEqual([call.args[0] for call in api.call_args_list], ["POST", "GET"])

    def test_candidate_pages_require_both_revisions_and_report_missing_patch(self):
        first = github.read(settings(), "files", REPO, number=2, expected_head_sha=HEAD, limit=1, transport=self.transport)
        self.assertEqual(first["next_page"], 2)
        self.assertFalse(first["data"][0]["patch_is_complete_evidence"])
        self.assert_fault("invalid_github_request", github.read, settings(), "files", REPO, number=2,
                          expected_head_sha=HEAD, page=2, limit=1, transport=self.transport)
        second = github.read(settings(), "files", REPO, number=2, expected_head_sha=HEAD, expected_base_sha=BASE, page=2, limit=1, transport=self.transport)
        self.assertFalse(second["has_more"])
        self.assertFalse(second["data"][0]["patch_available"])
        self.pr["head"]["sha"] = MERGE
        self.assert_fault("github_candidate_changed", github.read, settings(), "files", REPO, number=2,
                          expected_head_sha=HEAD, expected_base_sha=BASE, page=2, limit=1, transport=self.transport)

    def test_missing_file_page_and_gate_capacity_are_explicit(self):
        self.files = self.files[:1]
        self.assert_fault("github_evidence_unavailable", github.read, settings(), "files", REPO, number=2, expected_head_sha=HEAD, transport=self.transport)
        self.reviews = [dict(self.reviews[0], id=i + 1) for i in range(1001)]
        self.assert_fault("github_evidence_unavailable", evidence.merge_gate, REPO, 2, HEAD, transport=self.transport)

    def test_zero_statuses_are_no_checks_not_pending(self):
        observed = evidence.merge_gate(REPO, 2, HEAD, transport=self.transport)
        self.assertEqual(observed["checks"]["state"], "no_checks")
        self.assertEqual(observed["blockers"], [])
        self.assertFalse(observed["self_review_is_independent_approval"])

    def test_superseded_cancelled_check_does_not_block_current_success(self):
        run = {"id": 1, "name": "test", "app": {"id": 42}, "head_sha": HEAD, "status": "completed", "conclusion": "cancelled"}
        self.runs = [run, dict(run, id=2, conclusion="success")]
        observed = evidence.merge_gate(REPO, 2, HEAD, transport=self.transport)
        self.assertEqual(observed["checks"]["state"], "passed")
        self.assertEqual([item["id"] for item in observed["checks"]["check_runs"]], [2])
        self.runs[1]["conclusion"] = "cancelled"
        self.assertIn("current_checks_not_passed", evidence.merge_gate(REPO, 2, HEAD, transport=self.transport)["blockers"])

    def test_review_pagination_and_unresolved_outdated_thread_block_merge(self):
        self.reviews = [dict(self.reviews[0], id=i + 1) for i in range(101)]
        self.threads = [{"id": "thread", "isResolved": False, "isOutdated": True, "path": "a.py", "line": 1}]
        observed = evidence.merge_gate(REPO, 2, HEAD, transport=self.transport)
        self.assertEqual(observed["review_count"], 101)
        self.assertIn("unresolved_review_threads", observed["blockers"])
        result = github.merge(settings(), REPO, 2, HEAD, BASE, transport=self.transport)
        self.assertEqual(result["status"], "merge_blocked")
        self.assertFalse(any(call.args[0] == "PUT" for call in self.transport.call_args_list))

    def test_comment_does_not_clear_prior_change_request(self):
        self.reviews = [dict(self.reviews[0], id=8, state="CHANGES_REQUESTED"), self.reviews[0]]
        self.assertIn("review_requirements_not_met", evidence.merge_gate(REPO, 2, HEAD, transport=self.transport)["blockers"])
        self.reviews.append(dict(self.reviews[0], id=10, state="DISMISSED"))
        self.assertNotIn("review_requirements_not_met", evidence.merge_gate(REPO, 2, HEAD, transport=self.transport)["blockers"])
        self.policy["reviewDecision"] = "REVIEW_REQUIRED"
        self.assertIn("review_requirements_not_met", evidence.merge_gate(REPO, 2, HEAD, transport=self.transport)["blockers"])

    def test_missing_graphql_evidence_rejects_review_without_submission(self):
        def partial(method, endpoint, payload=None):
            if endpoint == "graphql":
                return {"data": {}, "errors": [{"message": "not available"}]}
            return self.response(method, endpoint, payload)
        with patch.object(self, "transport", side_effect=partial), patch.object(operations, "_mutate") as mutate:
            self.assert_fault("github_evidence_unavailable", github.review, settings(), "create", REPO, "review",
                              number=2, expected_head_sha=HEAD, transport=self.transport)
            mutate.assert_not_called()

    def test_changed_candidate_self_approval_and_optional_review(self):
        with patch.object(operations, "_mutate") as mutate:
            self.assert_fault("github_candidate_changed", github.review, settings(), "create", REPO, "review",
                              number=2, expected_head_sha=BASE, transport=self.transport)
            self.assert_fault("github_self_review", github.review, settings(), "create", REPO, "review",
                              number=2, expected_head_sha=HEAD, event="APPROVE", transport=self.transport)
            mutate.assert_not_called()
        self.reviews = []
        gate = evidence.merge_gate(REPO, 2, HEAD, transport=self.transport)
        self.assertEqual(gate["review_count"], 0)
        self.assertEqual(gate["current_head_reviews"], [])
        self.assertEqual(gate["blockers"], [])
        self.reviews = [{"id": 9, "user": ACTOR, "state": "COMMENTED", "commit_id": HEAD, "submitted_at": STAMP}]
        self.policy["mergeQueueEntry"] = {"id": "queue"}
        self.assertIn("native_policy_not_clean_or_queue_unsupported", github.merge(settings(), REPO, 2, HEAD, transport=self.transport)["gate"]["blockers"])

    def test_merge_verifies_actual_destination_and_never_bypasses(self):
        writes = []
        def merging(method, endpoint, payload=None):
            if method == "PUT":
                self.assertEqual(endpoint, f"{ROOT}/pulls/2/merge")
                self.assertEqual(payload, {"sha": HEAD, "merge_method": merge_method})
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
        for merge_method in ("squash", "merge", "rebase"):
            self.pr.update(state="open", merged=False)
            writes.clear()
            with self.subTest(method=merge_method), patch.object(self, "transport", side_effect=merging):
                result = github.merge(settings(), REPO, 2, HEAD, BASE, method=merge_method, transport=self.transport)
            self.assertEqual(result["status"], "merged_verified")
            self.assertEqual(result["merge_commit_sha"], MERGE)
            self.assertEqual(len(writes), 1)

        # A real merge can proceed with passing CI and zero submitted reviews.
        self.reviews = []
        self.runs = [{"id": 1, "name": "ci", "app": {"id": 42}, "head_sha": HEAD,
                      "status": "completed", "conclusion": "success"}]
        self.pr.update(state="open", merged=False)
        writes.clear()
        merge_method = "squash"
        with patch.object(self, "transport", side_effect=merging):
            result = github.merge(settings(), REPO, 2, HEAD, BASE, method=merge_method, transport=self.transport)
        self.assertEqual((result["status"], result["checks_state"]), ("merged_verified", "passed"))
        self.assertEqual(len(writes), 1)

    def test_merge_unknown_and_unverified_are_not_success(self):
        for behavior, expected in (("timeout", "merge_outcome_unknown"), ("wrong_destination", "merge_unverified")):
            self.pr.update(state="open", merged=False)
            writes = []
            def uncertain(method, endpoint, payload=None):
                if method == "PUT":
                    writes.append(payload)
                    if behavior == "timeout":
                        raise Fault("github_response_unknown", "lost acknowledgement")
                    self.pr.update(merged=True, merge_commit_sha=BASE, state="closed")
                    return {"merged": True, "sha": MERGE}
                return self.response(method, endpoint, payload)
            with self.subTest(behavior=behavior), patch.object(self, "transport", side_effect=uncertain):
                result = github.merge(settings(), REPO, 2, HEAD, transport=self.transport)
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(writes), 1)

    def test_fixed_blob_unicode_pages_and_symlink_rejection(self):
        raw = ("x" * 16383 + "\u00e9tail").encode()
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        values = [{"sha": HEAD, "commit": {"tree": {"sha": TREE}}},
                  {"sha": TREE, "truncated": False, "tree": [{"path": "code.py", "mode": "100644", "sha": blob}]},
                  {"sha": blob, "encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode()}]
        with patch.object(self, "transport", side_effect=copy.deepcopy(values) * 2):
            first = evidence.read_file(REPO, HEAD, "code.py", 0, transport=self.transport)
            second = evidence.read_file(REPO, HEAD, "code.py", first["next_offset"], transport=self.transport)
        self.assertEqual((first["text"] + second["text"]).encode(), raw)
        self.assertEqual(first["sha256"], hashlib.sha256(raw).hexdigest())
        values[1]["tree"][0]["mode"] = "120000"
        with patch.object(self, "transport", side_effect=values) as api:
            self.assert_fault("github_unsupported_file", evidence.read_file, REPO, HEAD, "code.py", 0, transport=self.transport)
            self.assertEqual(api.call_count, 2)
        with patch.object(self, "transport") as api:
            self.assert_fault("invalid_github_request", evidence.read_file, REPO, HEAD, "../code.py", 0, transport=self.transport)
            for path in (".env", ".ENV", "Secrets.toml", "ID_RSA", "config/.EnV.local"):
                self.assert_fault("invalid_github_request", evidence.read_file, REPO, HEAD, path, 0, transport=self.transport)
            api.assert_not_called()

    def test_short_counted_page_and_unknown_review_evidence_fail_closed(self):
        for response in ({"total_count": 2, "check_runs": [{"id": 1}]}, {"check_runs": []}):
            with self.subTest(response=response), patch.object(self, "transport", return_value=response):
                with self.assertRaises(Fault):
                    client.list_page("endpoint", 1, 100, "check_runs", transport=self.transport)
        for malformed in ({"id": None}, {"state": "UNRECOGNIZED"}, {"user": "unavailable"}):
            original = self.reviews
            self.reviews = original + [dict(original[0], **malformed)]
            self.assert_fault("github_evidence_unavailable", evidence.merge_gate, REPO, 2, HEAD, transport=self.transport)
            self.reviews = original
        self.policy["reviewDecision"] = "UNRECOGNIZED"
        self.assert_fault("github_evidence_unavailable", evidence.merge_gate, REPO, 2, HEAD, transport=self.transport)

    def test_public_redaction_and_response_limit_do_not_claim_complete_evidence(self):
        value = operations._public({"body": "ghp_" + "x" * 32})
        self.assertTrue(value["content_redacted"])
        self.assertIn("[REDACTED]", value["body"])
        self.assert_fault("github_output_too_large", operations._public, {"body": "x" * operations.MAX_RESULT_BYTES})



class ProjectReleaseTests(GitHubCaseFixture, unittest.TestCase):
    def test_publish_draft_only_changes_flag_and_preserves_metadata(self):
        tag, body = "v1.2.7", "Release notes"
        release = dict(id=91, tag_name=tag, body=body, name="Named",
                       draft=True, prerelease=True, target_commitish=HEAD,
                       author={"id": ACTOR["id"] + 1})
        original, writes = copy.deepcopy(release), []
        digest = hashlib.sha256(body.encode()).hexdigest()

        def api(method, endpoint, payload=None):
            if method == "GET" and endpoint in (f"{ROOT}/releases/91", f"{ROOT}/releases/tags/{tag}"):
                return copy.deepcopy(release)
            if method == "PATCH" and endpoint == f"{ROOT}/releases/91":
                writes.append(payload)
                release.update(payload)
                return copy.deepcopy(release)
            return self.response(method, endpoint, payload)

        with patch.object(self, "transport", side_effect=api):
            read = github.read(settings(), "release", REPO, ref=tag, transport=self.transport)
            self.assertEqual(read["data"]["body_sha256"], digest)
            result = github.release(settings(), "publish", REPO, tag, release_id=91,
                                    expected_body_sha256=digest, transport=self.transport)
            self.assertEqual(result["status"], "verified")
        self.assertEqual(writes, [{"draft": False}])
        self.assertEqual(result["receipt"]["draft"], False)
        self.assertEqual({key: release[key] for key in original if key != "draft"},
                         {key: original[key] for key in original if key != "draft"})

    def test_publish_preflight_rejects_stale_drafts_and_invalid_inputs(self):
        tag, body = "v1.2.8", "Changelog"
        release = dict(id=92, tag_name=tag, body=body, draft=True)
        digest = hashlib.sha256(body.encode()).hexdigest()

        def api(method, endpoint, payload=None):
            if method == "GET" and endpoint == f"{ROOT}/releases/92":
                return copy.deepcopy(release)
            return self.response(method, endpoint, payload)

        with patch.object(self, "transport", side_effect=api) as calls:
            for code, kwargs in (
                ("invalid_github_request", dict(release_id=0, expected_body_sha256=digest)),
                ("invalid_github_request", dict(release_id=92, expected_body_sha256="")),
                ("invalid_github_request", dict(release_id=92, expected_body_sha256=digest, body="changed")),
                ("github_object_changed", dict(release_id=92, expected_body_sha256="0" * 64)),
            ):
                with self.subTest(kwargs=kwargs):
                    self.assert_fault(code, github.release, settings(), "publish", REPO, tag,
                                      transport=self.transport, **kwargs)
            self.assert_fault("github_object_changed", github.release, settings(), "publish",
                              REPO, "wrong", release_id=92, expected_body_sha256=digest,
                              transport=self.transport)
            release["draft"] = False
            self.assert_fault("github_conflict", github.release, settings(), "publish",
                              REPO, tag, release_id=92, expected_body_sha256=digest,
                              transport=self.transport)
            self.assertFalse(any(call.args[0] in {"POST", "PATCH"} for call in calls.call_args_list))

    def test_publish_uncertain_response_reconciles_exact_original_destination(self):
        tag, body = "v1.2.9", "Changelog"
        release = dict(id=93, tag_name=tag, body=body, draft=True)
        digest = hashlib.sha256(body.encode()).hexdigest()
        writes = []

        def api(method, endpoint, payload=None):
            if method == "GET" and endpoint == f"{ROOT}/releases/93":
                return copy.deepcopy(release)
            if method == "PATCH" and endpoint == f"{ROOT}/releases/93":
                writes.append(payload)
                release["draft"] = False
                raise OSError("Lost response after write")
            return self.response(method, endpoint, payload)

        locator = dict(self.task_locator("release", 93), tag_name=tag)
        request = dict(action="publish", tag_name=tag, release_id=93, expected_body_sha256=digest)
        with patch.object(self, "transport", side_effect=api):
            result = github.release(settings(), "publish", REPO, tag, release_id=93,
                                    expected_body_sha256=digest, transport=self.transport)
            self.assertEqual(result["status"], "write_outcome_unknown")
            self.assertEqual(github.reconcile(settings(), locator, request,
                                              transport=self.transport)["status"], "observed")
            self.assertEqual(github.reconcile(settings(), locator, dict(request, release_id=99),
                                              transport=self.transport)["status"], "observed")
        self.assertEqual(writes, [{"draft": False}])

    def test_publish_changed_readback_never_claims_success(self):
        tag, body = "v1.2.10", "Changelog"
        release = dict(id=94, tag_name=tag, body=body, name="Original", draft=True)
        writes = []

        def api(method, endpoint, payload=None):
            if method == "GET" and endpoint == f"{ROOT}/releases/94":
                return copy.deepcopy(release)
            if method == "PATCH" and endpoint == f"{ROOT}/releases/94":
                writes.append(payload)
                release.update(draft=False, name="Changed during publication")
                return copy.deepcopy(release)
            return self.response(method, endpoint, payload)

        with patch.object(self, "transport", side_effect=api):
            result = github.release(settings(), "publish", REPO, tag, release_id=94,
                                    expected_body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                                    transport=self.transport)
            locator = dict(self.task_locator("release", 94), tag_name=tag)
            request = dict(action="publish", tag_name=tag, release_id=94,
                           expected_body_sha256=hashlib.sha256(body.encode()).hexdigest())
            recovered = github.reconcile(settings(), locator, request, transport=self.transport)
        self.assertEqual(result["status"], "write_unverified")
        self.assertEqual(recovered["status"], "observed")
        self.assertFalse(recovered["fields_verified"])
        self.assertEqual(writes, [{"draft": False}])

    def test_create_release_verifies_draft_and_prevents_duplicate(self):
        published, posts = {}, []
        tag = "v1.2.3"

        def api(method, endpoint, payload=None):
            if method == "GET" and endpoint.startswith(f"{ROOT}/releases?"):
                return [copy.deepcopy(published)] if published else []
            if method == "POST" and endpoint == f"{ROOT}/releases":
                posts.append(payload)
                published.update(id=42, html_url="https://example.invalid/release/42",
                                 author=ACTOR, **payload)
                return copy.deepcopy(published)
            if method == "GET" and endpoint == f"{ROOT}/releases/42":
                return copy.deepcopy(published)
            return self.response(method, endpoint, payload)

        with patch.object(self, "transport", side_effect=api):
            result = github.release(settings(), "create", REPO, tag, name="Release 1.2.3",
                                    body="Release notes", transport=self.transport)
            self.assertEqual(result["status"], "verified")
            self.assertEqual((result["receipt"]["id"], result["receipt"]["draft"]), (42, True))
            self.assertTrue(result["body_verified"])
            self.assertIn(CREATED, posts[0]["body"])
            self.assert_fault("github_conflict", github.release, settings(), "create",
                              REPO, tag, body="Different notes", transport=self.transport)
        self.assertEqual(len(posts), 1)

    def test_release_create_invalid_inputs_do_not_touch_provider(self):
        with patch.object(self, "transport") as api:
            for tag, body, draft in (("", "Notes", True), ("v..1", "Notes", True),
                                     ("v1", "", True), ("v1", "Notes", "no")):
                self.assert_fault("invalid_github_request", github.release, settings(),
                                  "create", REPO, tag, body=body, draft=draft,
                                  transport=self.transport)
            api.assert_not_called()

    def test_release_readback_requires_original_author(self):
        created = {}
        def api(method, endpoint, payload=None):
            if method == "GET" and endpoint.startswith(f"{ROOT}/releases?"):
                return []
            if method == "POST" and endpoint == f"{ROOT}/releases":
                created.update(id=47, author=ACTOR, **payload)
                return copy.deepcopy(created)
            if method == "GET" and endpoint == f"{ROOT}/releases/47":
                return {**created, "author": {"id": ACTOR["id"] + 1}}
            return self.response(method, endpoint, payload)
        with patch.object(self, "transport", side_effect=api) as calls:
            observed = github.release(settings(), "create", REPO, "v1.2.6",
                                      body="Notes", transport=self.transport)
            self.assertEqual(observed["status"], "write_unverified")
            self.assertEqual(observed["receipt"]["id"], 47)
            self.assertEqual(sum(c.args[0] == "POST" for c in calls.call_args_list), 1)

    def test_release_lost_response_reconciles_by_tag_with_marker(self):
        tag, published, posts = "v1.2.4", {}, []
        marker = "<!-- om-task:fixture:" + "e" * 32 + " -->"
        body = "Changelog.\n\n" + marker

        def api(method, endpoint, payload=None):
            if method == "GET" and endpoint.startswith(f"{ROOT}/releases?"):
                return [copy.deepcopy(published)] if published else []
            if method == "POST" and endpoint == f"{ROOT}/releases":
                posts.append(payload)
                published.update(id=73, html_url="https://example.invalid/release/73",
                                 author=ACTOR, **payload)
                raise OSError("Response lost after publication")
            return self.response(method, endpoint, payload)

        locator = dict(self.task_locator("release"), tag_name=tag)
        request = dict(action="create", tag_name=tag, name="", body=body, draft=True, prerelease=False)
        with patch.object(self, "transport", side_effect=api):
            unknown = github.release(settings(), "create", REPO, tag, body=body, transport=self.transport)
            self.assertEqual(unknown["status"], "write_outcome_unknown")
            observed = github.reconcile(settings(), locator, request, transport=self.transport)
            self.assertEqual((observed["status"], observed["receipt"]["id"]), ("verified", 73))
            self.assertEqual(github.reconcile(settings(), locator, dict(request, draft=False),
                                              transport=self.transport)["status"], "observed")
            self.assertEqual(github.reconcile(settings(), locator, transport=self.transport)["status"], "observed")
            self.assert_fault("github_conflict", github.release, settings(), "create",
                              REPO, tag, body=body, transport=self.transport)
        self.assertEqual(len(posts), 1)

    def test_release_create_fails_closed_after_bounded_incomplete_lookup(self):
        def api(method, endpoint, payload=None):
            if method == "GET" and endpoint.startswith(f"{ROOT}/releases?"):
                return [{"id": i + 1, "tag_name": f"other-{i}"} for i in range(100)]
            return self.response(method, endpoint, payload)
        with patch.object(self, "transport", side_effect=api) as calls:
            self.assert_fault("github_evidence_unavailable", github.release, settings(),
                              "create", REPO, "v1.2.3", body="Notes", transport=self.transport)
            self.assertTrue(all(call.args[0] == "GET" for call in calls.call_args_list))

    def test_release_tags_and_document_history_use_bounded_rest(self):
        calls = []

        def api(method, endpoint, payload=None):
            calls.append(endpoint)
            if endpoint.startswith(f"{ROOT}/releases/tags/"):
                return {"id": 1, "tag_name": "v0.2.18", "body": "Version notes", "html_url": "https://example.invalid/tag"}
            if endpoint.startswith(f"{ROOT}/releases?"):
                return [{"id": 1, "tag_name": "v0.2.18", "body": "Version notes"}]
            if endpoint.startswith(f"{ROOT}/commits?"):
                return [{"sha": HEAD, "html_url": "https://example.invalid/commit",
                         "commit": {"message": "Document update", "committer": {"date": STAMP}}}]
            return self.response(method, endpoint, payload)

        with patch.object(self, "transport", side_effect=api):
            listing = github.read(settings(), "releases", REPO, limit=3, transport=self.transport)
            release = github.read(settings(), "release", REPO, ref="v0.2.18", transport=self.transport)
            history = github.read(settings(), "file_history", REPO, path="docs/release notes.md",
                                  ref="v0.2.18", transport=self.transport)
            self.assert_fault("invalid_github_request", github.read, settings(), "file_history",
                              REPO, path="../secrets", transport=self.transport)
        self.assertEqual(listing["data"][0]["tag_name"], "v0.2.18")
        self.assertFalse(listing["has_more"])
        self.assertEqual(release["data"]["body"], "Version notes")
        self.assertEqual(history["data"][0]["sha"], HEAD)
        self.assertTrue(any("path=docs%2Frelease%20notes.md&sha=v0.2.18" in call for call in calls))
        self.assertTrue(any("/releases/tags/v0.2.18" in call for call in calls))

    def test_project_listing_and_item_fields_are_cursor_paged(self):
        requests = []

        def api(method, endpoint, payload=None):
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            requests.append(payload["variables"])
            query = payload["query"]
            if "projectsV2(first:" in query:
                return {"data": {"organization": {"projectsV2": {"nodes": [{"id": "PVT_1", "number": 1}],
                    "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}}
            if "fieldValues(first:100)" in query:
                return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                    "fieldValues": {"nodes": [{"text": "triage", "field": {"id": "PVTF_1"}}],
                                    "pageInfo": {"hasNextPage": False}}}}}
            if "fields(first:" in query:
                self.assertIn("configuration{iterations{id title}}", query)
                return {"data": {"organization": {"projectV2": {"fields": {
                    "nodes": [{"id": "PVTF_1", "name": "Status"},
                              {"id": "PVTF_2", "name": "Sprint",
                               "configuration": {"iterations": [{"id": "ITER_1", "title": "Sprint 1"}]}}],
                    "pageInfo": {"hasNextPage": False}}}}}}
            return {"data": {"organization": {"projectV2": {
                "id": "PVT_1", "number": 1, "title": "Dashboard", "url": "https://example.invalid/project"}}}}

        with patch.object(self, "transport", side_effect=api):
            listing = github.read(settings(), "projects", REPO, limit=1, transport=self.transport)
            fields = github.read(settings(), "project_fields", REPO, number=1, transport=self.transport)
            item = github.read(settings(), "project_item", REPO, number=1, ref="PVTI_1", transport=self.transport)
            self.assert_fault("invalid_github_request", github.read, settings(), "project_items",
                              REPO, number=1, page=2, transport=self.transport)
        self.assertEqual(listing["next_cursor"], "next")
        self.assertEqual(fields["data"][0]["id"], "PVTF_1")
        self.assertEqual(fields["data"][1]["configuration"]["iterations"][0]["id"], "ITER_1")
        self.assertEqual(item["data"]["field_values"][0]["text"], "triage")
        self.assertTrue(item["data"]["fields_complete"])
        self.assertEqual(requests[0]["owner"], "Owner")

    def test_project_field_recovery_requires_exact_complete_destination_without_writes(self):
        locator = self.task_locator("project", 1)
        request = dict(action="update_field", repository=REPO, project_number=1,
                       item_id="PVTI_1", field_id="PVTF_1", value={"date": "2026-01-02"})
        item = {"id": "PVTI_1", "project": {"id": "PVT_1"},
                "content": {"id": "ISSUE_1", "repository": {"nameWithOwner": REPO}},
                "fieldValues": {"nodes": [{"date": "2026-01-02", "field": {"id": "PVTF_1"}}],
                                "pageInfo": {"hasNextPage": False}}}

        def api(method, endpoint, payload=None):
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            self.assertTrue(payload["query"].startswith("query("))
            if "fieldValues(first:100)" in payload["query"]:
                return {"data": {"node": item}}
            return {"data": {"organization": {"projectV2": {"id": "PVT_1", "number": 1}}}}

        with patch.object(self, "transport", side_effect=api):
            self.assertEqual(github.reconcile(settings(), locator, transport=self.transport)["status"],
                             "write_outcome_unknown")
            for mismatch in (dict(request, project_number=2), dict(request, repository="Other/repo")):
                self.assertEqual(github.reconcile(settings(), locator, mismatch, transport=self.transport)["status"],
                                 "write_outcome_unknown")
            result = github.reconcile(settings(), locator, request, transport=self.transport)
            self.assertEqual((result["status"], result["fields_verified"]), ("verified", True))
            self.assertEqual(result["receipt"], {"id": "PVTI_1", "project_number": 1})
            with patch.dict(item["fieldValues"]["pageInfo"], hasNextPage=True):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "observed")
            with patch.dict(item["fieldValues"]["nodes"][0], date="2026-01-03"):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "observed")
            with patch.dict(item, content=None):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "observed")
            with patch.dict(item["content"]["repository"], nameWithOwner="Other/repo"):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "observed")
            with patch.dict(item, project={"id": "PVT_other"}):
                self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "write_outcome_unknown")
            with patch.object(connection, "read_actor", side_effect=[ACTOR, {"id": 99, "login": "other"}]):
                self.assert_fault("github_identity_changed", github.reconcile, settings(), locator, request, transport=self.transport)

    def test_project_add_recovery_finds_original_content_with_bounded_read_only_pages(self):
        self.issue_value["node_id"] = "ISSUE_1"
        locator = self.task_locator("project", 1)
        for selector, content_id in (({"issue_number": 1}, "ISSUE_1"),
                                     ({"pull_request_number": 2}, self.pr["node_id"])):
            with self.subTest(selector=selector):
                cursors, found = [], {"enabled": True}

                def api(method, endpoint, payload=None):
                    if endpoint != "graphql":
                        return self.response(method, endpoint, payload)
                    query = payload["query"]
                    self.assertTrue(query.startswith("query("))
                    if "projectItems(first:" in query:
                        cursor = payload["variables"]["cursor"]
                        cursors.append(cursor)
                        nodes = [{"id": "PVTI_1", "project": {"id": "PVT_1"}}] if cursor and found["enabled"] else []
                        return {"data": {"node": {"id": content_id, "projectItems": {"nodes": nodes,
                            "pageInfo": {"hasNextPage": not bool(nodes), "endCursor": str(len(cursors))}}}}}
                    if "fieldValues(first:100)" in query:
                        return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                            "content": {"id": content_id, "repository": {"nameWithOwner": REPO}},
                            "fieldValues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}
                    return {"data": {"organization": {"projectV2": {"id": "PVT_1", "number": 1}}}}

                request = dict(action="add_item", repository=REPO, project_number=1, **selector)
                with patch.object(self, "transport", side_effect=api):
                    self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "verified")
                    self.assertEqual(cursors, [None, "1"])
                    cursors.clear()
                    found["enabled"] = False
                    self.assertEqual(github.reconcile(settings(), locator, request, transport=self.transport)["status"], "write_outcome_unknown")
                    self.assertEqual(len(cursors), 10)

    def test_project_add_and_single_field_write_verify_destination(self):
        self.issue_value["node_id"] = "ISSUE_1"
        field = {"text": "triage"}
        mutations = []

        def api(method, endpoint, payload=None):
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            query = payload["query"]
            if "mutation(" in query:
                mutations.append(copy.deepcopy(payload))
                if "addProjectV2ItemById" in query:
                    return {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_1"}}}}
                field.update(payload["variables"]["input"]["value"])
                return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_1"}}}}
            if "fieldValues(first:100)" in query:
                return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                    "content": {"id": "ISSUE_1", "repository": {"nameWithOwner": REPO}}, "fieldValues": {
                        "nodes": [{"text": field["text"], "field": {"id": "PVTF_1"}}],
                        "pageInfo": {"hasNextPage": False}}}}}
            return {"data": {"organization": {"projectV2": {"id": "PVT_1", "number": 1}}}}

        with patch.object(self, "transport", side_effect=api):
            added = github.project(settings(), "add_item", REPO, 1, issue_number=1, transport=self.transport)
            updated = github.project(settings(), "update_field", REPO, 1, item_id="PVTI_1",
                                     field_id="PVTF_1", value={"text": "done"},
                                     expected_value={"text": "triage"}, transport=self.transport)
            self.assert_fault("github_object_changed", github.project, settings(), "update_field",
                              REPO, 1, item_id="PVTI_1", field_id="PVTF_1",
                              value={"text": "again"}, expected_value={"text": "triage"},
                              transport=self.transport)
            self.assert_fault("invalid_github_request", github.project, settings(), "update_field",
                              REPO, 1, item_id="PVTI_1", field_id="PVTF_1",
                              value={"number": True}, transport=self.transport)
        self.assertEqual((added["status"], updated["status"]), ("verified", "verified"))
        self.assertEqual(len(mutations), 2)
        self.assertEqual(mutations[1]["variables"]["input"]["fieldId"], "PVTF_1")
        self.assertEqual(field, {"text": "done"})

    def test_project_add_pull_request_verifies_repository_and_destination(self):
        self.pr["base"]["repo"] = {"full_name": REPO}
        writes = []
        returned_id = {"id": self.pr["node_id"]}

        def api(method, endpoint, payload=None):
            if endpoint == f"{ROOT}/issues/2":
                return {**self.pr, "pull_request": {"url": f"{ROOT}/pulls/2"}}
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            query = payload["query"]
            if "addProjectV2ItemById" in query:
                writes.append(payload["variables"]["input"])
                return {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_1"}}}}
            if "fieldValues(first:100)" in query:
                return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                    "content": {**returned_id, "repository": {"nameWithOwner": REPO}},
                    "fieldValues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}
            return {"data": {"organization": {"projectV2": {"id": "PVT_1"}}}}

        with patch.object(self, "transport", side_effect=api) as transport:
            added = github.project(settings(), "add_item", REPO, 1,
                                   pull_request_number=2, transport=self.transport)
            self.assertEqual((added["status"], added["fields_verified"]), ("verified", True))
            self.assertIn(("GET", f"{ROOT}/pulls/2"), [(c.args[0], c.args[1]) for c in transport.call_args_list])
            self.assertNotIn(("GET", f"{ROOT}/issues/2"), [(c.args[0], c.args[1]) for c in transport.call_args_list])
            self.assertEqual(writes, [{"projectId": "PVT_1", "contentId": self.pr["node_id"]}])
            returned_id["id"] = "PR_wrong"
            self.assertEqual(github.project(settings(), "add_item", REPO, 1,
                             pull_request_number=2, transport=self.transport)["status"], "write_unverified")
            self.assertEqual(len(writes), 2)

            for arguments in ({"issue_number": 1, "pull_request_number": 2},
                              {}, {"pull_request_number": -1},
                              {"pull_request_number": True}, {"issue_number": 2},
                              {"issue_number": 0, "pull_request_number": 0}):
                with self.subTest(arguments=arguments):
                    self.assert_fault("invalid_github_request", github.project, settings(),
                                      "add_item", REPO, 1, transport=self.transport, **arguments)
            self.assert_fault("invalid_github_request", github.project, settings(),
                              "update_field", REPO, 1, pull_request_number=2, item_id="PVTI_1",
                              field_id="PVTF_1", value={"text": "done"}, transport=self.transport)
            self.assertEqual(len(writes), 2)
            self.pr["base"]["repo"]["full_name"] = "Other/repo"
            self.assert_fault("github_evidence_unavailable", github.project, settings(),
                              "add_item", REPO, 1, pull_request_number=2, transport=self.transport)
            self.assertEqual(len(writes), 2)

    def test_project_update_rejects_items_from_other_repositories_and_drafts(self):
        for content in ({"id": "ISSUE_OTHER", "repository": {"nameWithOwner": "Owner/other"}},
                        {"id": "ISSUE_OTHER", "repository": {"nameWithOwner": "Other/repo"}},
                        None):
            writes = []

            def api(method, endpoint, payload=None):
                if endpoint != "graphql":
                    return self.response(method, endpoint, payload)
                if "mutation(" in payload["query"]:
                    writes.append(payload)
                    self.fail("No mutation is allowed for foreign-repository or draft items.")
                if "fieldValues(first:100)" in payload["query"]:
                    return {"data": {"node": {"id": "PVTI_1", "project": {"id": "PVT_1"},
                        "content": content, "fieldValues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}
                return {"data": {"organization": {"projectV2": {"id": "PVT_1"}}}}

            with self.subTest(content=content), patch.object(self, "transport", side_effect=api):
                self.assert_fault("github_not_allowed", github.project, settings(), "update_field",
                                  REPO, 1, item_id="PVTI_1", field_id="PVTF_1",
                                  value={"text": "changed"}, transport=self.transport)
            self.assertEqual(writes, [])

    def test_project_lost_mutation_acknowledgment_is_not_retried(self):
        self.issue_value["node_id"] = "ISSUE_1"
        writes = []

        def api(method, endpoint, payload=None):
            if endpoint != "graphql":
                return self.response(method, endpoint, payload)
            if "mutation(" in payload["query"]:
                writes.append(payload)
                raise OSError("Lost write acknowledgment")
            return {"data": {"organization": {"projectV2": {"id": "PVT_1"}}}}

        with patch.object(self, "transport", side_effect=api):
            result = github.project(settings(), "add_item", REPO, 1, issue_number=1, transport=self.transport)
            locator = dict(self.task_locator("project", 1))
            observed = github.reconcile(settings(), locator, transport=self.transport)
        self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertEqual(observed["status"], "write_outcome_unknown")
        self.assertEqual(len(writes), 1)


if __name__ == "__main__":
    unittest.main()
