"""Tests for cli behavior at the component layer."""

import json
import subprocess
import sys
import unittest
from test.support.ssh_cases import SSHCaseFixture


class SSHTests(SSHCaseFixture, unittest.TestCase):
    def test_doctor_does_not_probe_ssh_or_resolve_remote_paths_by_default(self):
        config = self.root / "config.toml"
        config.write_text(f'schema_version=1\n[machines.build]\ntransport="ssh"\nssh_alias="existing-host"\n'
                          f'ssh_worker="/remote/{self.digest}.pyz"\n[workspaces.remote]\nmachine="build"\nroot="/remote/not-on-controller"\n')
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(config), "doctor"],
                                capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["unavailable_workspaces"], [])
        self.assertEqual(value["ssh_targets"], {"build": "not_probed"})


if __name__ == "__main__":
    unittest.main()
