"""Tests for cli behavior at the component layer."""

import json
import subprocess
import sys
import unittest
from openmachine.config import load
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_init_is_disabled_private_and_never_overwrites(self):
        path = self.root / "config.toml"
        command = [sys.executable, "-I", "-m", "openmachine.cli", "--config", str(path),
                   "init", "--workspace", str(self.work)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse(load(path).machines["local"].execution_enabled)
        self.assertIs(load(path).machines["local"].workspace_write_enabled, False)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        content = path.read_bytes()
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(path.read_bytes(), content)

    def test_doctor_does_not_create_state_or_claim_live_health(self):
        path = self.root / "config.toml"
        path.write_text('schema_version = 1\n[machines.local]\ntransport = "local"\n')
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(path),
                                 "--state-dir", str(self.state), "doctor"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual(json.loads(result.stdout)["chatgpt"], "not_probed")


if __name__ == "__main__":
    unittest.main()
