"""Tests for config behavior at the unit layer."""

import unittest
from pydantic import ValidationError
from openmachine.config import GitHubConfig



class GitHubTests(unittest.TestCase):
    def test_attribution_setting_accepts_only_booleans(self):
        for value in (None, "true", "false", 0, 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                GitHubConfig.model_validate({"attribution": value})


if __name__ == "__main__":
    unittest.main()
