"""Private web token schemas and unchanged legacy credential serialization."""

import json
import unittest
from unittest.mock import patch

from integrations import credentials
from integrations.github import auth
from openmachine.foundation.sanitize import Fault


class WebTokenTests(unittest.TestCase):
    def tokens(self):
        return dict(access_token="fixture-access-" + "a" * 32,
                    refresh_token="fixture-refresh-" + "r" * 32,
                    expires_in=28800, refresh_token_expires_in=15897600, token_type="bearer")

    def test_web_pair_has_source_and_exact_core_binding(self):
        with patch.object(auth.time, "time_ns", return_value=1_000_000_000):
            pair = auth.accept_web_tokens(self.tokens(), core_binding="a" * 64)
        self.assertEqual((pair.account_id, pair.phase, pair.source, pair.core_binding, pair.refresh_id),
                         (0, "unverified", "web", "a" * 64, None))
        self.assertEqual((pair.access_expires_at_ms, pair.refresh_expires_at_ms),
                         (1000 + 28800 * 1000, 1000 + 15897600 * 1000))
        self.assertEqual(credentials._decode(pair.model_dump_json().encode()), pair)
        for token in (pair.access_token, pair.refresh_token):
            self.assertNotIn(token, repr(pair))

    def test_private_token_fields_are_exact_and_strict(self):
        original = self.tokens()
        invalid = [original | {"scope": ""}, original | {"client_secret": "fixture"},
                   {key: value for key, value in original.items() if key != "refresh_token"},
                   original | {"access_token": 123}, original | {"refresh_token": "short"},
                   original | {"access_token": "fixture-access-\n" + "x" * 32},
                   original | {"access_token": "fixture-access-" + "\u00e9" * 32},
                   original | {"access_token": "a" * 2049},
                   original | {"expires_in": True}, original | {"expires_in": "28800"},
                   original | {"expires_in": 59}, original | {"expires_in": 86401},
                   original | {"refresh_token_expires_in": 86399},
                   original | {"refresh_token_expires_in": 366 * 86400 + 1},
                   original | {"token_type": "installation"}, None, []]
        for index, value in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(Fault) as caught:
                auth.accept_web_tokens(value, core_binding="a" * 64)
            self.assertEqual(caught.exception.code, "github_invalid_response")
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertNotIn(original["access_token"], str(caught.exception))
            self.assertNotIn(original["refresh_token"], str(caught.exception))
        for binding in ("", "a" * 63, "A" * 64, "/tmp/core.sock", None, True):
            with self.subTest(binding=binding), self.assertRaises(Fault):
                auth.accept_web_tokens(original, core_binding=binding)

    def test_device_code_roundtrip_keeps_exact_legacy_fields(self):
        original = dict(format=1, account_id=7, phase="ready", access_token="fixture-access-" + "a" * 32,
                        refresh_token="fixture-refresh-" + "r" * 32,
                        access_expires_at_ms=28800000, refresh_expires_at_ms=15897600000)
        pair = credentials._decode(json.dumps(original).encode())
        self.assertEqual((pair.source, pair.core_binding, pair.refresh_id), ("device_code", None, None))
        self.assertTrue(pair.model_dump() == original)
        self.assertTrue(json.loads(pair.model_dump_json()) == original)
        rotating = pair.model_copy(update={"phase": "refreshing"})
        self.assertTrue(rotating.model_dump() == original | {"phase": "refreshing"})
        device_pair = auth._credential(self.tokens() | {"scope": ""})
        self.assertEqual(device_pair.source, "device_code")
        self.assertNotIn("source", device_pair.model_dump())

    def test_web_credentials_require_binding_and_original_refresh_id(self):
        pair = auth.accept_web_tokens(self.tokens(), core_binding="a" * 64)
        original = pair.model_dump() | {"account_id": 7, "phase": "ready"}
        rotating = original | {"phase": "refreshing", "refresh_id": "b" * 32}
        self.assertEqual(credentials._decode(json.dumps(rotating).encode()).refresh_id, "b" * 32)
        for index, value in enumerate((original | {"core_binding": None},
                original | {"source": "device_code"}, original | {"source": "unknown"},
                original | {"phase": "refreshing"}, rotating | {"refresh_id": "B" * 32})):
            with self.subTest(case=index), self.assertRaises(Fault) as caught:
                credentials._decode(json.dumps(value).encode())
            self.assertEqual(caught.exception.code, "integration_credential_invalid")
            self.assertNotIn(pair.access_token, str(caught.exception))
            self.assertNotIn(pair.refresh_token, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
