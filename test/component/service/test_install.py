"""An installation is built offline from the retained payload and checked by running its access launcher."""

import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from openmachine.service import install, payload, processes
from openmachine.service import state as service_state
from test.support.payload_cases import PayloadCaseFixture


class InstallTests(PayloadCaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.retained, _ = payload.retain(self.state, self.source, self.digest)
        self.destination = self.state / "service/versions" / self.digest
        self.calls = []

    def run_fake(self, version="0.2.8", failing=None):
        def run(argv, **options):
            self.calls.append((argv, options))
            if "venv" in argv:
                (self.destination / "bin").mkdir(parents=True)
            stdout = json.dumps({"version": version}) if argv[-2:] == ["openmachine.access", "capabilities"] else ""
            return subprocess.CompletedProcess(argv, 1 if failing and failing in argv else 0, stdout, "")
        return patch.object(processes, "run", side_effect=run)

    def test_offline_build_then_capabilities_check_records_the_installation(self):
        with self.run_fake():
            self.assertEqual(install.prepare(self.state, self.retained, self.digest), {"identity": self.digest, "version": "0.2.8"})
        python, payload_python = str(self.destination / "bin/python"), str(self.retained / "python/bin/python3")
        pip = [python, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-deps",
               "--no-index", "--find-links", str(self.retained / "wheels"), "--only-binary=:all:"]
        self.assertEqual([argv for argv, _ in self.calls], [
            [payload_python, "-I", "-m", "venv", str(self.destination)], [*pip, "-r", str(self.retained / "requirements.lock")],
            [*pip, str(self.retained / self.manifest["core"]["wheel"])], [python, "-I", "-m", "pip", "check"],
            [python, "-I", "-m", "openmachine.access", "capabilities"]])
        self.assertTrue(all(options["env"] == payload.environment() for _, options in self.calls))
        self.assertEqual(service_state._installed(self.state, self.digest), {
            "identity": self.digest, "version": "0.2.8", "protocol": 1, "state_contract": service_state.STATE_CONTRACT,
            "management_contract": 1, "payload_sha256": self.digest, "commit": "a" * 40})
        with patch.object(processes, "run") as run:
            self.assertEqual(install.prepare(self.state, self.retained, self.digest)["identity"], self.digest)
            run.assert_not_called()

    def test_a_failed_build_or_a_wrong_version_records_no_installation(self):
        for failing, version, code in (("venv", "0.2.8", "installation_failed"), ("check", "0.2.8", "installation_failed"),
                                       (None, "0.2.9", "installation_identity_mismatch"),
                                       ("capabilities", "0.2.8", "installation_identity_mismatch")):
            with self.subTest(failing=failing, version=version), self.run_fake(version, failing):
                self.fault(code, lambda: install.prepare(self.state, self.retained, self.digest))
                self.assertFalse((self.destination / "installation.json").exists())
                # A failed candidate stays for diagnosis and is never adopted.
                self.fault("service_state_invalid", lambda: install.prepare(self.state, self.retained, self.digest))
            shutil.rmtree(self.destination)


if __name__ == "__main__":
    unittest.main()
