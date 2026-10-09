"""Tests for service behavior at the component layer."""

import unittest
from unittest.mock import patch
import zipfile
from openmachine.service import processes as service_processes
from openmachine.service import install as service_install
from openmachine.service import state as service_state
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_invalid_lock_is_rejected_before_package_commands(self):
        wheel = self.root / "openmachine-0.1.0a7-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("openmachine-0.1.0a7.dist-info/METADATA", "Name: openmachine\nVersion: 0.1.0a7\n")
        requirements = self.root / "requirements.lock"
        requirements.write_text("mcp>=1\n")
        with patch.object(service_processes, "run") as run:
            self.assert_fault("invalid_installation_artifact", lambda: service_install._prepare(self.state, wheel, requirements, self.config))
            run.assert_not_called()
        self.assertEqual(service_state._record(self.state), self.record)


if __name__ == "__main__":
    unittest.main()
