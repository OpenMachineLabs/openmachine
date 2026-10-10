"""Tests for service behavior at the component layer."""

import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import zipfile
from openmachine.foundation.state import save_json
from openmachine.service import processes as service_processes
from openmachine.service import install as service_install
from openmachine.service import state as service_state
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_commit_build_uses_verified_source_and_isolated_environment(self):
        commit, sources = "c" * 40, []
        injected = {"GIT_DIR": "/unrelated", "GIT_WORK_TREE": "/unrelated", "GIT_CONFIG_COUNT": "1",
                    "PYTHONPATH": "/unrelated", "PIP_EXTRA_INDEX_URL": "https://example.test"}
        def run(argv, **options):
            self.assertFalse(injected.keys() & options["env"].keys())
            self.assertEqual(options["env"]["PIP_CONFIG_FILE"], os.devnull)
            if "fetch" in argv:
                self.assertNotIn("GIT_CONFIG_GLOBAL", options["env"])
                self.assertNotIn("GIT_CONFIG_NOSYSTEM", options["env"])
                self.assertEqual(options["env"]["HOME"], str(Path.home()))
                self.assertEqual(argv[-2:], ["https://github.com/OpenMachineLabs/openmachine.git", commit])
            else:
                self.assertEqual(options["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
                self.assertNotEqual(options["env"]["HOME"], str(Path.home()))
            if "checkout" in argv:
                (Path(argv[argv.index("-C") + 1]) / "requirements.lock").write_text("fixture==1.0\n")
            if "wheel" in argv:
                self.assertIn("-I", argv)
                self.assertNotIn("--no-build-isolation", argv)
                (Path(argv[argv.index("--wheel-dir") + 1]) / "openmachine-0.1.0a7-py3-none-any.whl").touch()
            return subprocess.CompletedProcess(argv, 0, commit + "\n" if "rev-parse" in argv else "", "")
        def prepare(state, wheel, requirements, config, **options):
            self.assertTrue(wheel.is_file())
            self.assertEqual(requirements.read_text(), "fixture==1.0\n")
            self.assertEqual((state, config, options), (self.state, self.config, {"commit": commit}))
            sources.append(requirements.parent)
            return self.b
        with patch.dict(os.environ, injected), patch.object(service_processes, "run", side_effect=run), \
                patch.object(service_install, "_prepare", side_effect=prepare):
            self.assertEqual(service_install._prepare_commit(self.state, commit, self.config), self.b)
        self.assertFalse(sources[0].exists())
        self.assertEqual(service_state._record(self.state), self.record)

    def test_invalid_or_unverified_commit_never_reaches_installation(self):
        for commit in ("", "main", "v0.2.18", "a" * 7, "z" * 40):
            with self.subTest(commit=commit), patch.object(service_processes, "run") as run:
                self.assert_fault("invalid_commit", lambda: service_install._prepare_commit(self.state, commit, self.config))
                run.assert_not_called()
        commit = "c" * 40
        for failure, code in (("fetch", "commit_fetch_failed"), ("head", "commit_unverified"),
                              ("build", "commit_build_failed"), ("missing", "commit_build_failed"),
                              ("multiple", "commit_build_failed")):
            def run(argv, **options):
                if failure == "fetch" and "fetch" in argv or failure == "build" and "wheel" in argv:
                    return subprocess.CompletedProcess(argv, 1, "", "fixture")
                if failure == "multiple" and "wheel" in argv:
                    for name in ("first.whl", "second.whl"):
                        (Path(argv[argv.index("--wheel-dir") + 1]) / name).touch()
                return subprocess.CompletedProcess(argv, 0, "d" * 40 if failure == "head" else commit, "")
            with self.subTest(failure=failure), patch.object(service_processes, "run", side_effect=run), \
                    patch.object(service_install, "_prepare") as prepare:
                self.assert_fault(code, lambda: service_install._prepare_commit(self.state, commit, self.config))
                prepare.assert_not_called()
                self.assertEqual(service_state._record(self.state), self.record)

    def test_identical_wheels_at_distinct_commits_keep_distinct_installations(self):
        (self.state / "service/versions").chmod(0o700)
        wheel = self.root / "openmachine-0.1.0a7-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("openmachine-0.1.0a7.dist-info/METADATA", "Name: openmachine\nVersion: 0.1.0a7\n")
        requirements = self.root / "requirements.lock"
        requirements.write_text("fixture==1.0\n")
        def observed(python, *_args, **_options):
            return {"version": "0.1.0a7", "prefix": str(python.parent.parent), "protocol": 1, "state_contract": 1}
        with patch.object(service_processes, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(service_install.checks, "_candidate_state", side_effect=observed):
            plain = service_install._prepare(self.state, wheel, requirements, self.config)
            first = service_install._prepare(self.state, wheel, requirements, self.config, commit="c" * 40)
            second = service_install._prepare(self.state, wheel, requirements, self.config, commit="d" * 40)
            self.assertEqual(service_install._prepare(self.state, wheel, requirements, self.config, commit="c" * 40), first)
            self.assertEqual(service_install._prepare(self.state, wheel, requirements, self.config), plain)
            self.assertEqual(len({plain, first, second}), 3)
            self.assertNotIn("commit", service_state._installed(self.state, plain))
            self.assertEqual(service_state._installed(self.state, first)["commit"], "c" * 40)
            self.assertEqual(service_state._installed(self.state, second)["commit"], "d" * 40)
            path = service_state._runtime(self.state, first) / "installation.json"
            record = service_state._json(path)
            del record["commit"]
            save_json(path, record)
            self.assert_fault("installation_conflict", lambda: service_install._prepare(
                self.state, wheel, requirements, self.config, commit="c" * 40))
        self.assertEqual(service_state._record(self.state), self.record)

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
