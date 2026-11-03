"""Tests for GitHub release lifecycle at the unit layer."""

import copy
import hashlib
import unittest
from unittest.mock import patch
from integrations import github
from test.support.github_cases import ACTOR, CREATED, GitHubCaseFixture, HEAD, REPO, ROOT, STAMP, settings


class ReleaseTests(GitHubCaseFixture, unittest.TestCase):
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
