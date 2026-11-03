"""Native HTTP failures preserve bounded diagnostics and uncertain writes."""

import unittest
from unittest.mock import patch
import httpx2 as httpx
from integrations.github import auth, operations
from openmachine.foundation.sanitize import Fault
from test.support.github_cases import REPO
from test.support.goal_recovery import GoalRecoveryFixture


class GoalRecoveryTransportTests(GoalRecoveryFixture, unittest.TestCase):
    def test_observer_identity_change_cannot_certify_a_current_goal(self):
        arguments, original = self.lose_ack()
        reads = 0
        def changed(method, endpoint, payload=None):
            nonlocal reads
            value = self.response(method, endpoint, payload)
            if method == "GET" and endpoint == "user":
                reads += 1
                if reads > 1:
                    value["id"] = 2
            return value
        self.transport.side_effect = changed
        self.fault("github_identity_changed", self.recover, arguments)
        self.assertNotIn("goal_witnesses", self.view()["record"])
        self.assertEqual(self.thread_writes, 1)

    def test_retarget_to_same_commit_during_read_does_not_certify_old_destination(self):
        arguments, original = self.lose_ack()
        reads = 0
        def retargeted(method, endpoint, payload=None):
            nonlocal reads
            if method == "GET" and endpoint.endswith("/pulls/2"):
                reads += 1
                if reads == 2:
                    self.provider.pr["base"]["ref"] = "other"
            return self.response(method, endpoint, payload)
        self.transport.side_effect = retargeted
        self.fault("github_candidate_changed", self.recover, arguments)
        self.assertNotIn("goal_witnesses", self.view()["record"])
        self.assertEqual(self.thread_writes, 1)

    def test_incomplete_thread_membership_is_not_goal_proof(self):
        arguments, original = self.lose_ack()
        def incomplete(method, endpoint, payload=None):
            value = self.response(method, endpoint, payload)
            if method == "POST" and endpoint == "graphql":
                value["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"] = {}
            return value
        self.transport.side_effect = incomplete
        self.fault("github_evidence_unavailable", self.recover, arguments)
        self.assertNotIn("goal_witnesses", self.view()["record"])
        self.assertEqual(self.thread_writes, 1)


