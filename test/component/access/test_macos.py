"""macOS Seatbelt profile, toolchain, environment and private-home construction."""

import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from openmachine.access import restricted
from openmachine.access.macos import seatbelt
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
        home, writable, worktree = self.root / "H", self.root / "Dev (1)+x/core", self.root / "wt"
        for path in (home, writable, worktree):
            path.mkdir(parents=True)
        arguments = dict(home=home, folders=[writable, worktree], private=self.root / "T/omc/lifetime",
                         control=[self.root / "state"], denied=[self.root / "home/Library"],
                         exceptions=[self.root / "home/Library/Developer"], executable=self.root / "omc/om-code")
        with patch.object(seatbelt, "user_temp", return_value=self.root / "T"):
            text = seatbelt.profile(**arguments)
            # Protection is by name: a .git file changes nothing, and nested Git dirs that exist gain only
            # their own data directories.
            (worktree / ".git").write_text("gitdir: elsewhere\n")
            self.assertEqual(seatbelt.profile(**arguments), text)
            for path in (writable / ".git/worktrees/wt", writable / ".git/modules/vendor/objects/lib",
                         writable / '.git/modules/qu"ote'):
                path.mkdir(parents=True)
            for path in (writable / ".git/modules/vendor/objects/lib", writable / '.git/modules/qu"ote'):
                (path / "HEAD").write_text("ref: refs/heads/main\n")
            nested = seatbelt.profile(**arguments)  # A Git dir the profile cannot name stays fully denied.
            self.assertNotIn('qu"ote', nested)
            with self.assertRaises(Fault) as caught:
                seatbelt.profile(home=home, folders=[self.root / 'quote"d'], private=self.root, control=[],
                                 denied=[], exceptions=[], executable=self.root / "omc/om-code")
            self.assertEqual(caught.exception.code, "scope_unsupported")
        lines = text.splitlines()
        self.assertEqual(lines[:2], ["(version 1)", "(deny default)"])
        # Broad reads come first; denied areas, then their exceptions and writable roots, narrow them in order.
        writes = (f'(allow file-read* file-read-data file-write* process-exec (subpath "{home}") (subpath "{writable}") '
                  f'(subpath "{worktree}") (subpath "{self.root / "T/omc/lifetime"}"))')
        order = ["(allow file-read*)", f'(deny file-read* process-exec (subpath "{self.root / "home/Library"}") '
                 f'(subpath "{self.root / "T/omc"}"))',
                 f'(allow file-read* process-exec (subpath "{self.root / "home/Library/Developer"}"))', writes]
        self.assertEqual([lines.index(line) for line in order], sorted(lines.index(line) for line in order))
        self.assertNotIn("network", " ".join(line for line in lines if line.startswith("(deny")))
        # Secret-name denials precede every root re-allow, which must name file-read-data to override them.
        secret = lines.index(f'(deny file-read-data (regex #"{seatbelt.SECRET_DIRECTORY_REGEX}"))')
        self.assertLess(secret, lines.index(writes))
        # Git rules hold in every writable place, private trees included, after every write allow. Nested Git
        # areas deny every kind of entry; each exception also requires a writable root, and protected names
        # are denied after the exceptions, naming each operation they allow.
        protected = (f'(deny {seatbelt.WRITES} (regex #"{seatbelt.PROTECTED_REGEX}") '
                     f'(regex #"{seatbelt.PROTECTED_IDE_REGEX}"))')
        freeze = f'(deny file-write-create file-write-unlink (regex #"{seatbelt.NESTED_GIT_REGEX}"))'
        self.assertGreater(lines.index(freeze), lines.index(f'(allow file-read* file-write* (prefix "{self.root / "T/xcrun_db"}"))'))
        inside = (f'(require-any (subpath "{home}") (subpath "{writable}") (subpath "{worktree}") '
                  f'(subpath "{self.root / "T/omc/lifetime"}"))')
        # Agent and hook manager configuration is protected inside the folders only, never in the sandbox home.
        config = (f'(deny {seatbelt.WRITES} (require-all (require-any (subpath "{writable}") (subpath "{worktree}")) '
                  f'(regex #"{seatbelt.PROTECTED_CONFIG_REGEX}")))')
        tail = [protected,
                rf'(allow file-write-unlink (require-all {inside} (regex #"^(/.*)?/\.git$") (vnode-type REGULAR-FILE SYMLINK)))',
                "(deny file-clone (vnode-type DIRECTORY))", config,
                f'(deny file-read* file-read-data {seatbelt.WRITES} process-exec (subpath "{self.root / "state"}"))']
        self.assertEqual(lines[lines.index(freeze):], [freeze, *tail])
        self.assertNotIn(".git", "".join(line for line in lines if "literal" in line))
        # Files and data directories only inside the nested Git dirs that exist, never beside or in place of them.
        found = nested.splitlines()
        start = found.index(freeze)
        self.assertEqual((found[:start], found[start + 3:]), (lines[:lines.index(freeze)], tail))
        files, data = found[start + 1:start + 3]
        self.assertTrue(files.startswith(f"(allow file-write-create file-write-unlink (require-all {inside} (vnode-type REGULAR-FILE) "))
        self.assertTrue(data.startswith(f"(allow file-write-create file-write-unlink (require-all {inside} "
                                        "(vnode-type REGULAR-FILE DIRECTORY) "))
        files, data = ([re.compile(pattern) for pattern in re.findall(r'#"([^"]+)"', line)] for line in (files, data))
        for path in ("worktrees/wt/HEAD", "worktrees/wt/index.lock", "modules/vendor/objects/lib/HEAD"):
            self.assertTrue(any(pattern.match(str(writable / ".git" / path)) for pattern in files), path)
        for path in ("worktrees/wt/logs", "worktrees/wt/rebase-merge", "modules/vendor/objects/lib/objects/ab",
                     "modules/vendor/objects/lib/refs/heads/x"):
            self.assertTrue(any(pattern.match(str(writable / ".git" / path)) for pattern in data), path)
        # A planted HEAD would make a name directory look like a Git dir to the next profile.
        for path in ("modules/HEAD", "modules/vendor/HEAD", "modules/vendor/objects/HEAD", "worktrees/x",
                     "modules/vendor/objects/lib/modules/x/HEAD", "worktrees/new/HEAD"):
            self.assertFalse(any(pattern.match(str(writable / ".git" / path)) for pattern in files + data), path)
        for path in ("worktrees/wt", "worktrees/new/logs", "modules/vendor", "modules/vendor/objects",
                     "modules/vendor/objects/lib", "modules/vendor/objects/other", "modules/vendor/objects/lib/modules/x"):
            self.assertFalse(any(pattern.match(str(writable / ".git" / path)) for pattern in data), path)
        # A later wildcard denial does not override an allowance that names an operation, so the denials
        # name every write operation that any allowance names.
        named = {operation for line in found if line.startswith("(allow")
                 for operation in re.findall(r"file-write-[a-z]+", line)}
        self.assertLessEqual(named, set(seatbelt.WRITES.split()))
        self.assertNotIn("SecurityServer", text)
        self.assertNotIn("appleevents", text.lower())

    def test_only_the_named_executable_runs_inside_a_denied_area(self):
        client, denied = self.root / "home/Library/om/om-code", self.root / "home/Library"
        with patch.object(seatbelt, "user_temp", return_value=self.root / "T"):
            text = seatbelt.profile(home=self.root / "H", folders=[], private=self.root / "T/p", control=[self.root / "state"],
                                    denied=[denied], exceptions=[], executable=client)
        allow = f'(allow file-read* process-exec (literal "{client}"))'
        lines = text.splitlines()
        self.assertLess(lines.index(f'(deny file-read* process-exec (subpath "{denied}") (subpath "{self.root / "T"}"))'),
                        lines.index(allow))
        # It only reads and runs the client: it names no write operation the Git and control denials must cover.
        self.assertLess(lines.index(allow), lines.index(f'(deny file-write-create file-write-unlink (regex #"{seatbelt.NESTED_GIT_REGEX}"))'))
        self.assertEqual(lines[-1], f'(deny file-read* file-read-data {seatbelt.WRITES} process-exec (subpath "{self.root / "state"}"))')

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
        # Seatbelt matches names case-insensitively, as the volume resolves them; IDE settings run later too.
        protected = re.compile(f"{seatbelt.PROTECTED_REGEX}|{seatbelt.PROTECTED_IDE_REGEX}", re.IGNORECASE)
        for path in (".vscode/tasks.json", "a/.idea/x", ".VSCODE/tasks.json", ".vſcode/x", ".GIT/CONFIG", "sub/.Git",
                     ".git/hooKs/x"):
            self.assertRegex("/r/" + path, protected)
        for path in ("file.txt", ".vscodex/x", "idea/x", ".github/x"):
            self.assertNotRegex("/r/" + path, protected)
        config = re.compile(seatbelt.PROTECTED_CONFIG_REGEX)
        for path in (".claude", ".claude/settings.json", "a/.codex/config.toml", ".agents/x", ".cursor/rules/x", ".gemini",
                     ".husky/pre-commit", ".mcp.json", "sub/.mcp.json"):
            self.assertRegex("/r/" + path, config)
        for path in (".claudex/x", "claude/x", ".mcp.json.bak", "x.mcp.json", ".github/x", "CLAUDE.md"):
            self.assertNotRegex("/r/" + path, config)

    def test_nested_git_dirs_are_the_ones_git_lays_out(self):
        nested = re.compile(seatbelt.NESTED_GIT_REGEX)
        for path in (".git/modules", ".git/modules/lib", ".git/modules/a/objects/b", ".git/worktrees", ".git/worktrees/wt",
                     ".git/worktrees/wt/modules/lib", ".git/modules/lib/worktrees/wt"):
            self.assertRegex("/r/" + path, nested)
        for path in (".git", ".git/objects/ab", ".git/refs/heads", "src/modules/x", ".git-modules/x"):
            self.assertNotRegex("/r/" + path, nested)
        # Submodule names may span directories and hold any word; a Git dir is where HEAD is. Links are not followed.
        git = self.root / "repo/.git"
        heads = ("modules/vendor/objects/lib", "modules/vendor/objects/lib/modules/deps/refs/inner", "modules/plain",
                 "worktrees/wt/modules/sub")
        for name in heads:
            (git / name).mkdir(parents=True)
            (git / name / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "modules/plain/worktrees/pw").mkdir(parents=True)
        (self.root / "elsewhere/x").mkdir(parents=True)
        (self.root / "elsewhere/x/HEAD").write_text("ref: refs/heads/main\n")
        (git / "modules/linked").symlink_to(self.root / "elsewhere", target_is_directory=True)
        self.assertEqual({path.relative_to(git).as_posix() for path in seatbelt.nested_git_dirs(git)},
                         {*heads, "worktrees/wt", "modules/plain/worktrees/pw"})
        self.assertEqual(seatbelt.nested_git_dirs(self.root / "missing/.git"), [])

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

    def test_environment_holds_no_inherited_secrets(self):
        base = {"PATH": "/usr/bin", "LANG": "C", "LC_ALL": "C", "TERM": "xterm", "HOME": "/Users/real",
                "SSH_CONNECTION": "100.64.0.2 52144 100.64.0.1 22", "SSH_CLIENT": "100.64.0.2 52144 22",
                "SSH_AUTH_SOCK": "/tmp/agent", "GIT_DIR": "/elsewhere", "GITHUB_TOKEN": "secret", "OPENMACHINE_STATE": "x"}
        private = self.root / "private"
        environment = restricted.environment(base, private)
        self.assertEqual({key for key in environment if key in base}, {"PATH", "LANG", "LC_ALL", "TERM", "HOME", "SSH_CONNECTION", "SSH_CLIENT"})
        self.assertEqual((environment["HOME"], environment["TMPDIR"]), (str(private / "home"), str(private / "tmp") + "/"))


if __name__ == "__main__":
    unittest.main()
