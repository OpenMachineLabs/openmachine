"""Shared disposable fixtures; no test cases or test discovery."""

from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from openmachine.service import checks, install, payload



class PayloadCaseFixture(object):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="om-payload-", dir="/tmp")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.cleanup)
        self.source = self.root / "app-payload"
        self.source.mkdir()
        for name in ("python/bin/python3.13", "git/bin/git", "bin/git"):
            path = self.source / name
            path.parent.mkdir(parents=True)
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        (self.source / "python/bin/python3").symlink_to("python3.13")
        (self.source / "wheels").mkdir()
        self.wheel = self.source / "wheels/openmachine-0.2.8-py3-none-any.whl"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("openmachine-0.2.8.dist-info/METADATA", "Name: openmachine\nVersion: 0.2.8\n")
        (self.source / "requirements.lock").write_text("example==1.0\n")
        (self.source / "THIRD_PARTY_NOTICES.md").write_text("Fixture notices.\n")
        self.manifest = {"format": 1, "target": dict(payload.TARGET),
            "core": {"version": "0.2.8", "commit": "a" * 40, "wheel": self.wheel.relative_to(self.source).as_posix()},
            "python": {"version": "3.13.16", "build": "20261003", "url": "https://example.test/python", "sha256": "b" * 64},
            "git": {"version": "2.53.0", "build": "fixture", "url": "https://example.test/git", "sha256": "c" * 64}}
        self.digest = self.seal()
        self.state = private_directory(self.root / "state")
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(payload.sys, "platform", "darwin"))
        stack.enter_context(patch.object(payload.platform, "machine", return_value="arm64"))
        stack.enter_context(patch.object(payload.platform, "mac_ver", return_value=("13.0", (), "")))

    def cleanup(self):
        # Only this disposable fixture: retained product directories are read-only.
        for directory, directories, _ in os.walk(self.root, followlinks=False):
            Path(directory).chmod(0o700)
            for name in directories:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o700)
        self.temporary.cleanup()

    def seal(self):
        self.manifest["files"] = payload.inventory(self.source)
        raw = (json.dumps(self.manifest, sort_keys=True) + "\n").encode()
        (self.source / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def fault(self, code, function):
        with self.assertRaises(Fault) as caught:
            function()
        self.assertEqual(caught.exception.code, code)
