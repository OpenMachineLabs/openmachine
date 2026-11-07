"""The persistent restricted-mode home: owner-only, link-free and never rewritten by unconfined code."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from openmachine.access import sessions
from openmachine.foundation.sanitize import Fault

STICKY_ROOT = Path("/tmp").resolve()  # Root-owned and sticky on macOS and Linux.


class PersistentHomeTests(unittest.TestCase):
    def setUp(self):
        self.home = STICKY_ROOT / f"openmachine-code-{os.getuid()}"
        shutil.rmtree(self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.home, True)
        root = patch.object(sessions, "SHARED", STICKY_ROOT)
        root.start()
        self.addCleanup(root.stop)

    def unsafe(self):
        with self.assertRaises(Fault) as caught:
            sessions.persistent_home()
        self.assertEqual(caught.exception.code, "persistent_home_unsafe")

    def test_the_home_is_created_owner_only_with_only_the_git_identity(self):
        with patch.object(sessions, "_git_identity", return_value={"name": "Dev", "email": "dev@example.invalid"}):
            self.assertEqual(sessions.persistent_home(), self.home)
        for name in ("", "home", "cache", "config", "data", "state"):
            self.assertEqual((self.home / name).stat().st_mode & 0o777, 0o700, name)
        config = self.home / "home" / ".gitconfig"
        self.assertEqual(config.read_text(), "[user]\n\tname = Dev\n\temail = dev@example.invalid\n")
        config.write_text("changed by confined work\n")
        with patch.object(sessions, "_git_identity", return_value={"name": "Other"}):
            sessions.persistent_home()
        self.assertEqual(config.read_text(), "changed by confined work\n")

    def test_links_loose_modes_and_unprotected_parents_are_refused(self):
        sessions.persistent_home()
        (self.home / "cache").rmdir()
        (self.home / "cache").symlink_to(Path.home())
        self.unsafe()
        (self.home / "cache").unlink()
        sessions.persistent_home()
        self.home.chmod(0o755)
        self.unsafe()
        self.home.chmod(0o700)
        if sys.platform == "darwin":
            subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(self.home / "data")], check=True)
            self.unsafe()
            subprocess.run(["/bin/chmod", "-N", str(self.home / "data")], check=True)
        sessions.persistent_home()
        with tempfile.TemporaryDirectory() as owned, patch.object(sessions, "SHARED", Path(owned)):
            self.unsafe()
        with patch.object(sessions, "SHARED", STICKY_ROOT / "missing-om-root"):
            self.unsafe()
