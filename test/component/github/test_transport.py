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

    def test_lost_write_response_is_unknown_without_retry(self):
        calls = []
        def lost(request):
            calls.append(request)
            raise httpx.ReadError("synthetic-private-detail")
        result = operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback",
                                    transport=self.transport(lost))
        self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertEqual(result["error_code"], "github_response_unknown")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("synthetic-private-detail", str(result))

    def test_connect_failure_and_response_limits_are_explicit(self):
        def offline(request):
            raise httpx.ConnectTimeout("synthetic-private-detail")
        with self.assertRaises(Fault) as caught:
            self.transport(offline)("GET", "user")
        self.assertEqual(caught.exception.code, "github_network_unavailable")
        for body in (b"x" * (2 * 1024 * 1024 + 1), b"not JSON", b"null"):
            with self.subTest(size=len(body)), self.assertRaises(Fault) as caught:
                self.transport(lambda request: httpx.Response(200, content=body))("GET", "user")
            self.assertEqual(caught.exception.code, "github_response_unknown")


if __name__ == "__main__":
    unittest.main()
