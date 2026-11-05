"""Tests for config behavior at the component layer."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch
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
            'schema_version = 1\n[machines.any]\ntransport = "local"\n[github]\nattribution = true\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\n[github]\nattribution = false\n',
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
            self.assertEqual(machine.local_access, "read_only")
            with self.assertRaises(Fault):
                load(path).machine("local", write=True)
            with self.assertRaises(Fault):
                load(path).workspace_machine("local")
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                initialize(path, self.work, commands=not commands, workspace_writes=not writes)
            self.assertEqual(path.read_bytes(), before)

    def test_unapproved_local_config_cannot_keep_old_authority(self):
        import hashlib
        import json
        from openmachine.config import Settings
        from pydantic import ValidationError
        original = {"schema_version": 1, "machines": {"local": {"transport": "local"}}}
        config = Settings.model_validate(original)
        self.assertIs(config.check_for_updates, True)
        legacy = config.model_dump(exclude={"check_for_updates"})
        del legacy["machines"]["local"]["workspace_write_enabled"]
        del legacy["machines"]["local"]["local_access"]
        expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertNotEqual(config.configuration_sha256(), expected)
        self.assertIsNone(config.machine("local").local_access)
        from openmachine.workspace.files import root_for
        with self.assertRaises(Fault) as denied:
            root_for(config, "local", str(self.work))
        self.assertEqual(denied.exception.code, "local_approval_required")
        with self.assertRaises(ValidationError):
            Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local", "local_access": "workspace"}}})
        explicit = Settings.model_validate(original | {"check_for_updates": True})
        self.assertEqual(explicit.configuration_sha256(), config.configuration_sha256())
        disabled = Settings.model_validate(original | {"check_for_updates": False})
        self.assertIs(disabled.check_for_updates, False)
        self.assertNotEqual(disabled.configuration_sha256(), expected)
        for mode in ("read_only", "full_access"):
            native = Settings.model_validate({"schema_version": 1,
                "machines": {"local": {"transport": "local", "local_access": mode}}})
            self.assertNotEqual(native.configuration_sha256(), expected)
        for value in (0, 1, "false", None, {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Settings.model_validate(original | {"check_for_updates": value})


class FirstAccessTests(RuntimeCaseFixture, unittest.TestCase):
    def test_explicit_full_access_keeps_command_and_write_grants_independent(self):
        from openmachine.config import initialize
        for commands, writes in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(commands=commands, writes=writes):
                path = self.root / f"full-{commands}-{writes}.toml"
                inputs = dict(local_access="full_access", commands=commands, workspace_writes=writes)
                created = initialize(path, self.work, **inputs)
                config = load(path)
                machine = config.machines["local"]
                self.assertEqual((machine.local_access, machine.execution_enabled, machine.workspace_write_enabled),
                                 ("full_access", commands, writes))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                for allowed, check in ((commands, lambda: config.machine("local", write=True)),
                                       (writes, lambda: config.workspace_machine("local"))):
                    if allowed:
                        self.assertEqual(check(), machine)
                    else:
                        with self.assertRaises(Fault):
                            check()
                before = path.read_bytes()
                verified = initialize(path, self.work, **inputs, verify_existing=True)
                self.assertEqual(verified["configuration_sha256"], created["configuration_sha256"])
                self.assertEqual(verified["verified"], str(path))
                with self.assertRaises(FileExistsError):
                    initialize(path, self.work, **inputs)
                self.assertEqual(path.read_bytes(), before)

    def test_verify_existing_compares_all_first_run_inputs_without_changes(self):
        from openmachine.config import initialize
        path = self.root / "full.toml"
        inputs = dict(local_access="full_access", commands=True, workspace_writes=True)
        full = initialize(path, self.work, **inputs)
        read_only = initialize(self.root / "read.toml", self.work, commands=True, workspace_writes=True)
        self.assertNotEqual(full["configuration_sha256"], read_only["configuration_sha256"])
        other = self.root / "other"
        other.mkdir()
        before, metadata = path.read_bytes(), path.stat()
        cases = [(self.work, inputs | {"local_access": "read_only"}),
                 (self.work, {"commands": True, "workspace_writes": True}),
                 (self.work, inputs | {"commands": False}),
                 (self.work, inputs | {"workspace_writes": False}), (other, inputs), (None, inputs)]
        for workspace, requested in cases:
            with self.subTest(workspace=workspace, requested=requested):
                with self.assertRaises(Fault) as caught:
                    initialize(path, workspace, **requested, verify_existing=True)
                self.assertEqual(caught.exception.code, "configuration_mismatch")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (metadata.st_ino, metadata.st_mtime_ns))
        missing = self.root / "missing.toml"
        with self.assertRaises(FileNotFoundError):
            initialize(missing, self.work, **inputs, verify_existing=True)
        self.assertFalse(missing.exists())

    def test_invalid_first_run_inputs_never_create_configuration(self):
        from openmachine.config import initialize
        path = self.root / "absent" / "config.toml"
        for mode in (None, True, 1, [], {}, "", "full", "FULL_ACCESS", 'full_access"\nexecution_enabled=true'):
            with self.subTest(mode=mode):
                with self.assertRaises(Fault) as caught:
                    initialize(path, self.work, local_access=mode, commands=True, workspace_writes=True)
                self.assertEqual(caught.exception.code, "invalid_config")
                self.assertFalse(path.parent.exists())
        for field in ("commands", "workspace_writes", "verify_existing"):
            with self.subTest(field=field):
                with self.assertRaises(Fault) as caught:
                    initialize(path, self.work, local_access="full_access", **{field: "true"})
                self.assertEqual(caught.exception.code, "invalid_config")
                self.assertFalse(path.parent.exists())
        not_directory = self.root / "file"
        not_directory.write_text("preserve")
        for workspace in (Path("relative"), self.root / "missing", not_directory):
            with self.subTest(workspace=workspace):
                with self.assertRaises(Fault) as caught:
                    initialize(path, workspace, local_access="full_access")
                self.assertEqual(caught.exception.code, "invalid_workspace")
                self.assertFalse(path.parent.exists())
        with patch("openmachine.config.os.open", side_effect=PermissionError("fixture denied")):
            with self.assertRaises(PermissionError):
                initialize(path, self.work, local_access="full_access")
        self.assertFalse(path.exists())

    def test_full_verification_rejects_unsafe_or_invalid_existing_files(self):
        from openmachine.config import initialize
        path = self.root / "full.toml"
        inputs = dict(local_access="full_access", commands=True, workspace_writes=True)
        initialize(path, self.work, **inputs)
        original = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaises(Fault) as caught:
            initialize(path, self.work, **inputs, verify_existing=True)
        self.assertEqual(caught.exception.code, "configuration_mismatch")
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        path.chmod(0o600)
        link = self.root / "linked.toml"
        link.symlink_to(path)
        with self.assertRaises(Fault) as caught:
            initialize(link, self.work, **inputs, verify_existing=True)
        self.assertEqual(caught.exception.code, "configuration_mismatch")
        link.unlink()
        os.link(path, link)
        with self.assertRaises(Fault) as caught:
            initialize(path, self.work, **inputs, verify_existing=True)
        self.assertEqual(caught.exception.code, "configuration_mismatch")
        link.unlink()
        with patch("openmachine.config.os.getuid", return_value=path.stat().st_uid + 1):
            with self.assertRaises(Fault) as caught:
                initialize(path, self.work, **inputs, verify_existing=True)
        self.assertEqual(caught.exception.code, "configuration_mismatch")
        self.assertEqual(path.read_bytes(), original)
        path.write_text("[invalid TOML")
        with self.assertRaises(Fault) as caught:
            initialize(path, self.work, **inputs, verify_existing=True)
        self.assertEqual(caught.exception.code, "invalid_config")
        self.assertEqual(path.read_text(), "[invalid TOML")
