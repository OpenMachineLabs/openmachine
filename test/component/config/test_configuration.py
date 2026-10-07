"""Tests for config behavior at the component layer."""

import unittest
from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_configuration_rejects_unknown_or_invalid_fields(self):
        path = self.root / "config.toml"
        for text in (
            'schema_version = 1\n[machines.any]\ntransport = "ssh"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\nextra = true\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\n[workspaces.x]\nmachine = "missing"\nroot = "/tmp"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\n[workspaces.x]\nmachine = "any"\nroot = "relative"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\nexecution_enabled = "true"\n',
        ):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(Fault) as caught:
                    load(path)
                self.assertEqual(caught.exception.code, "invalid_config")

    def test_init_grants_are_explicit_and_independent(self):
        from openmachine.config import initialize
        for commands, writes in ((False, False), (True, False), (False, True), (True, True)):
            path = self.root / f"config-{commands}-{writes}.toml"
            initialize(path, self.work, commands=commands, workspace_writes=writes)
            machine = load(path).machines["local"]
            self.assertEqual((machine.execution_enabled, machine.workspace_write_enabled), (commands, writes))
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                initialize(path, self.work, commands=not commands, workspace_writes=not writes)
            self.assertEqual(path.read_bytes(), before)

    def test_first_run_recovery_uses_existing_validation_without_granting_execution(self):
        from openmachine.config import initialize
        profile = dict(workspace="project", connection='Existing "OM" \\ route',
                       browser_workspace="Personal", model="selected-model")
        path = self.root / "first-run.toml"
        initialize(path, self.work, chat_recovery=profile)
        config = load(path)
        self.assertEqual(config.chat_recovery.model_dump(exclude_none=True), profile)
        self.assertFalse(config.machines["local"].execution_enabled)
        self.assertIs(config.machines["local"].workspace_write_enabled, False)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            initialize(path, self.work, chat_recovery=profile | {"model": "another-model"})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.state.exists())
        verified = initialize(path, self.work, chat_recovery=profile, verify_existing=True)
        self.assertEqual(verified["configuration_sha256"], config.configuration_sha256())
        self.assertNotIn("created", verified)
        with self.assertRaises(Fault):
            initialize(path, self.work, chat_recovery=profile, commands=True, verify_existing=True)
        self.assertEqual(path.read_bytes(), before)
        missing = self.root / "missing.toml"
        with self.assertRaises(FileNotFoundError):
            initialize(missing, self.work, chat_recovery=profile, verify_existing=True)
        self.assertFalse(missing.exists())
        for patch in ({"model": None}, {"connection": ""}, {"browser_account": "two\nlines"}, {"model": "bad\x7flabel"},
                      {"workspace": "missing"}, {"extra": "forbidden"}):
            candidate = self.root / "invalid.toml"
            with self.assertRaises(Fault) as caught:
                initialize(candidate, self.work, chat_recovery=profile | patch)
            self.assertEqual(caught.exception.code, "invalid_config")
            self.assertFalse(candidate.exists())


if __name__ == "__main__":
    unittest.main()
