"""Tests for github behavior at the component layer."""

import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from integrations.github import connection, operations
from openmachine.foundation.sanitize import Fault
from test.support.github_cases import GitHubCaseFixture, REPO, REQUEST


class GitHubTests(GitHubCaseFixture, unittest.TestCase):
    def test_transport_diagnostics_are_bounded_and_do_not_expose_credentials(self):
        with patch.object(connection.subprocess, "run", side_effect=FileNotFoundError()):
            self.assert_fault("github_unavailable", REQUEST, "GET", "user")
        with patch.object(connection.subprocess, "run", side_effect=subprocess.TimeoutExpired(["gh"], 30)):
            self.assert_fault("github_timeout", REQUEST, "POST", "endpoint", {"body": "x"})
        for status, code in ((401, "github_authentication_required"), (403, "github_access_denied"), (409, "github_conflict")):
            def reject(argv, **kwargs):
                self.assertIn("--hostname", argv)
                self.assertNotIn("GH_DEBUG", kwargs["env"])
                kwargs["stderr"].write(f"gh: synthetic-private-detail (HTTP {status})".encode())
                return SimpleNamespace(returncode=1)
            with self.subTest(status=status), patch.object(connection.subprocess, "run", side_effect=reject):
                with self.assertRaises(Fault) as caught:
                    REQUEST("GET", "user")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("synthetic-private-detail", str(caught.exception))

    def test_capture_io_failure_preserves_uncertain_write_outcome(self):
        with patch.object(connection.subprocess, "run", side_effect=OSError("synthetic I/O failure")):
            self.assert_fault("github_transport_failed", REQUEST, "POST", "endpoint", {"body": "x"})
        with patch.object(connection, "request", side_effect=Fault("github_transport_failed", "I/O failure")) as api:
            result = operations._mutate(REPO, "POST", "endpoint", {"body": "x"}, lambda _: "readback")
        self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
