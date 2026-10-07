"""Tests for execution behavior at the component layer."""

import unittest
from openmachine.foundation.sanitize import Fault
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_disabled_machine_cannot_execute(self):
        self.config.machines["my-laptop"].execution_enabled = False
        with self.assertRaises(Fault) as caught:
            self.start('print("no")')
        self.assertEqual(caught.exception.code, "execution_disabled")
        self.assertFalse(self.state.exists())

    def test_expected_head_mismatch_prevents_launch(self):
        with self.assertRaises(Fault) as caught:
            self.start('from pathlib import Path; Path("should-not-exist").touch()', expected_head_sha="0" * 40)
        self.assertEqual(caught.exception.code, "context_mismatch")
        self.assertFalse((self.work / "should-not-exist").exists())

    def test_unsafe_state_directory_is_rejected_without_chmod(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaises(Fault) as caught:
            self.start('print("no")')
        self.assertEqual(caught.exception.code, "unsafe_state")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
