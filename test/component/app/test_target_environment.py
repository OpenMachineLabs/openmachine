"""Target facts through the public context interface, with real disposable inputs."""

import hashlib
import json
import os
import socket
import sys
import unittest
from unittest.mock import patch

from test.support.tasks import TaskFixture


class TargetContextTests(TaskFixture, unittest.TestCase):
    def observe(self, **arguments):
        return self.app.inspect_context("desktop", "project", **arguments)

    def test_resolution_uses_selected_cwd_without_executing_project_code(self):
        directory = self.work / "bin"
        directory.mkdir()
        executable = directory / "om-fixture-tool"
        marker = self.work / "executed"
        executable.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"PATH": "bin:" + os.environ.get("PATH", os.defpath),
                                     "UNRELATED_SECRET": "not-part-of-observation"}):
            result = self.observe(sections=["environment"], executables=["om-fixture-tool", "./bin/om-fixture-tool"])
        observed = result["sections"]["environment"]
        self.assertEqual(observed["scope"], "controller")
        value = observed["data"]["value"]
        self.assertEqual(value["interpreter"]["path"], sys.executable)
        self.assertTrue(all(item["path"] == str(executable) and not item["executed"] for item in value["executables"]))
        self.assertNotIn("not-part-of-observation", json.dumps(result))
        self.assertFalse(marker.exists())
        self.assertFalse(self.state.exists())

    def test_project_declarations_hashes_exclusions_and_original_reader(self):
        manifest = {"name": "fixture", "engines": {"node": ">=20"}, "scripts": {"prepare": "touch executed"},
                    "dependencies": {"plain": "1.2.3", "remote": "https://user:private@example.invalid/pkg"}}
        raw = json.dumps(manifest).encode()
        (self.work / "package.json").write_bytes(raw)
        (self.work / "pyproject.toml").write_text('[project]\nname="fixture"\nrequires-python=">=3.12"\n')
        (self.work / "Cargo.toml").write_text("malformed [")
        (self.work / "uv.lock").symlink_to(self.work / "package.json")
        (self.work / "poetry.lock").write_bytes(b"x" * 32769)
        result = self.observe(sections=["project", "environment"])
        project = result["sections"]["project"]
        entries = {item["path"]: item for item in project["data"]["entries"]}
        package = entries["package.json"]
        self.assertEqual(package["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn("scripts", package["declarations"])
        self.assertNotIn("private", json.dumps(project))
        self.assertEqual(package["declarations"]["dependencies"]["plain"], "1.2.3")
        self.assertTrue(package["content_redacted"])
        self.assertEqual(self.app.read_workspace_file(**package["evidence_ref"]["arguments"])["sha256"], package["sha256"])
        self.assertTrue(all(entries[name]["status"] == "unavailable" for name in ("Cargo.toml", "uv.lock", "poetry.lock")))
        self.assertFalse(project["complete"])
        self.assertEqual(result["sections"]["environment"]["status"], "observed")
        self.assertFalse((self.work / "executed").exists())
        limited = self.observe(sections=["project"], limit=1)["sections"]["project"]
        self.assertEqual(len(limited["data"]["entries"]), 1)
        self.assertIn("package.json", limited["data"]["unexamined_markers"])
        self.assertFalse(limited["complete"])

    def test_manifest_shapes_and_non_json_toml_values_are_unavailable(self):
        (self.work / "package.json").write_text("[]")
        (self.work / "pyproject.toml").write_text('[project]\nversion=2020-01-01\n')
        entries = self.observe(sections=["project"])["sections"]["project"]["data"]["entries"]
        self.assertTrue(all(item["status"] == "unavailable" for item in entries))
        self.assertEqual(len(entries), 2)

    def test_actual_resources_and_passive_listener_preserve_scope(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            result = self.observe(sections=["resources", "services"], ports=[port])
            services = result["sections"]["services"]["data"]
            family = "ipv4" if sys.platform == "linux" else "tcp"
            self.assertEqual(services[family]["status"], "observed", services)
            self.assertGreaterEqual(services[family]["value"]["ports"][0]["listener_count"], 1)
            listener.setblocking(False)
            with self.assertRaises(BlockingIOError):
                listener.accept()
        resources = result["sections"]["resources"]["data"]
        self.assertGreater(resources["cpu"]["value"], 0)
        self.assertEqual(resources["disk"]["value"]["device"], self.work.stat().st_dev)
        self.assertGreater(resources["disk"]["value"]["total_bytes"], 0)
        for sample in resources.values():
            self.assertIn("source", sample)
            self.assertIn("scope", sample)
            self.assertIn("units", sample)
            self.assertLessEqual(sample["observed_at_ms"], sample["finished_at_ms"])
        self.assertFalse(self.state.exists())

    def test_invalid_selectors_and_omission_do_not_start_collectors(self):
        with patch.object(self.app, "read_workspace_file", side_effect=AssertionError("Unexpected target read")):
            for arguments in ({"ports": [80]}, {"sections": ["services"], "ports": [True]},
                              {"sections": ["services"], "ports": [0]}, {"sections": ["services"], "ports": [80, 80]},
                              {"sections": ["environment"], "executables": ["../secret"]},
                              {"sections": ["environment"], "executables": ["a", "b"], "limit": 1}):
                with self.subTest(arguments=arguments):
                    self.fault("invalid_context_request", self.observe, **arguments)
        with patch.object(self.app.machines, "inspect_environment", side_effect=AssertionError("Unexpected collector")):
            self.observe()
        with patch("openmachine.execution.resources._read", side_effect=AssertionError("Unexpected OS read")), \
             patch("openmachine.execution.resources._command", side_effect=AssertionError("Unexpected OS reader")):
            section = self.observe(sections=["services"])["sections"]["services"]
        self.assertEqual(section["status"], "not_selected")
        self.assertFalse(section["complete"])

    def test_target_collector_drift_rejects_combined_result(self):
        original = self.app.machines.inspect_environment
        def drift(*args, **kwargs):
            result = original(*args, **kwargs)
            result["context"]["workspace_identity"] = "changed"
            return result
        with patch.object(self.app.machines, "inspect_environment", side_effect=drift):
            self.fault("source_changed", self.observe, sections=["runtime", "environment"])


if __name__ == "__main__":
    unittest.main()
