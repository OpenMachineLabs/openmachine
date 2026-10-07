"""Tests for github behavior at the unit layer."""

import unittest
from unittest.mock import patch
from integrations.github import auth
from openmachine.foundation.sanitize import Fault



class GitHubDeviceWireTests(unittest.TestCase):
    def test_device_intervals_and_definitive_denials(self):
        with patch.object(auth, "CLIENT_ID", "Iv1.fixture"), patch.object(
                auth, "_post", return_value={"error": "slow_down", "interval": 75}):
            self.assertEqual(auth.poll_device("private-device", 65), (None, 75))
        for provider, expected in (("access_denied", "github_access_denied"), ("expired_token", "integration_login_expired")):
            with patch.object(auth, "CLIENT_ID", "Iv1.fixture"), patch.object(
                    auth, "_post", return_value={"error": provider}), self.assertRaises(Fault) as caught:
                auth.poll_device("private-device", 5)
            self.assertEqual(caught.exception.code, expected)

    def test_device_refresh_requires_no_client_secret_and_keeps_both_deadlines(self):
        result = dict(token_type="bearer", scope="", access_token="access-fixture-000000", refresh_token="refresh-fixture-000000",
                      expires_in=28800, refresh_token_expires_in=15897600)
        with patch.object(auth, "CLIENT_ID", "Iv1.fixture"), patch.object(auth, "_post", return_value=result) as post:
            pair = auth.refresh("original-refresh")
        self.assertEqual(post.call_args.args[1], dict(client_id="Iv1.fixture", grant_type="refresh_token", refresh_token="original-refresh"))
        self.assertGreater(pair.refresh_expires_at_ms, pair.access_expires_at_ms)
        self.assertEqual(pair.phase, "unverified")


if __name__ == "__main__":
    unittest.main()
