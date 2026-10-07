"""Native HTTP failures preserve bounded diagnostics and uncertain writes."""

import unittest
from unittest.mock import patch
import httpx2 as httpx
from integrations.github import auth, operations
from openmachine.foundation.sanitize import Fault
from test.support.github_cases import REPO


class GitHubTests(unittest.TestCase):
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
            self.assertEqual(len(calls), 1)
        for body in (b"x" * (2 * 1024 * 1024 + 1), b"not JSON", b"null"):
            with self.subTest(size=len(body)), self.assertRaises(Fault) as caught:
                self.transport(lambda request: httpx.Response(200, content=body))("GET", "user")
            self.assertEqual(caught.exception.code, "github_response_unknown")


if __name__ == "__main__":
    unittest.main()
