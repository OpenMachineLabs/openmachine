"""Regression checks for the public repository's trusted maintainer review gate."""
import copy
import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("trusted_review_gate", ROOT / "scripts/trusted_review_gate.py")
gate = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)
SHA = "a" * 40
NEW_SHA = "b" * 40
REPO = {"id": 1392535998, "full_name": "OpenMachineLabs/openmachine"}
FORK = {"id": 991887, "full_name": "outside/openmachine"}


def pull(user=333333, head_repo=REPO, sha=SHA, draft=False):
    return {"number": 123, "state": "open", "draft": draft, "user": {"id": user},
            "head": {"sha": sha, "repo": head_repo},
            "base": {"ref": "main", "repo": REPO}}


def review(user=70309026, state="APPROVED", commit=SHA):
    return {"user": {"id": user}, "state": state, "commit_id": commit}


class ReviewPolicyTests(unittest.TestCase):
    def check(self, pr, reviews=(), expected="pending"):
        self.assertEqual(gate.decide(pr, list(reviews))[0], expected)

    def test_named_maintainers_can_merge_their_own_internal_pr_without_review(self):
        for user in (70309026, 191080179):
            with self.subTest(user=user):
                self.check(pull(user), expected="success")

    def test_external_author_requires_core_approval(self):
        self.check(pull(), expected="pending")
        self.check(pull(), [review(567890)], expected="pending")
        self.check(pull(), [review(191080179)], expected="success")
        self.check(pull(), [review(70309026)], expected="success")

    def test_maintainer_named_as_author_of_a_fork_does_not_bypass(self):
        self.check(pull(70309026, head_repo=FORK))
        self.check(pull(191080179, head_repo=FORK))
        self.check(pull(191080179, head_repo=FORK), [review()], "success")

    def test_non_core_approvals_cannot_substitute_for_trusted_reviewer(self):
        self.check(pull(), [review(80000), review(90000)], "pending")

    def test_new_push_invalidates_prior_approval(self):
        self.check(pull(sha=NEW_SHA), [review(commit=SHA)], "pending")
        self.check(pull(sha=NEW_SHA), [review(commit=NEW_SHA)], "success")

    def test_dismissed_or_rejected_vote_is_not_an_approval(self):
        self.check(pull(), [review(), review(state="DISMISSED")], "pending")
        self.check(pull(), [review(), review(state="CHANGES_REQUESTED")], "failure")
        self.check(pull(), [review(state="CHANGES_REQUESTED"), review()], "success")

    def test_outstanding_change_request_from_other_maintainer_blocks(self):
        self.check(pull(), [review(), review(191080179, "CHANGES_REQUESTED")], "failure")
        self.check(pull(70309026), [review(191080179, "CHANGES_REQUESTED")], "failure")

    def test_non_decisive_comments_do_not_erase_approval(self):
        self.check(pull(), [review(), review(state="COMMENTED")], "success")

    def test_draft_closed_or_unexpected_target_cannot_pass(self):
        self.check(pull(70309026, draft=True))
        p = pull(70309026)
        p["state"] = "closed"
        self.check(p)
        p = pull(70309026)
        p["base"]["ref"] = "release"
        self.check(p, expected="error")
        p = pull(70309026)
        p["base"]["repo"] = FORK
        self.check(p, expected="error")

    def test_spoofed_author_fields_are_not_accepted(self):
        p = pull(333333)
        p["user"]["login"] = "LuozhuZhang"
        self.check(p)
        p = pull(True)
        self.check(p)
        p = pull(70309026, head_repo={"id": 42, "full_name": "OpenMachineLabs/openmachine"})
        self.check(p)

    def test_malformed_or_unknown_trusted_review_fails_closed(self):
        self.check(pull(), [review(state="SOMETHING_NEW")], "error")
        self.check(pull(), [None], "error")
        p = pull()
        p["head"]["sha"] = "fake"
        self.check(p, expected="error")


