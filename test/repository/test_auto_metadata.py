"""Regression coverage for the default-branch metadata automation."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("auto_metadata", ROOT / "scripts/auto_metadata.py")
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


def issue(number, title, *, state="open", closed_at=None, labels=(), assignees=(), body=""):
    return {
        "number": number, "title": title, "body": body, "state": state,
        "closed_at": closed_at,
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": login} for login in assignees],
    }


class FakeGitHub:
    repo = "OpenMachineLabs/openmachine"

    def __init__(self, objects=None, files=(), linked=(), closers=(), assignable=True):
        self.objects = objects or {}
        self.files = list(files)
        self.linked = list(linked)
        self.closers = list(closers)
        self.can_assign = assignable
        self.writes = []
        self.lookup_count = 0

    def get(self, path):
        if "/files?" in path:
            page = int(path.split("page=")[-1])
            return [{"filename": name} for name in self.files[(page - 1) * 100:page * 100]]
        return self.objects[path]

    def post(self, path, payload):
        self.writes.append((path, payload))
        base, kind = path.rsplit("/", 1)
        if kind == "labels":
            self.objects[base]["labels"].extend({"name": label} for label in payload["labels"])
        elif kind == "assignees" and self.can_assign:
            self.objects[base]["assignees"].extend({"login": login} for login in payload["assignees"])
        return self.objects[base]

    def graphql(self, query, number):
        self.lookup_count += 1
        if query == metadata.ISSUE_REFERENCES:
            return {"issue": {"closedByPullRequestsReferences": {
                "nodes": self.closers, "pageInfo": {"hasNextPage": False},
            }}}
        return {"pullRequest": {"closingIssuesReferences": {
            "nodes": [{"number": n} for n in self.linked],
            "pageInfo": {"hasNextPage": False},
        }}}

class ClassificationTests(unittest.TestCase):
    def test_issue_templates_and_explicit_body_classification(self):
        for prefix, label in metadata.ISSUE_PREFIXES.items():
            with self.subTest(prefix=prefix):
                self.assertEqual(metadata.issue_label(issue(1, f"[{prefix}] runtime(scope): describe it")), label)
        text = "## Project metadata\n- Primary label: documentation\n\n## Acceptance\n"
        self.assertEqual(metadata.issue_label(issue(1, "runtime(scope): document a usage change", body=text)),
                         "documentation")
        self.assertIsNone(metadata.issue_label(issue(1, "docs(scope): update the guide",
                                                body=text.replace("documentation", "bug"))))
        self.assertIsNone(metadata.issue_label(issue(1, "runtime(scope): unspecified task")))

    def test_pr_paths_and_title_must_not_disagree(self):
        self.assertEqual(metadata.pull_label({"title": "docs(repo): explain it"}, ["docs/guide.md"]),
                         "documentation")
        self.assertEqual(metadata.pull_label({"title": "ci(repo): guard metadata"},
                                             [".github/workflows/main.yml", "scripts/check.py", "test/t.py"]),
                         "maintenance")
        self.assertIsNone(metadata.pull_label({"title": "docs(repo): explain it"}, ["scripts/tool.py"]))
        self.assertIsNone(metadata.pull_label({"title": "runtime(repo): change logic"},
                                               ["docs/guide.md", "openmachine/app.py"]))
        self.assertEqual(metadata.pull_label({"title": "fix(runtime): avoid crash"},
                                             ["openmachine/app.py", "test/unit/test_app.py"]), "bug")

    def test_bots_and_invalid_authors_never_become_assignees(self):
        self.assertEqual(metadata.human_login({"type": "User", "login": "user-name"}), "user-name")
        for author in ({"type": "Bot", "login": "dependabot[bot]"},
                       {"type": "User", "login": "invalid user"},
                       {"type": "Mannequin", "login": "ghost"}, None):
            self.assertIsNone(metadata.human_login(author))

    def test_existing_metadata_is_never_replaced(self):
        item = issue(12, "docs(repo): documentation", labels=["bug", "custom"],
                     assignees=["maintainer"])
        api = FakeGitHub({f"repos/{FakeGitHub.repo}/issues/12": item})
        metadata.fill_missing(api, item, "documentation", "contributor")
        self.assertEqual(api.writes, [])

    def test_missing_primary_label_is_added_without_deleting_unrelated_labels(self):
        item = issue(12, "docs(repo): documentation", labels=["custom"])
        api = FakeGitHub({f"repos/{FakeGitHub.repo}/issues/12": item})
        metadata.fill_missing(api, item, "documentation")
        self.assertEqual(item["labels"], [{"name": "custom"}, {"name": "documentation"}])
        metadata.fill_missing(api, item, "documentation")
        self.assertEqual(len(api.writes), 1)

    def test_provider_404_or_422_leaves_assignment_empty(self):
        from urllib.error import HTTPError

        class Rejected(FakeGitHub):
            def post(self, path, payload):
                raise HTTPError(path, self.status, "not assignable", {}, None)

        for status in (404, 422):
            with self.subTest(status=status):
                item = issue(12, "runtime(scope): unknown")
                api = Rejected(assignable=False)
                api.status = status
                metadata.fill_missing(api, item, None, "contributor")
                self.assertEqual(item["assignees"], [])

    def test_provider_rejection_does_not_claim_assignment(self):
        item = issue(12, "runtime(scope): unknown")
        api = FakeGitHub({f"repos/{FakeGitHub.repo}/issues/12": item}, assignable=False)
        metadata.fill_missing(api, item, None, "contributor")
        self.assertEqual(item["assignees"], [])
        self.assertEqual(api.writes, [(
            f"repos/{FakeGitHub.repo}/issues/12/assignees",
            {"assignees": ["contributor"]},
        )])


class ContributorTests(unittest.TestCase):
    def test_completed_issue_requires_merge_near_close_and_unique_human(self):
        item = issue(8, "[BUG] runtime(scope): reproduce a crash", state="closed",
                     closed_at="2026-10-16T07:55:00Z")
        near = {"mergedAt": "2026-10-16T07:54:55Z",
                "author": {"__typename": "User", "login": "contributor"}}
        api = FakeGitHub(closers=[near])
        self.assertEqual(metadata.merged_issue_author(api, item), "contributor")
        api.closers = [near, {**near, "author": {"__typename": "User", "login": "different"}}]
        self.assertIsNone(metadata.merged_issue_author(api, item))
        api.closers = [{**near, "mergedAt": "2026-10-15T07:55:00Z"}]
        self.assertIsNone(metadata.merged_issue_author(api, item))
        api.closers = [{**near, "author": {"__typename": "Bot", "login": "ci-bot"}}]
        self.assertIsNone(metadata.merged_issue_author(api, item))

    def test_open_issue_never_infers_owner_from_reporter(self):
        item = issue(8, "runtime(scope): something")
        item["user"] = {"type": "User", "login": "reporter"}
        path = f"repos/{FakeGitHub.repo}/issues/8"
        api = FakeGitHub({path: item})
        metadata.process_issue(api, 8)
        self.assertFalse(api.writes)
        self.assertEqual(api.lookup_count, 0)

    def test_merged_pr_fills_both_pr_and_its_closed_issue(self):
        base = f"repos/{FakeGitHub.repo}"
        pull = {
            "number": 17, "title": "ci(metadata): fill missing fields",
            "user": {"type": "User", "login": "contributor"},
            "merged_at": "2026-10-16T07:54:55Z",
        }
        linked = issue(8, "[BUG] runtime(scope): handle the crash", state="closed",
                       closed_at="2026-10-16T07:55:00Z")
        pr_issue = issue(17, pull["title"])
        api = FakeGitHub({
            f"{base}/pulls/17": pull, f"{base}/issues/17": pr_issue,
            f"{base}/issues/8": linked,
        }, files=[".github/workflows/auto-metadata.yml", "scripts/auto_metadata.py"],
            linked=[8], closers=[{
                "mergedAt": "2026-10-16T07:54:55Z",
                "author": {"__typename": "User", "login": "contributor"},
            }])
        metadata.process_pull(api, 17)
        self.assertEqual([item["name"] for item in pr_issue["labels"]], ["maintenance"])
        self.assertEqual([item["login"] for item in pr_issue["assignees"]], ["contributor"])
        self.assertEqual([item["name"] for item in linked["labels"]], ["bug"])
        self.assertEqual([item["login"] for item in linked["assignees"]], ["contributor"])
        count = len(api.writes)
        metadata.process_pull(api, 17)
        self.assertEqual(len(api.writes), count)

    def test_new_merge_does_not_change_an_old_closed_issue(self):
        base = f"repos/{FakeGitHub.repo}"
        pull = {"number": 17, "title": "ci(metadata): maintenance",
                "user": {"type": "User", "login": "contributor"},
                "merged_at": "2026-10-16T07:54:55Z"}
        old = issue(8, "[BUG] runtime(scope): old issue", state="closed",
                    closed_at="2026-09-01T00:00:00Z")
        pr_issue = issue(17, pull["title"])
        api = FakeGitHub({f"{base}/pulls/17": pull, f"{base}/issues/17": pr_issue,
                          f"{base}/issues/8": old},
                         files=["scripts/auto_metadata.py"], linked=[8])
        metadata.process_pull(api, 17)
        self.assertEqual(old["labels"], [])
        self.assertEqual(old["assignees"], [])
        self.assertEqual(api.lookup_count, 1)  # PR lookup only.

    def test_no_historical_scan_or_untrusted_checkout(self):
        workflow = (ROOT / ".github/workflows/auto-metadata.yml").read_text()
        self.assertIn("pull_request_target:", workflow)
        self.assertIn("ref: main", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:\n", workflow)


if __name__ == "__main__":
    unittest.main()
