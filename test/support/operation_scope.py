"""Disposable Selected Projects tree: Read-only parent, Full children, siblings and a fake account home."""

import os
from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch

from openmachine import execution
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.execution.records import TERMINAL
from openmachine.foundation.sanitize import Fault

AUTHORITY = {"policy_revision": 7, "grant_id": "grant-fixture", "grant_revision": 3, "session_generation": 12}


class ScopeFixture(object):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-scope-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.dev = self.root / "Development"
        self.core, self.cloud, self.desktop = (self.dev / name for name in ("core", "cloud", "desktop"))
        self.personal = self.root / "personal"
        # Default-denied areas live under a fake account home; the real home is never touched.
        self.home = self.root / "home"
        self.drive = self.home / "Library" / "CloudStorage" / "GoogleDrive"
        self.developer = self.home / "Library" / "Developer"
        for path in (self.core, self.cloud, self.desktop, self.personal, self.drive, self.developer, self.home / ".ssh"):
            path.mkdir(parents=True)
        (self.dev / "README").write_text("parent\n")
        (self.personal / "notes.txt").write_text("personal notes\n")
        (self.drive / "drive.txt").write_text("DRIVE-SECRET\n")
        (self.home / ".ssh" / "id_test").write_text("SSH-SECRET\n")
        (self.developer / "dev.txt").write_text("developer\n")
        # Secret-named files: unselected (personal), Read-only (desktop) and Full (core) projects.
        for folder in (self.personal, self.desktop, self.core):
            (folder / ".env").write_text(f"ENV-{folder.name.upper()}\n")
        home = patch.object(execution, "account_home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        for repo in (self.core, self.cloud, self.desktop):
            (repo / "file.txt").write_text(repo.name + "\n")
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "add", "file.txt")
            self.git(repo, "commit", "-q", "-m", "Fixture")
        self.state = self.root / "state"
        self.config = Settings.model_validate({"schema_version": 1, "machines": {"local": {
            "transport": "local", "local_access": "full_access", "execution_enabled": True,
            "workspace_write_enabled": True}}})
        self.app = create_application(self.config, self.state)

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def binding(self, path, mode, *, workspace=None, relative="."):
        workspace = str(path) if workspace is None else str(workspace)
        target = self.app.inspect_projects("local", workspace, "inspect", relative)["project"]["target_id"]
        return {"machine": "local", "workspace_name": workspace, "relative_path": relative, "target_id": target, "mode": mode}

    def scope(self, *bindings):
        bindings = bindings or (self.binding(self.dev, "read_only"), self.binding(self.core, "full_access"),
                                self.binding(self.cloud, "full_access"))
        return {"protocol": 1, "authority": dict(AUTHORITY), "roots": list(bindings)}

    def fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def run_scoped(self, argv, cwd=None, scope=None, **options):
        identity = os.urandom(16).hex()
        result = self.app.start_execution("local", identity, str(cwd or self.core), argv,
                                          operation_scope=scope or self.scope(), **options)
        deadline = time.monotonic() + 30
        while result["status"] not in TERMINAL and time.monotonic() < deadline:
            time.sleep(0.05)
            result = self.app.read_execution("local", identity)
        self.assertIn(result["status"], TERMINAL)
        return self.app.read_execution("local", identity)
