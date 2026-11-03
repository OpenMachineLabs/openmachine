"""Actions route, identity and acknowledgement boundaries using scoped fixtures."""

import copy
import unittest
from urllib.parse import parse_qs, urlsplit

from integrations import github
from openmachine.foundation.sanitize import Fault
from test.support.github_cases import GitHubCaseFixture, HEAD, BASE, REPO, ROOT, settings


class ActionsTests(GitHubCaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.workflow_value = {"id": 12, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}
        self.run = {"id": 42, "workflow_id": 12, "head_sha": HEAD, "head_branch": "main", "event": "workflow_dispatch",
                    "status": "completed", "conclusion": "failure", "run_attempt": 2, "repository": {"full_name": REPO}}
        self.job = {"id": 71, "run_id": 42, "run_attempt": 2, "head_sha": HEAD, "name": "test",
                    "status": "completed", "conclusion": "failure", "steps": [{"number": 1, "name": "check", "status": "completed", "conclusion": "failure"}]}
        self.deployment = {"id": 8, "sha": HEAD, "ref": "main", "environment": "staging", "task": "deploy", "payload": {"private": "omit"}}
        self.posts = []
        self.ack = None
        self.dispatch_ack = {"workflow_run_id": 42, "run_url": f"https://api.github.com/{ROOT}/actions/runs/42",
                             "html_url": f"https://github.com/{REPO}/actions/runs/42"}
        self.transport.side_effect = self.actions_response

    def actions_response(self, method, endpoint, payload=None):
        path = urlsplit(endpoint).path
        if method == "POST":
            self.posts.append((endpoint, payload))
            if isinstance(self.ack, Exception):
                raise self.ack
            return self.ack if self.ack is not None else self.dispatch_ack if path.endswith("dispatches") else {"http_status": 202 if path.endswith("cancel") else 201}
        values = {
            f"{ROOT}/actions/workflows": {"total_count": 1, "workflows": [self.workflow_value]},
            f"{ROOT}/actions/workflows/ci.yml": self.workflow_value,
            f"{ROOT}/actions/workflows/12": self.workflow_value,
            f"{ROOT}/actions/workflows/12/runs": {"total_count": 1, "workflow_runs": [self.run]},
            f"{ROOT}/actions/runs": {"total_count": 1, "workflow_runs": [self.run]},
            f"{ROOT}/actions/runs/42": self.run,
            f"{ROOT}/actions/runs/42/attempts/2": self.run,
            f"{ROOT}/actions/runs/42/attempts/2/jobs": {"total_count": 1, "jobs": [self.job]},
            f"{ROOT}/commits/main": {"sha": HEAD},
            f"{ROOT}/deployments": [self.deployment],
            f"{ROOT}/deployments/8": self.deployment,
            f"{ROOT}/deployments/8/statuses": [{"id": 9, "state": "success"}],
        }
        if path in values:
            return copy.deepcopy(values[path])
        return self.response(method, endpoint, payload)

    def dispatch(self, **kwargs):
        args = dict(action="dispatch", repository=REPO, workflow="ci.yml", ref="main",
                    expected_head_sha=HEAD, inputs={"runner_mode": "hosted", "flag": True, "count": 2})
        return github.workflow(settings(), **(args | kwargs), transport=self.transport)

    def control(self, action="rerun", **kwargs):
        args = dict(action=action, repository=REPO, number=42, expected_attempt=2, expected_head_sha=HEAD)
        return github.workflow(settings(), **(args | kwargs), transport=self.transport)

    def read(self, action, **kwargs):
        return github.read(settings(read_repositories=[REPO]), action, REPO, **kwargs, transport=self.transport)

    def test_routes_and_acknowledgements_do_not_claim_completion(self):
        result = self.dispatch()
        self.assertEqual(self.posts, [(f"{ROOT}/actions/workflows/12/dispatches", {
            "ref": "main", "inputs": {"runner_mode": "hosted", "flag": True, "count": 2}, "return_run_details": True})])
        self.assertEqual(result["status"], "request_accepted")
        self.assertFalse(result["workflow_completion_verified"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["receipt"]["run_id"], 42)
        self.assertEqual(result["receipt"]["run_url"], self.dispatch_ack["run_url"])
        self.assertEqual(result["receipt"]["html_url"], self.dispatch_ack["html_url"])
        self.assertIn("not atomic", result["detail"])
        for action, suffix, code in (("rerun", "rerun", 201), ("rerun_failed", "rerun-failed-jobs", 201), ("cancel", "cancel", 202)):
            self.run["status"] = "queued" if action == "cancel" else "completed"
            result = self.control(action)
            self.assertEqual((result["status"], result["http_status"]), ("request_accepted", code))
            self.assertEqual(self.posts[-1], (f"{ROOT}/actions/runs/42/{suffix}", {}))
            self.assertEqual(result["observed_run_before_request"]["run_attempt"], 2)
            self.assertFalse(result["workflow_completion_verified"])

    def test_all_reads_filter_route_and_project_without_mutation(self):
        self.assertEqual(self.read("workflows")["data"][0]["id"], 12)
        self.assertEqual(self.read("workflow", path="ci.yml")["data"]["id"], 12)
        self.assertEqual(self.read("workflow", number=12)["data"]["id"], 12)
        filters = dict(workflow="ci.yml", branch="main", head_sha=HEAD, event="workflow_dispatch", status="failure")
        self.assertEqual(self.read("workflow_runs", filters=filters)["data"][0]["id"], 42)
        query = parse_qs(urlsplit(self.transport.call_args.args[1]).query)
        self.assertEqual(query, {**{key: [value] for key, value in filters.items() if key != "workflow"}, "per_page": ["30"], "page": ["1"]})
        self.assertEqual(self.read("workflow_run", number=42)["data"]["run_attempt"], 2)
        jobs = self.read("workflow_jobs", number=42, filters={"attempt": 2})
        self.assertEqual(jobs["data"][0]["steps"][0]["conclusion"], "failure")
        self.assertEqual(jobs["run_attempt"], 2)
        deployments = self.read("deployments", ref="main", filters={"head_sha": HEAD, "environment": "staging", "task": "deploy"})
        self.assertNotIn("payload", deployments["data"][0])
        self.assertFalse(deployments["has_more"])
        self.assertEqual(self.read("deployment", number=8)["data"]["sha"], HEAD)
        self.assertEqual(self.read("deployment_statuses", number=8)["data"][0]["state"], "success")
        self.assertEqual(self.posts, [])

    def test_strict_validation_precedes_mutation(self):
        bad = [{"workflow": value} for value in ("../ci.yml", "0", "-1", "ci.yml/dispatches", ".github/workflows/ci.yml", "9" * 20)]
        bad += [{"ref": value} for value in ("", "../main", "a//b", "x.lock", "a?b", "main\n", HEAD)]
        bad += [{"inputs": value} for value in ([], {"bad": []}, {"bad": None}, {"bad": float("nan")}, {"bad": float("inf")},
                    {"bad": 2**60}, {"bad": "x" * 16385}, {f"input_{i}": i for i in range(26)}, {"bad/key": "x"})]
        bad += [{"number": 42}, {"expected_attempt": 2}, {"expected_head_sha": "short"}, {"number": True}]
        for args in bad:
            with self.subTest(args=args):
                self.assert_fault("invalid_github_request", self.dispatch, **args)
        for args in ({"workflow": "ci.yml"}, {"ref": "main"}, {"inputs": {}}, {"number": -1}, {"expected_attempt": 0}, {"number": True}):
            with self.subTest(args=args):
                self.assert_fault("invalid_github_request", self.control, **args)
        self.assertEqual(self.posts, [])

    def test_dispatch_accepts_25_named_inputs_within_the_existing_payload_bound(self):
        inputs = {f"input_{i}": i for i in range(25)}
        self.assertEqual(self.dispatch(inputs=inputs)["status"], "request_accepted")
        self.assertEqual(self.posts[0][1]["inputs"], inputs)

    def test_dispatch_acknowledgement_requires_exact_causal_identity_and_canonical_urls(self):
        invalid = [self.dispatch_ack | {"workflow_run_id": value} for value in (True, 0, -1, 42.0, 2**53, "42")]
        invalid += [self.dispatch_ack | {key: value} for key, value in (
            ("run_url", f"https://api.github.com/repos/Other/repo/actions/runs/42"),
            ("run_url", f"https://api.github.com/{ROOT}/actions/runs/43"),
            ("run_url", self.dispatch_ack["run_url"] + "?token=untrusted"),
            ("html_url", self.dispatch_ack["html_url"] + "/"),
            ("html_url", f"https://github.com@other.invalid/{REPO}/actions/runs/42"),
            ("html_url", None))]
        for ack in invalid:
            self.ack = ack
            before = len(self.posts)
            result = self.dispatch()
            self.assertEqual(result["status"], "write_outcome_unknown")
            self.assertNotIn("run_id", result["receipt"])
            self.assertEqual(len(self.posts), before + 1)

    def test_stale_or_wrong_identity_and_read_only_grants_prevent_post(self):
        self.assert_fault("github_candidate_changed", self.dispatch, expected_head_sha=BASE)
        self.assert_fault("github_candidate_changed", self.control, expected_head_sha=BASE)
        self.assert_fault("github_candidate_changed", self.control, expected_attempt=1)
        self.run["repository"]["full_name"] = "Other/repo"
        self.assert_fault("github_invalid_response", self.control)
        self.run["repository"]["full_name"] = REPO
        self.workflow_value["id"] = 99
        self.assert_fault("github_invalid_response", self.dispatch, workflow="12")
        self.assert_fault("github_not_allowed", github.workflow, settings(read_repositories=[REPO]), "dispatch", REPO,
                          "ci.yml", "main", expected_head_sha=HEAD, transport=self.transport)
        self.assert_fault("github_not_allowed", self.dispatch, repository="Other/repo")
        self.assertEqual(self.posts, [])

    def test_lost_or_wrong_acknowledgement_never_replays_or_correlates_newest_run(self):
        for ack in (OSError("lost"), Fault("github_response_unknown", "lost"), {}, [], {"http_status": 200}, {"http_status": 204}):
            self.ack = ack
            before = len(self.posts)
            result = self.dispatch()
            observed = github.reconcile(settings(), self.task_locator("workflow"), transport=self.transport)
            self.assertEqual((result["status"], observed["status"]), ("write_outcome_unknown", "write_outcome_unknown"))
            self.assertEqual(len(self.posts), before + 1)
        self.assertFalse(any("/runs" in call.args[1] for call in self.transport.call_args_list))
        self.ack = OSError("lost")
        self.control()
        self.run.update(status="completed", conclusion="success", run_attempt=3)
        observed = github.reconcile(settings(), self.task_locator("workflow", 42), transport=self.transport)
        self.assertEqual(observed["status"], "write_outcome_unknown")
        self.assertEqual(observed["observed_run"]["run_attempt"], 3)

    def test_malformed_reads_filters_and_pagination_fail_closed(self):
        for action, kwargs in (("workflows", {"number": 12}), ("workflow", {"number": 12, "path": "ci.yml"}),
                ("workflow_runs", {"filters": {"url": "https://example.invalid"}}), ("workflow_runs", {"filters": []}),
                ("workflow_runs", {"filters": {"environment": "staging"}}), ("workflow_jobs", {"number": 42}),
                ("workflow_jobs", {"number": 42, "filters": {"attempt": True}}), ("deployments", {"ref": "../x"}),
                ("workflow_run", {"number": True}), ("issues", {"filters": {}})):
            with self.subTest(action=action, kwargs=kwargs):
                self.assert_fault("invalid_github_request", self.read, action, **kwargs)
        self.job["run_attempt"] = 3
        self.assert_fault("github_invalid_response", self.read, "workflow_jobs", number=42, filters={"attempt": 2})
        self.run["id"] = 43
        self.assert_fault("github_invalid_response", self.read, "workflow_run", number=42)
        self.run["id"] = 42
        self.assert_fault("github_invalid_response", self.read, "workflow_runs", filters={"head_sha": BASE})
        for response, code in (({"total_count": "1", "workflow_runs": []}, "github_invalid_response"),
                ({"total_count": 2, "workflow_runs": [self.run]}, "github_evidence_unavailable"),
                ({"total_count": 1, "workflow_runs": [None]}, "github_invalid_response")):
            self.transport.side_effect = lambda method, endpoint, payload=None: copy.deepcopy(response) if "/actions/runs?" in endpoint else self.actions_response(method, endpoint, payload)
            self.assert_fault(code, self.read, "workflow_runs")
        self.transport.side_effect = lambda method, endpoint, payload=None: {"total_count": 2, "workflow_runs": [self.run]} if "/actions/runs?" in endpoint else self.actions_response(method, endpoint, payload)
        self.assertEqual(self.read("workflow_runs", limit=1)["next_page"], 2)
        self.assertFalse(self.read("workflow_runs", limit=1, page=2)["has_more"])
