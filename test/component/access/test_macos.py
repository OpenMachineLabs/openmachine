"""macOS Seatbelt profile, toolchain, environment and private-home construction."""

import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from openmachine.access import macos as seatbelt
from openmachine.foundation.sanitize import Fault


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-profile-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_read_denials_cover_credentials_library_media_and_volumes(self):
        home = self.root / "home"
        (home / "Library").mkdir(parents=True)
        (home / "Pictures").symlink_to(self.root / "photos")
        denied, exceptions = seatbelt.read_denials(home)
        for path in (home / ".ssh", home / ".config/gh", home / ".zsh_history", home / "Library", home / "Movies",
                     self.root / "photos", Path("/Volumes")):
            self.assertIn(path, denied)
        self.assertNotIn(home / "Desktop", denied)
        self.assertEqual(exceptions, [home / "Library/Developer"])

    def test_profile_denies_by_default_and_ends_with_protected_and_control_denials(self):
        readable, writable, worktree = self.root / "Dev (1)+x", self.root / "Dev (1)+x/core", self.root / "wt"
        for path in (writable, worktree):
            path.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: elsewhere\n")
        (writable / ".git/worktrees/wt").mkdir(parents=True)
        (writable / ".git/worktrees/wt/commondir").write_text("../..\n")
        with patch.object(seatbelt, "user_temp", return_value=self.root / "T"):
            text = seatbelt.profile(readable=[readable], writable=[writable, worktree], private=self.root / "T/openmachine-scope/exec",
                                    control=[self.root / "state"], denied=[self.root / "home/Library"],
                                    exceptions=[self.root / "home/Library/Developer"])
            with self.assertRaises(Fault) as caught:
                seatbelt.profile(readable=[self.root / 'quote"d'], writable=[writable], private=self.root, control=[],
                                 denied=[], exceptions=[])
            self.assertEqual(caught.exception.code, "scope_unsupported")
        lines = text.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(deny default)"])
        # Broad reads come first; denied areas, then their exceptions and granted roots, narrow them in order.
        order = ["(allow file-read*)", f'(deny file-read* process-exec (subpath "{self.root / "home/Library"}") '
                 f'(subpath "{self.root / "T/openmachine-scope"}"))',
                 f'(allow file-read* process-exec (subpath "{self.root / "home/Library/Developer"}"))',
                 f'(allow file-read* file-read-data process-exec (subpath "{readable}"))']
        self.assertEqual([lines.index(line) for line in order], sorted(lines.index(line) for line in order))
        self.assertNotIn("network", " ".join(line for line in lines if line.startswith("(deny")))
        # Secret-name denials precede every root re-allow, which must name file-read-data to override them.
        secret = lines.index(f'(deny file-read-data (regex #"{seatbelt.SECRET_DIRECTORY_REGEX}"))')
        self.assertLess(secret, lines.index(f'(allow file-read* file-read-data process-exec (subpath "{readable}"))'))
        self.assertIn(f'(allow file-read* file-read-data file-write* process-exec (subpath "{writable}")', text)
        self.assertEqual(lines[-1], f'(deny file-read* file-write* process-exec (subpath "{self.root / "state"}"))')
        self.assertIn(r"Dev \(1\)\+x/core" + seatbelt.PROTECTED_REGEX, text)
        for path in (worktree / ".git", writable / ".git", writable / ".git/worktrees/wt/commondir"):
            self.assertIn(f'(deny file-write* (literal "{path}"))', text)
        self.assertNotIn(f'(literal "{readable / ".git"}")', text)
        self.assertNotIn("SecurityServer", text)
        self.assertNotIn("appleevents", text.lower())
        self.assertIn('(allow file-read* file-write* (literal "/dev/ttys004"))', seatbelt.own_terminal(text, "/dev/ttys004"))

    def test_only_the_named_executables_run_inside_a_denied_area(self):
        client, denied = self.root / "home/Library/om/om-code", self.root / "home/Library"
        with patch.object(seatbelt, "user_temp", return_value=self.root / "T"):
            plain = seatbelt.profile(readable=[], writable=[], private=self.root / "T/p", control=[self.root / "state"],
                                     denied=[denied], exceptions=[])
            text = seatbelt.profile(readable=[], writable=[], private=self.root / "T/p", control=[self.root / "state"],
                                    denied=[denied], exceptions=[], executables=[client])
        allow = f'(allow file-read* process-exec (literal "{client}"))'
        self.assertNotIn("literal \"" + str(client), plain)
        lines = text.splitlines()
        self.assertLess(lines.index(f'(deny file-read* process-exec (subpath "{denied}") (subpath "{self.root / "T"}"))'),
                        lines.index(allow))
        self.assertEqual(lines[-1], f'(deny file-read* file-write* process-exec (subpath "{self.root / "state"}"))')

    def test_protected_git_files_cover_worktrees_and_submodules_but_not_refs(self):
        pattern = re.compile("^/r" + seatbelt.PROTECTED_REGEX)
        for path in (".git/hooks", ".git/hooks/pre-commit", ".git/config", ".git/config.worktree", ".git/commondir",
                     ".git/worktrees/wt/config.worktree", ".git/modules/lib/config", ".git/modules/a/b/hooks/post-checkout",
                     ".git/modules/lib/config.worktree", ".git/modules/lib/worktrees/wt/config.worktree", "sub/.git/config"):
            self.assertRegex("/r/" + path, pattern)
        for path in (".git/config.lock", ".git/refs/heads/config", ".git/refs/heads/hooks/x", ".git/worktrees/wt/gitdir",
                     ".git/info/exclude", "config", "hooks/x", ".gitconfig"):
            self.assertNotRegex("/r/" + path, pattern)

    def test_secret_names_skip_public_certificates_and_sdk_directories(self):
        directories, files = re.compile(seatbelt.SECRET_DIRECTORY_REGEX), re.compile(seatbelt.SECRET_FILE_REGEX)
        for path in ("/p/.env", "/p/.env.local", "/p/.env.example", "/p/srv.key", "/p/cert.p12", "/p/credentials",
                     "/p/credentials.json", "/p/secrets.toml", "/p/id_ed25519", "/p/.netrc"):
            self.assertRegex(path, files)
        self.assertRegex("/p/.aws/config", directories)
        for path in ("/etc/ssl/cert.pem", "/p/certifi/cacert.pem", "/p/id_rsa.pub", "/p/.environment", "/p/app.ts"):
            self.assertNotRegex(path, files)
            self.assertNotRegex(path, directories)
        self.assertNotRegex("/p/aws-sdk/lib/credentials/index.js", files)

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
