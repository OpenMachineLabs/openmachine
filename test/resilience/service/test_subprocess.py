"""Tests for service behavior at the resilience layer."""

import sys
import unittest
from openmachine.service import checks as service_checks
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_service_subprocess_reply_and_deadline_are_bounded(self):
        result = service_checks._run([sys.executable, "-c", 'print("bounded")'])
        self.assertEqual(result.stdout, "bounded\n")
        self.assert_fault("service_output_limit", lambda: service_checks._run([sys.executable, "-c", 'print("x" * 70000)']))
        self.assert_fault("service_command_unverified", lambda: service_checks._run([sys.executable, "-c", 'import time; time.sleep(10)'], timeout=0.05))


if __name__ == "__main__":
    unittest.main()
