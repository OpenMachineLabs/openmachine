"""Shared disposable fixtures; no test cases or test discovery."""

import copy
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault

REPO = "Owner/repo"

ROOT = f"repos/{REPO}"

HEAD, BASE, MERGE, TREE = (char * 40 for char in "abcd")

ACTOR = {"id": 1, "login": "Operator"}

STAMP = "2026-01-01T00:00:00Z"

CREATED = "---\n\nCreated via [Open Machine](https://github.com/OpenMachineLabs/openmachine)."

POSTED = "---\n\nPosted via [Open Machine](https://github.com/OpenMachineLabs/openmachine)."

def settings(attribution=None, **scopes):
    config = scopes or {"repositories": [REPO]}
    if attribution is not None:
        config["attribution"] = attribution
    return Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}}, "github": config})

class GitHubCaseFixture(object):
    def setUp(self):
        self.metadata = {"id": 100, "full_name": REPO, "private": False, "default_branch": "main"}
        self.pr = {"id": 202, "node_id": "PR_fixture", "number": 2, "state": "open", "draft": False, "title": "old", "body": "legacy",
                   "head": {"sha": HEAD, "ref": "topic", "label": "Owner:topic"},
                   "base": {"sha": BASE, "ref": "main", "label": "Owner:main"},
                   "user": ACTOR, "assignees": [], "labels": [], "updated_at": STAMP, "changed_files": 2}
        self.issue_value = {"id": 101, "number": 1, "title": "old", "body": "legacy", "state": "open",
                            "assignees": [], "labels": [], "updated_at": STAMP}
        self.comments = []
        self.reviews = [{"id": 9, "body": "review", "state": "COMMENTED", "commit_id": HEAD,
                         "submitted_at": STAMP, "user": ACTOR, "html_url": "https://example.invalid/review/9"}]
        self.runs, self.statuses, self.threads = [], [], []
        self.policy = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                       "reviewDecision": None, "mergeQueueEntry": None}
        self.files = [{"filename": "a.py", "status": "modified", "sha": TREE, "changes": 1, "patch": "@@ -1 +1 @@\n-a\n+b"},
                      {"filename": "b.py", "status": "modified", "sha": TREE, "changes": 1}]
        self.transport = Mock(side_effect=self.response)

    def response(self, method, endpoint, payload=None):
        parsed = urlsplit(endpoint)
        query = parse_qs(parsed.query)
        page, limit = int(query.get("page", [1])[0]), int(query.get("per_page", [100])[0])
        def portion(items):
            return copy.deepcopy(items[(page - 1) * limit:page * limit])
        if method == "GET":
            if parsed.path == f"{ROOT}/pulls/2/reviews/9":
                return copy.deepcopy(self.reviews[0])
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
            if "markPullRequestReadyForReview" in payload["query"]:
                self.assertEqual(payload["variables"], {"pullRequestId": self.pr["node_id"]})
                self.pr["draft"] = False
                return {"data": {"markPullRequestReadyForReview": {"pullRequest": {
                    "id": self.pr["node_id"], "number": self.pr["number"], "isDraft": False}}}}
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
