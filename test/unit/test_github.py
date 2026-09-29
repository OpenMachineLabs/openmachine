"""Focused GitHub attribution policy and readback tests; no network credentials."""

import unittest
from unittest.mock import patch

from openmachine import github
from openmachine.config import Settings
from openmachine.sanitize import Fault

REPO = "Owner/repo"


def settings(attribution=None):
    github_config = {"repositories": [REPO]}
    if attribution is not None:
        github_config["attribution"] = attribution
    return Settings.model_validate({
        "schema_version": 1,
        "machines": {"local": {"transport": "local"}},
        "github": github_config,
    })


class GitHubTests(unittest.TestCase):
    def test_public_default_uses_exact_created_and_posted_footers(self):
        with patch.object(github, "_api", return_value={"private": False}), \
             patch.object(github, "_mutate", return_value={}) as mutate:
            github.issue(settings(), "create", REPO, "title", "body")
            self.assertEqual(mutate.call_args.args[3]["body"], "body\n\n" + github.CREATED_FOOTER)
            github.pull_request(settings(), "create", REPO, "pr", "body", head="topic", base="main")
            self.assertEqual(mutate.call_args.args[3]["body"], "body\n\n" + github.CREATED_FOOTER)
            github.comment(settings(), "create", REPO, "comment", number=1)
            self.assertEqual(mutate.call_args.args[3]["body"], "comment\n\n" + github.POSTED_FOOTER)

    def test_private_default_and_explicit_override(self):
        with patch.object(github, "_api", return_value={"private": True}), \
             patch.object(github, "_mutate", return_value={}) as mutate:
            github.issue(settings(), "create", REPO, "title", "body")
            self.assertEqual(mutate.call_args.args[3]["body"], "body")
        with patch.object(github, "_api") as api, patch.object(github, "_mutate", return_value={}) as mutate:
            github.issue(settings(True), "create", REPO, "title", "body")
            self.assertEqual(mutate.call_args.args[3]["body"], "body\n\n" + github.CREATED_FOOTER)
            api.assert_not_called()
        with patch.object(github, "_api") as api, patch.object(github, "_mutate", return_value={}) as mutate:
            github.comment(settings(False), "create", REPO, "body", number=1)
            self.assertEqual(mutate.call_args.args[3]["body"], "body")
            api.assert_not_called()

    def test_exact_footer_is_idempotent_and_whitespace_is_preserved(self):
        body = "body\n\n" + github.CREATED_FOOTER
        self.assertEqual(github._append(body, github.CREATED_FOOTER), body)
        self.assertEqual(github._append("body\n", github.CREATED_FOOTER), "body\n\n" + github.CREATED_FOOTER)
        self.assertEqual(github._append("body\n\n", github.CREATED_FOOTER), "body\n\n" + github.CREATED_FOOTER)

    def test_updates_preserve_existing_footer_without_branding_legacy_content(self):
        attributed = "old\n\n" + github.CREATED_FOOTER
        with patch.object(github, "_api", return_value={"body": attributed}), \
             patch.object(github, "_mutate", return_value={}) as mutate:
            github.issue(settings(True), "update", REPO, None, "replacement", number=1)
            self.assertEqual(mutate.call_args.args[3]["body"], "replacement\n\n" + github.CREATED_FOOTER)
        with patch.object(github, "_api", return_value={"body": "legacy"}), \
             patch.object(github, "_mutate", return_value={}) as mutate:
            github.pull_request(settings(True), "update", REPO, None, "edited", number=1)
            self.assertEqual(mutate.call_args.args[3]["body"], "edited")

        with patch.object(github, "_api", return_value={"body": attributed}), \
             patch.object(github, "_mutate", return_value={}) as mutate:
            github.comment(settings(True), "update", REPO, github.POSTED_FOOTER, comment_id=9)
            self.assertIn(github.CREATED_FOOTER, mutate.call_args.args[3]["body"])

    def test_review_summary_is_decorated_and_bound_to_exact_head(self):
        head = "a" * 40
        with patch.object(github, "_api", side_effect=[{"state": "open", "head": {"sha": head}}, {"private": False}]), \
             patch.object(github, "_mutate", return_value={}) as mutate:
            github.review(settings(), "create", REPO, "review", number=2, expected_head_sha=head)
        payload = mutate.call_args.args[3]
        self.assertEqual(payload["body"], "review\n\n" + github.POSTED_FOOTER)
        self.assertEqual(payload["commit_id"], head)
        self.assertNotIn("comments", payload)

    def test_mutation_requires_matching_destination_readback(self):
        expected = "body\n\n" + github.CREATED_FOOTER
        with patch.object(github, "_api", side_effect=[{"number": 3, "html_url": "https://example.invalid/3"},
                                                        {"number": 3, "html_url": "https://example.invalid/3", "body": expected}]):
            result = github._mutate(REPO, "POST", "repos/Owner/repo/issues", {"body": expected},
                                    lambda value: f"repos/Owner/repo/issues/{value['number']}")
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["body_verified"])
        with patch.object(github, "_api", side_effect=[{"number": 3}, {"number": 3, "body": "different"}]):
            result = github._mutate(REPO, "POST", "repos/Owner/repo/issues", {"body": expected},
                                    lambda value: f"repos/Owner/repo/issues/{value['number']}")
        self.assertEqual(result["status"], "write_unverified")

    def test_unconfigured_repository_fails_closed(self):
        with self.assertRaises(Fault) as caught:
            github.issue(settings(True), "create", "Other/repo", "title", "body")
        self.assertEqual(caught.exception.code, "github_not_allowed")
