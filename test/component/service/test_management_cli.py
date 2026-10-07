"""Tests for service behavior at the component layer."""

import json
import subprocess
import sys
import unittest
from openmachine.service import state as service_state
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_stable_entry_is_a_real_cli_consumer_and_cannot_follow_a_changed_binding(self):
        record = self.policy_fixture()
        import sysconfig
        import venv
        for identity in (self.a, self.b):
            directory = self.state / "service/versions" / identity
            venv.EnvBuilder(with_pip=False).create(directory)
            site = directory / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
            (site / "fixture-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
        entry = self.state / "service/manage.py"
        entry.write_bytes(service_state.management_entry(self.state, record))
        entry.chmod(0o600)
        command = [sys.executable, "-I", str(entry), "--contract", "99", "status"]
        # Unsupported contract is rejected before any native observation.
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertTrue(result.stdout, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "incompatible_management")
        self.save(record | {"current": self.b, "previous": self.a})
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertTrue(result.stdout, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason"], "incompatible_management")
        self.save(record | {"config": str(self.root / "foreign.toml")})
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "management_identity_unavailable")


if __name__ == "__main__":
    unittest.main()
