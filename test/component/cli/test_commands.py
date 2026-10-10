"""Tests for cli behavior at the component layer."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from openmachine import __version__, cli, service
from openmachine.config import load
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_update_commit_and_wheel_arguments_are_exclusive(self):
        commit = "c" * 40
        valid = [["--commit", commit], ["--wheel", "/wheel.whl", "--requirements", "/requirements.lock"]]
        for arguments in valid:
            with self.subTest(arguments=arguments), patch.object(sys, "argv", ["openmachine", "update", *arguments]), \
                    patch.object(service, "operate", return_value={"status": "ready"}) as operate, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(), 0)
                self.assertEqual(operate.call_args.kwargs["commit"], commit if "--commit" in arguments else None)
                self.assertEqual(operate.call_args.kwargs["requirements"], None if "--commit" in arguments else Path("/requirements.lock"))
        for arguments in ([], ["--wheel", "/wheel.whl"], ["--commit", commit, "--wheel", "/wheel.whl"],
                          ["--commit", commit, "--requirements", "/requirements.lock"]):
            with self.subTest(arguments=arguments), patch.object(sys, "argv", ["openmachine", "update", *arguments]), \
                    patch.object(service, "operate") as operate, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                cli.main()
            self.assertEqual(stopped.exception.code, 2)
            operate.assert_not_called()

    def test_version_reports_commit_or_explicit_unknown(self):
        for commit in ("c" * 40, None):
            output = io.StringIO()
            with patch.object(service, "running_commit", return_value=commit), \
                    patch.object(sys, "argv", ["openmachine", "--version"]), redirect_stdout(output), \
                    self.assertRaises(SystemExit) as stopped:
                cli.main()
            self.assertEqual(stopped.exception.code, 0)
            self.assertEqual(output.getvalue().strip(), f"{__version__} ({commit or 'unknown'})")

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
        self.assertIsNone(json.loads(result.stdout)["commit"])


if __name__ == "__main__":
    unittest.main()
