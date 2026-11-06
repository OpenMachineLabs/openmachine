"""macOS Seatbelt profile, toolchain, environment and private-home construction."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openmachine.execution import seatbelt
from openmachine.foundation.sanitize import Fault


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-profile-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_toolchains_never_open_shared_home_areas(self):
        home = self.root / "home"
        paths = {name: home / name for name in (".local/bin", ".nvm/versions/node/v1/bin", "bin")}
        for path in [*paths.values(), self.root / "opt/tool/bin", self.root / "control/bin"]:
            path.mkdir(parents=True)
        entries = [str(path) for path in paths.values()] + [str(self.root / "opt/tool/bin"), str(home), "relative/bin",
                                                             str(self.root / "missing/bin"), str(self.root / "control/bin")]
        found = seatbelt.toolchain_directories(os.pathsep.join(entries), home, [self.root / "control"])
        self.assertEqual(found, [paths[".local/bin"], paths[".nvm/versions/node/v1/bin"], home / ".nvm/versions/node/v1",
                                 paths["bin"], self.root / "opt/tool/bin", self.root / "opt/tool"])

    def test_profile_denies_by_default_and_ends_with_protected_and_control_denials(self):
        readable, writable, worktree = self.root / "Dev (1)+x", self.root / "Dev (1)+x/core", self.root / "wt"
        for path in (writable, worktree):
            path.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: elsewhere\n")
        with patch.object(seatbelt, "user_temp", return_value=self.root / "T"):
            text = seatbelt.profile(readable=[readable], writable=[writable, worktree], private=self.root / "private",
                                    control=[self.root / "state"], path_environment="", home=self.root / "home")
        lines = text.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(deny default)"])
        self.assertEqual(lines[-1], f'(deny file-read* file-write* (subpath "{self.root / "state"}"))')
        self.assertIn(r"Dev \(1\)\+x/core(/.*)?/\.git/(hooks(/.*)?|config|worktrees/[^/]+/config\.worktree)$", text)
        self.assertIn(f'(deny file-write* (literal "{worktree / ".git"}"))', text)
        self.assertNotIn(f'(literal "{writable / ".git"}")', text)
        self.assertNotIn("SecurityServer", text)
        self.assertNotIn("appleevents", text.lower())
        self.assertIn('(allow file-read* file-write* (literal "/dev/ttys004"))', seatbelt.own_terminal(text, "/dev/ttys004"))
        with self.assertRaises(Fault) as caught:
            seatbelt.profile(readable=[self.root / 'quote"d'], writable=[writable], private=self.root, control=[],
                             path_environment="", home=self.root)
        self.assertEqual(caught.exception.code, "scope_unsupported")

    def test_environment_and_private_home_hold_no_inherited_secrets(self):
        base = {"PATH": "/usr/bin", "LANG": "C", "LC_ALL": "C", "TERM": "xterm", "HOME": "/Users/real",
                "SSH_AUTH_SOCK": "/tmp/agent", "GIT_DIR": "/elsewhere", "GITHUB_TOKEN": "secret", "OPENMACHINE_STATE": "x"}
        private = self.root / "private"
        environment = seatbelt.environment(base, private)
        self.assertEqual({key for key in environment if key in base}, {"PATH", "LANG", "LC_ALL", "TERM", "HOME"})
        self.assertEqual((environment["HOME"], environment["TMPDIR"]), (str(private / "home"), str(private / "tmp") + "/"))
        seatbelt.prepare(private, {"name": "Fixture", "email": "fixture@example.invalid\n[core]"})
        self.assertEqual((private / "home/.gitconfig").read_text(), "[user]\n\tname = Fixture\n")
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)



if __name__ == "__main__":
    unittest.main()
