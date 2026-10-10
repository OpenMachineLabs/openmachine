"""Tests for service behavior at the component layer."""

import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from openmachine.foundation.state import lease, save_json
from openmachine.service import checks, install, payload, processes
from test.support.payload_cases import PayloadCaseFixture


class PayloadTests(PayloadCaseFixture, unittest.TestCase):
    def test_payload_commit_identity_and_historical_metadata_reuse(self):
        def observed(python, *_args, **_options):
            return {"version": "0.2.8", "prefix": str(python.parent.parent), "protocol": 1, "state_contract": 13}
        with patch.object(processes, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(checks, "_candidate_state", side_effect=observed):
            first = install._prepare(self.state, self.wheel, self.source / "requirements.lock", self.config,
                                     offline=(self.source, self.digest, self.manifest))
            path = self.state / "service/versions" / first / "installation.json"
            original = json.loads(path.read_text())
            self.assertEqual(original["commit"], self.manifest["core"]["commit"])
            save_json(path, original | {"commit": "f" * 40})
            self.fault("installation_conflict", lambda: install._prepare(
                self.state, self.wheel, self.source / "requirements.lock", self.config,
                offline=(self.source, self.digest, self.manifest)))
            del original["commit"]
            save_json(path, original)
            self.assertEqual(install._prepare(self.state, self.wheel, self.source / "requirements.lock", self.config,
                                              offline=(self.source, self.digest, self.manifest)), first)
            self.assertEqual(json.loads(path.read_text()), original)
            self.manifest["core"]["commit"] = "e" * 40
            second = install._prepare(self.state, self.wheel, self.source / "requirements.lock", self.config,
                                      offline=(self.source, self.seal(), self.manifest))
            self.assertNotEqual(first, second)
            self.assertEqual(json.loads((self.state / "service/versions" / second / "installation.json").read_text())["commit"], "e" * 40)

    def test_offline_preparation_reuses_installer_and_never_selects_service(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            output = "3.13.16\n" if "-c" in argv else "git version 2.53.0\n" if "--version" in argv else ""
            return subprocess.CompletedProcess(argv, 0, output, "")
        def state(python, *_args, **kwargs):
            self.assertIn(str(self.state / "service/payloads"), kwargs["env"]["PATH"])
            return {"version": "0.2.8", "prefix": str(python.parent.parent), "protocol": 1, "state_contract": 13}
        with patch.object(processes, "run", side_effect=run), patch.object(checks, "_candidate_state", side_effect=state):
            result = install.prepare_payload(self.config, self.state, self.source, self.digest)
            again = install.prepare_payload(self.config, self.state, self.source, self.digest)
            with lease(self.state / "service/operation.lock"):
                inherited = install._prepare_payload(self.config, self.state, self.source, self.digest)
        self.assertEqual(result, again)
        self.assertEqual(result, inherited)
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(set(result), {"status", "identity", "version", "payload_sha256", "runtime",
                                       "launch_python", "path_environment", "selection", "service"})
        self.assertEqual(result["selection"], "unchanged")
        self.assertFalse((self.state / "service/service.json").exists())
        installers = [(argv, options) for argv, options in calls if "install" in argv]
        self.assertEqual(len(installers), 2)
        for argv, options in installers:
            self.assertIn("--no-index", argv)
            self.assertIn("--only-binary=:all:", argv)
            self.assertIn("--no-deps", argv)
            self.assertEqual(options["env"]["PIP_CONFIG_FILE"], os.devnull)
        venv = [argv for argv, _ in calls if "venv" in argv]
        self.assertEqual(len(venv), 1)
        self.assertTrue(venv[0][0].startswith(str(self.state / "service/payloads")))
        self.assertEqual(venv[0][-1], result["runtime"])
        record = json.loads((Path(result["runtime"]) / "installation.json").read_text())
        self.assertEqual(record["payload_sha256"], self.digest)
        self.assertEqual(record["commit"], self.manifest["core"]["commit"])
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(self.config.read_bytes() + b"# configuration-only update\n")
        with patch.object(processes, "run", side_effect=run), patch.object(checks, "_candidate_state", side_effect=state):
            changed = install.prepare_payload(candidate, self.state, self.source, self.digest)
            self.assertEqual(install.prepare_payload(candidate, self.state, self.source, self.digest), changed)
        self.assertEqual(changed["payload_sha256"], result["payload_sha256"])
        self.assertNotEqual(changed["identity"], result["identity"])
        self.assertFalse((self.state / "service/service.json").exists())

    def test_failed_installation_is_retained_without_an_online_fallback(self):
        with patch.object(processes, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.fault("installation_failed", lambda: install._prepare(
                self.state, self.wheel, self.source / "requirements.lock", self.config,
                offline=(self.source, self.digest, self.manifest)))
        versions = list((self.state / "service/versions").iterdir())
        self.assertEqual(len(versions), 1)
        self.assertFalse((versions[0] / "installation.json").exists())
        with patch.object(processes, "run") as run:
            self.fault("service_state_invalid", lambda: install._prepare(
                self.state, self.wheel, self.source / "requirements.lock", self.config,
                offline=(self.source, self.digest, self.manifest)))
            run.assert_not_called()
        self.assertTrue((versions[0] / "artifacts" / self.wheel.name).exists())

    def test_same_python_version_with_different_payload_has_different_identity(self):
        def state(python, *_args, **kwargs):
            return {"version": "0.2.8", "prefix": str(python.parent.parent), "protocol": 1, "state_contract": 13}
        with patch.object(processes, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(checks, "_candidate_state", side_effect=state):
            first = install._prepare(self.state, self.wheel, self.source / "requirements.lock", self.config,
                                     offline=(self.source, self.digest, self.manifest))
            self.manifest["python"]["build"] = "different-build"
            second = install._prepare(self.state, self.wheel, self.source / "requirements.lock", self.config,
                                      offline=(self.source, self.seal(), self.manifest))
        self.assertNotEqual(first, second)

    def test_installer_environment_does_not_inherit_injection_or_package_indexes(self):
        with patch.dict(os.environ, {"PIP_EXTRA_INDEX_URL": "https://example.test", "PYTHONPATH": "/untrusted", "DYLD_LIBRARY_PATH": "/untrusted"}):
            env = payload.environment(self.source)
        self.assertFalse({"PIP_EXTRA_INDEX_URL", "PYTHONPATH", "DYLD_LIBRARY_PATH"} & env.keys())
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertTrue(env["PATH"].startswith(str(self.source / "bin") + os.pathsep))


if __name__ == "__main__":
    unittest.main()
