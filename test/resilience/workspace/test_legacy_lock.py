"""Tests for workspace behavior at the resilience layer."""

import sys
import unittest
import uuid
from openmachine import execution, workspace
from test.support.continuity_cases import ContinuityCaseFixture


class ContinuityTests(ContinuityCaseFixture, unittest.TestCase):
    def test_file_write_reuses_execution_lock_and_disabled_permission(self):
        running = execution.start(self.config, self.state, "desktop", uuid.uuid4().hex, str(self.work),
                                  [sys.executable, "-c", "import time; time.sleep(0.7)"])
        args = (self.config, self.state, "desktop", "project", "new.py", "value = 1\n", "", "")
        try:
            self.fault("resource_busy", workspace.write_text_file, *args)
        finally:
            self.assertEqual(self.wait(running)["status"], "succeeded")
        self.config.machines["desktop"].execution_enabled = False
        self.fault("execution_disabled", workspace.write_text_file, *args)
        self.assertFalse((self.work / "new.py").exists())


if __name__ == "__main__":
    unittest.main()
