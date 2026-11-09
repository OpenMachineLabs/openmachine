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
        arguments = dict(readable=[readable], writable=[writable, worktree], private=self.root / "T/openmachine-scope/exec",
                         control=[self.root / "state"], denied=[self.root / "home/Library"],
                         exceptions=[self.root / "home/Library/Developer"])
        with patch.object(seatbelt, "user_temp", return_value=self.root / "T"):
            text = seatbelt.profile(**arguments)
            # Protection is by name: Git entries that exist when the profile is built change nothing.
            (worktree / ".git").write_text("gitdir: elsewhere\n")
            (writable / ".git/worktrees/wt").mkdir(parents=True)
            (writable / ".git/worktrees/wt/commondir").write_text("../..\n")
            self.assertEqual(seatbelt.profile(**arguments), text)
            checkout = seatbelt.profile(**arguments, checkout=(self.root / "Dev (1)+x/new", writable / ".git"))
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
        # Git rules hold in every writable place, private trees included, after every write allow.
        git = lines.index(f'(deny file-write* (regex #"{seatbelt.PROTECTED_REGEX}") (regex #"{seatbelt.PROTECTED_IDE_REGEX}"))')
        self.assertGreater(git, lines.index(f'(allow file-read* file-write* (prefix "{self.root / "T/xcrun_db"}"))'))
        self.assertEqual(lines[git + 1:], [
            f'(deny file-write-create file-write-unlink (require-all (regex #"{seatbelt.NESTED_GIT_REGEX}") '
            "(vnode-type DIRECTORY SYMLINK)))",
            f'(allow file-write-create file-write-unlink (require-all (regex #"{seatbelt.GIT_DATA_REGEX}") (vnode-type DIRECTORY)))',
            r'(allow file-write-unlink (require-all (regex #"^(/.*)?/\.git$") (vnode-type REGULAR-FILE SYMLINK)))',
            "(deny file-clone (vnode-type DIRECTORY))",
            f'(deny file-read* file-write* process-exec (subpath "{self.root / "state"}"))'])
        self.assertNotIn(".git", "".join(line for line in lines if "literal" in line))
        # Only Core's own worktree creation may create its new link and registration, after the Git rules.
        added = checkout.splitlines()
        self.assertEqual((added[:-4], added[-1]), (lines[:-1], lines[-1]))
        self.assertEqual(added[-4], f'(allow file-write-create (literal "{self.root / "Dev (1)+x/new/.git"}"))')
        self.assertIn("(vnode-type DIRECTORY)", added[-3])
        directories, commondir = (re.search(r'#"([^"]+)"', line).group(1) for line in added[-3:-1])
        registrations = writable / ".git/worktrees"
        for path in (registrations, registrations / "new"):
            self.assertRegex(str(path), directories)
        self.assertRegex(str(registrations / "new/commondir"), commondir)
        for path in (registrations / "new/logs", worktree / ".git/worktrees", writable / ".git/modules/x"):
            self.assertNotRegex(str(path), directories)
        for path in (registrations / "new/gitdir", writable / ".git/commondir", worktree / ".git/worktrees/x/commondir"):
            self.assertNotRegex(str(path), commondir)
        self.assertNotIn("SecurityServer", text)
        self.assertNotIn("appleevents", text.lower())
        self.assertIn('(allow file-read* file-write* (literal "/dev/ttys004"))', seatbelt.own_terminal(text, "/dev/ttys004"))

    def test_protected_names_cover_git_entries_worktrees_and_submodules_but_not_refs(self):
        pattern = re.compile(seatbelt.PROTECTED_REGEX)
        for path in (".git", "sub/.git", ".git/hooks", ".git/hooks/pre-commit", ".git/config", ".git/config.worktree",
                     ".git/commondir", ".git/worktrees/wt/config.worktree", ".git/worktrees/wt/commondir",
                     ".git/worktrees/wt/config", ".git/worktrees/wt/hooks/pre-commit", ".git/worktrees/wt/modules/lib/config",
                     ".git/modules/lib/config", ".git/modules/a/b/hooks/post-checkout", ".git/modules/lib/config.worktree",
                     ".git/modules/lib/worktrees/wt/commondir", "sub/.git/config"):
            self.assertRegex("/r/" + path, pattern)
        for path in (".git/config.lock", ".git/refs/heads/config", ".git/refs/heads/hooks/x", ".git/worktrees/wt/gitdir",
                     ".git/info/exclude", ".git/objects/ab/cd", "config", "hooks/x", ".gitconfig", ".gitmodules",
                     ".github/hooks/x", "bare.git/config"):
            self.assertNotRegex("/r/" + path, pattern)

    def test_nested_git_dirs_stay_in_place_while_git_manages_their_data(self):
        nested, data = re.compile(seatbelt.NESTED_GIT_REGEX), re.compile(seatbelt.GIT_DATA_REGEX)
        for path in (".git/modules", ".git/modules/lib", ".git/modules/a/b", ".git/worktrees", ".git/worktrees/wt",
                     ".git/worktrees/wt/modules/lib", ".git/modules/lib/worktrees/wt"):
            self.assertRegex("/r/" + path, nested)
            self.assertNotRegex("/r/" + path, data)
        for path in (".git/modules/lib/objects/ab", ".git/modules/a/b/refs/heads/x", ".git/worktrees/wt/rebase-merge",
                     ".git/worktrees/wt/logs/refs", ".git/worktrees/wt/modules/lib/objects/pack"):
            self.assertRegex("/r/" + path, data)
        for path in (".git", ".git/objects/ab", ".git/refs/heads", "src/modules/x", ".git-modules/x"):
            self.assertNotRegex("/r/" + path, nested)

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
