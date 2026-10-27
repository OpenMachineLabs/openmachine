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

    def test_release_check_preference_preserves_old_default_fingerprint(self):
        import hashlib
        import json
        from openmachine.config import Settings
        from pydantic import ValidationError
        original = {"schema_version": 1, "machines": {"local": {"transport": "local"}}}
        config = Settings.model_validate(original)
        self.assertIs(config.check_for_updates, True)
        legacy = config.model_dump(exclude={"check_for_updates"})
        del legacy["machines"]["local"]["workspace_write_enabled"]
        del legacy["machines"]["local"]["ssh_connection"]
        expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(config.configuration_sha256(), expected)
        explicit = Settings.model_validate(original | {"check_for_updates": True})
        self.assertEqual(explicit.configuration_sha256(), expected)
        disabled = Settings.model_validate(original | {"check_for_updates": False})
        self.assertIs(disabled.check_for_updates, False)
        self.assertNotEqual(disabled.configuration_sha256(), expected)
        for value in (0, 1, "false", None, {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Settings.model_validate(original | {"check_for_updates": value})