class FakeAPI:
    def __init__(self, pr, reviews=()):
        self.pr = copy.deepcopy(pr)
        self.votes = list(reviews)
        self.writes = []
        self.previous = None
        self.reads = 0

    def pull(self, number):
        self.reads += 1
        return copy.deepcopy(self.pr)

    def reviews(self, number):
        return copy.deepcopy(self.votes)

    def status(self, sha):
        return self.previous

    def publish(self, sha, number, state, reason):
        self.writes.append((sha, number, state, reason))


class GatePublicationTests(unittest.TestCase):
    def test_external_missing_vote_publishes_pending_at_head(self):
        api = FakeAPI(pull())
        gate.process(api, 123)
        self.assertEqual(api.writes[0][:3], (SHA, 123, "pending"))

    def test_internal_maintainer_publishes_success_only_on_its_head(self):
        api = FakeAPI(pull(70309026))
        gate.process(api, 123)
        self.assertEqual(api.writes[0][:3], (SHA, 123, "success"))

    def test_unchanged_bot_status_does_not_consume_a_second_status_record(self):
        api = FakeAPI(pull(), [review()])
        api.previous = {"state": "success", "context": gate.CONTEXT,
                        "description": "Trusted maintainer approved current HEAD",
                        "creator": {"login": "github-actions[bot]"}}
        gate.process(api, 123)
        self.assertEqual(api.writes, [])

    def test_untusted_old_result_is_overwritten(self):
        api = FakeAPI(pull())
        api.previous = {"state": "success", "description": "Trusted maintainer approved current HEAD",
                        "creator": {"login": "another-bot"}}
        gate.process(api, 123)
        self.assertEqual(api.writes[0][2], "pending")

    def test_superseded_head_is_not_published(self):
        class MovingAPI(FakeAPI):
            def pull(self, number):
                value = super().pull(number)
                if self.reads >= 2:
                    value["head"]["sha"] = NEW_SHA
                return value
        api = MovingAPI(pull(), [review()])
        gate.process(api, 123)
        self.assertEqual(api.writes, [])

    def test_workflow_hints_are_not_authorizations(self):
        api = FakeAPI(pull())
        original = gate.os.environ.get("GITHUB_EVENT_NAME")
        try:
            gate.os.environ["GITHUB_EVENT_NAME"] = "workflow_run"
            event = {"workflow_run": {"name": "review-policy-signal",
                                      "event": "pull_request_review",
                                      "display_title": "Review policy PR #123"}}
            self.assertEqual(gate.event_numbers(event, api), [123])
            event["workflow_run"]["name"] = "fake"
            with self.assertRaises(ValueError):
                gate.event_numbers(event, api)
        finally:
            if original is None:
                gate.os.environ.pop("GITHUB_EVENT_NAME", None)
            else:
                gate.os.environ["GITHUB_EVENT_NAME"] = original


class WorkflowHardeningTests(unittest.TestCase):
    def test_privilege_is_confined_to_default_branch_workflow(self):
        signal = (ROOT / ".github/workflows/review-policy-signal.yml").read_text()
        trusted = (ROOT / ".github/workflows/trusted-review-gate.yml").read_text()
        self.assertIn("pull_request_review:", signal)
        self.assertIn("workflow_run:", trusted)
        self.assertNotIn("workflow_run:", signal)
        self.assertNotIn("pull_request_target:", trusted)
        self.assertNotIn("pull_request_target:", signal)
        self.assertNotIn("statuses: write", signal)
        self.assertIn("statuses: write", trusted)
        self.assertNotIn("contents: write", trusted)
        self.assertNotIn("pull-requests: write", trusted)
        self.assertIn("ref: main", trusted)
        self.assertIn("persist-credentials: false", trusted)
        self.assertIn("python3 scripts/trusted_review_gate.py", trusted)


if __name__ == "__main__":
    unittest.main()
