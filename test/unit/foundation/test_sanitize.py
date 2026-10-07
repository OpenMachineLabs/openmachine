"""Tests for foundation behavior at the unit layer."""

import unittest
from openmachine.foundation.sanitize import redact



class RuntimeTests(unittest.TestCase):
    def test_redaction_is_applied(self):
        self.assertNotIn("synthetic-value", redact("password=synthetic-value"))


if __name__ == "__main__":
    unittest.main()
