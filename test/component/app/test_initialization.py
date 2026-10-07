"""Tests for app behavior at the component layer."""

import os
import unittest
from openmachine.bootstrap import create_application
from test.support.continuity_cases import ContinuityCaseFixture


class ContinuityTests(ContinuityCaseFixture, unittest.TestCase):
    def test_application_initializes_private_state_before_admission_locks(self):
        app = create_application(self.config, self.state)
        previous_umask = os.umask(0o022)
        try:
            saved = app.save_task("private-root", 0, "Private state", "First direct application write.")
        finally:
            os.umask(previous_umask)
        self.assertEqual(saved["revision"], 1)
        for path in (self.state, self.state / "locks", self.state / "tasks"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "locks/lifecycle.lock").stat().st_mode & 0o777, 0o600)

        unsafe = self.root / "unsafe-state"
        unsafe.mkdir()
        unsafe.chmod(0o755)
        denied = create_application(self.config, unsafe)
        self.fault("unsafe_state", denied.save_task, "not-saved", 0, "Denied", "Keep the original root.")
        self.assertEqual(unsafe.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(unsafe.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
