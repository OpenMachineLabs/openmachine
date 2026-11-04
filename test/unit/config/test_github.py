"""Tests for config behavior at the unit layer."""

import hashlib
import json
import unittest
from pydantic import ValidationError
from openmachine.config import GitHubConfig, Settings


class GitHubTests(unittest.TestCase):
    def test_attribution_has_no_configuration_override(self):
        self.assertNotIn("attribution", GitHubConfig.model_fields)
        for value in (True, False, None, "true", "false", 0, 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                GitHubConfig.model_validate({"attribution": value})

    def test_fixed_attribution_preserves_default_on_configuration_identity(self):
        settings = Settings.model_validate({
            "schema_version": 1,
            "machines": {"local": {"transport": "local", "local_access": "read_only"}},
            "github": {"repositories": ["Owner/repo"]},
        })
        legacy = settings.model_dump()
        legacy["github"]["attribution"] = True
        legacy.pop("check_for_updates")
        for machine in legacy["machines"].values():
            if machine["workspace_write_enabled"] is None:
                del machine["workspace_write_enabled"]
        expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(settings.configuration_sha256(), expected)


if __name__ == "__main__":
    unittest.main()