class GitHubTests(unittest.TestCase):
    def test_actions_accept_only_exact_empty_acknowledgements_on_owned_routes(self):
        for suffix, expected in (("runs/42/rerun", 201),
                                 ("runs/42/rerun-failed-jobs", 201), ("runs/42/cancel", 202)):
            endpoint = f"repos/{REPO}/actions/{suffix}"
            def acknowledgement(request):
                self.assertEqual(request.headers["X-GitHub-Api-Version"], "2022-11-28")
                return httpx.Response(expected)
            self.assertEqual(self.transport(acknowledgement)("POST", endpoint, {}), {"http_status": expected})
            for code, body in ((200, b"{}"), (200, b""), (expected, b"{}"), (expected, b"bad"), (expected, b" ")):
                with self.subTest(suffix=suffix, code=code, body=body), self.assertRaises(Fault) as caught:
                    self.transport(lambda _: httpx.Response(code, content=body))("POST", endpoint, {})
                self.assertEqual(caught.exception.code, "github_response_unknown")
        for method, endpoint, code in (("GET", f"repos/{REPO}/actions/runs/42", 204),
                ("POST", f"repos/{REPO}/issues", 201), ("POST", f"repos/{REPO}/actions/runs/42/force-cancel", 202)):
            with self.assertRaises(Fault):
                self.transport(lambda _: httpx.Response(code))(method, endpoint)
        self.assertEqual(self.transport(lambda _: httpx.Response(204))("DELETE", "repos/Owner/repo/issues/comments/9"), {"http_status": 204})

    def test_dispatch_requires_200_json_with_run_details_on_the_pinned_api(self):
        endpoint = f"repos/{REPO}/actions/workflows/12/dispatches"
        ack = {"workflow_run_id": 42, "run_url": f"https://api.github.com/repos/{REPO}/actions/runs/42",
               "html_url": f"https://github.com/{REPO}/actions/runs/42"}
        def response(request):
            self.assertEqual(request.headers["X-GitHub-Api-Version"], "2022-11-28")
            return httpx.Response(200, json=ack)
        self.assertEqual(self.transport(response)("POST", endpoint, {"ref": "main", "return_run_details": True}), ack)
        for code, body in ((204, b""), (201, b'{}'), (202, b'{}'), (200, b""), (200, b"bad"), (200, b"[]")):
            with self.subTest(code=code, body=body), self.assertRaises(Fault) as caught:
                self.transport(lambda _: httpx.Response(code, content=body))("POST", endpoint, {"return_run_details": True})
            self.assertEqual(caught.exception.code, "github_response_unknown")

    def transport(self, handler):
        def send(method, endpoint, payload=None):
            client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
            with patch.object(auth.httpx, "Client", return_value=client):
                return auth.request("private-fixture-token", method, endpoint, payload)
        return send

    def test_transport_diagnostics_are_bounded_and_do_not_expose_credentials(self):
        for status, code in ((401, "github_authentication_required"), (403, "github_access_denied"),
                             (409, "github_conflict"), (429, "github_rate_limited")):
            def reject(request):
                self.assertEqual(str(request.url), auth.API + "user")
                return httpx.Response(status, text="synthetic-private-detail")
            with self.subTest(status=status):
                send = self.transport(reject)
                with self.assertRaises(Fault) as caught:
                    send("GET", "user")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("synthetic-private-detail", str(caught.exception))
                self.assertNotIn("private-fixture-token", str(caught.exception))

    def test_rate_limits_expose_only_bounded_numeric_wait_evidence(self):
        cases = [
            (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1791400000"},
             "github_rate_limited", {"http_status": 403, "rate_limit_reset_epoch_seconds": 1791400000}),
            (403, {"Retry-After": " 0 "}, "github_rate_limited", {"http_status": 403, "retry_after_seconds": 0}),
            (403, {"x-ratelimit-remaining": "1", "x-ratelimit-reset": "1791400000"}, "github_access_denied", None),
            (403, {"x-ratelimit-reset": "1791400000"}, "github_access_denied", None),
            (429, {}, "github_rate_limited", {"http_status": 429}),
            (429, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "invalid"},
             "github_rate_limited", {"http_status": 429}),
            (429, {"retry-after": "9999999999", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1791400000"},
             "github_rate_limited", {"http_status": 429, "retry_after_seconds": 9999999999,
                                    "rate_limit_reset_epoch_seconds": 1791400000}),
            (429, {"retry-after": "60", "x-ratelimit-remaining": "1", "x-ratelimit-reset": "1791400000"},
             "github_rate_limited", {"http_status": 429, "retry_after_seconds": 60}),
            (500, {"retry-after": "60"}, "github_response_unknown", None),
        ]
        for status, headers, code, evidence in cases:
            with self.subTest(status=status, headers=headers), self.assertRaises(Fault) as caught:
                self.transport(lambda _: httpx.Response(status, headers=headers, text="synthetic-private-detail"))("GET", "user")
            self.assertEqual((caught.exception.code, caught.exception.evidence), (code, evidence))
        for value in ("", "-1", "+1", "1.5", "1e3", "NaN", "1, 2", "1" * 11):
            for status in (403, 429):
                with self.subTest(status=status, value=value), self.assertRaises(Fault) as caught:
                    self.transport(lambda _: httpx.Response(status, headers={
                        "retry-after": value, "x-ratelimit-remaining": value, "x-ratelimit-reset": value}))("GET", "user")
                self.assertEqual(caught.exception.code, "github_access_denied" if status == 403 else "github_rate_limited")
                self.assertEqual(caught.exception.evidence, None if status == 403 else {"http_status": 429})
        self.assertEqual(self.transport(lambda _: httpx.Response(200, json={"ok": True},
            headers={"x-ratelimit-remaining": "0", "retry-after": "60"}))("GET", "user"), {"ok": True})

    def test_lost_write_response_is_unknown_without_retry(self):
        for error in (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError):
            calls = []
            def lost(request):
                calls.append(request)
                raise error("synthetic-private-detail")
            with self.subTest(error=error.__name__):
                result = operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback",
                                            transport=self.transport(lost))
                self.assertEqual(result["status"], "write_outcome_unknown")
                self.assertEqual(result["error_code"], "github_response_unknown")
                self.assertEqual(len(calls), 1)
                self.assertNotIn("synthetic-private-detail", str(result))

    def test_http_mutation_rejections_are_explicitly_no_effect(self):
        for status in (401, 403, 404, 405, 409, 422, 429):
            calls = []

            def rejected(request):
                calls.append(request)
                return httpx.Response(status, json={"message": "synthetic-private-detail"})

            with self.subTest(status=status), self.assertRaises(Fault) as caught:
                operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback",
                                   transport=self.transport(rejected))
            self.assertEqual(caught.exception.effect, "known_no_effect")
            self.assertEqual([request.method for request in calls], ["POST"])

    def test_acknowledged_http_write_keeps_readback_failure_unverified(self):
        for failure in ("connect", "rate_limit", "lost_response"):
            calls = []

            def response(request):
                calls.append(request)
                if request.method == "POST":
                    return httpx.Response(201, json={"id": 9, "body": "x"})
                if failure == "connect":
                    raise httpx.ConnectError("synthetic-private-detail")
                if failure == "rate_limit":
                    return httpx.Response(429)
                raise httpx.ReadError("synthetic-private-detail")

            with self.subTest(failure=failure):
                result = operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback",
                                            transport=self.transport(response))
            self.assertEqual(result["status"], "write_unverified")
            self.assertEqual(result["error_code"], {"connect": "github_network_unavailable", "rate_limit": "github_rate_limited",
                                                     "lost_response": "github_response_unknown"}[failure])
            self.assertEqual(result["receipt"]["id"], 9)
            self.assertEqual([request.method for request in calls], ["POST", "GET"])

    def test_connect_failure_and_response_limits_are_explicit(self):
        for error in (httpx.ConnectError, httpx.ConnectTimeout):
            calls = []
            def offline(request):
                calls.append(request)
                raise error("synthetic-private-detail")
            with self.subTest(error=error.__name__), self.assertRaises(Fault) as caught:
                operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback",
                                   transport=self.transport(offline))
            self.assertEqual(caught.exception.code, "github_network_unavailable")
            self.assertEqual(caught.exception.effect, "known_no_effect")
            self.assertEqual(len(calls), 1)
        for body in (b"x" * (2 * 1024 * 1024 + 1), b"not JSON", b"null"):
            with self.subTest(size=len(body)), self.assertRaises(Fault) as caught:
                self.transport(lambda request: httpx.Response(200, content=body))("GET", "user")
            self.assertEqual(caught.exception.code, "github_response_unknown")


if __name__ == "__main__":
    unittest.main()
