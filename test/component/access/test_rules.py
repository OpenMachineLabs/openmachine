"""Workspace folders: the folder rule's codes and canonical paths, and the worktrees a folder grants."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from openmachine.access import rules
from openmachine.access.__main__ import folders_reply


@unittest.skipUnless(sys.platform == "darwin", "folders resolve through the macOS F_GETPATH")
class FolderRuleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-rules-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.code, self.notes = self.home / "code", self.home / "Documents/notes"
        for path in (self.code, self.notes, self.home / "Library/LaunchAgents", self.home / ".ssh", self.home / ".local/state"):
            path.mkdir(parents=True)
        self.control = (self.home / ".local/state",)
        home = patch.object(rules.seatbelt, "account_home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def check(self, *paths, admission=False):
        return rules.folders([str(path) for path in paths], self.control, admission=admission)

    def refused(self, code, index, *paths, admission=False):
        with self.assertRaises(rules.FolderFault) as caught:
            self.check(*paths, admission=admission)
        self.assertEqual((caught.exception.code, caught.exception.index, caught.exception.effect),
                         (code, index, "known_no_effect"))
        self.assertIn(str(paths[index]), str(caught.exception))
        self.assertIn("Open Machine app on this Mac", str(caught.exception))

    def test_folders_resolve_to_their_kernel_path_in_order(self):
        link = self.root / "link"
        link.symlink_to(self.code, target_is_directory=True)
        tmp = Path("/tmp") / self.root.name  # /tmp is a link to /private/tmp
        self.assertEqual(self.check(self.notes, link, admission=False), [self.notes, self.code])
        self.assertEqual(self.check(tmp / "home/code"), [self.code])
        # Another case spelling gives the on-disk names.
        self.assertEqual(self.check(Path(str(self.code).upper())), [self.code])
        data = Path("/System/Volumes/Data") / self.code.relative_to("/")
        if data.is_dir():  # The data-volume firmlink spelling.
            self.assertEqual(self.check(data), [self.code])

    def test_each_code_names_the_refused_folder(self):
        (self.root / "file").write_text("x\n")
        locked = self.root / "locked"
        locked.mkdir(mode=0o000)
        self.addCleanup(locked.chmod, 0o700)
        quoted, broken = self.root / 'qu"ote', self.root / "line\nbreak"
        for path in (quoted, broken, self.home / ".config"):
            path.mkdir()
        (self.root / "broken").symlink_to(broken, target_is_directory=True)
        for code, path in (("folder_missing", self.root / "missing"), ("folder_not_directory", self.root / "file"),
                           ("folder_access_denied", locked), ("folder_unsupported", quoted),
                           ("folder_unsupported", self.root / "broken"),
                           ("folder_too_broad", Path("/")), ("folder_too_broad", self.home), ("folder_too_broad", self.root),
                           ("folder_protected", self.home / "Library/LaunchAgents"), ("folder_protected", self.home / ".ssh"),
                           ("folder_protected", self.home / "Library"), ("folder_protected", self.home / ".config"),
                           ("folder_protected", Path("/Volumes")), ("folder_protected", Path("/dev")),
                           ("folder_protected", rules.seatbelt.user_temp()), ("folder_protected", rules.seatbelt.user_temp().parent),
                           ("folder_conflict", self.home / ".local"), ("folder_conflict", self.home / ".local/state")):
            with self.subTest(code=code, path=path):
                self.refused(code, 1, self.notes, path)
        # The account database or a control path may spell a name in another case than the disk does.
        with patch.object(rules.seatbelt, "account_home", return_value=Path(str(self.home).swapcase())):
            self.refused("folder_too_broad", 0, self.home)
        self.control = (Path(str(self.code).swapcase()),)
        self.refused("folder_conflict", 0, self.code)

    def test_a_folder_may_not_repeat_or_lie_inside_another(self):
        (self.code / "docs").mkdir()
        self.refused("folder_overlap", 1, self.code, self.code)
        self.refused("folder_overlap", 1, self.code, Path(str(self.code).upper()))
        self.refused("folder_overlap", 1, self.code, self.code / "docs")
        self.refused("folder_overlap", 0, self.code / "docs", self.code)
        self.assertEqual(self.check(self.code, self.notes), [self.code, self.notes])

    def test_admission_refuses_a_folder_whose_kernel_path_changed(self):
        self.assertEqual(self.check(self.code, admission=True), [self.code])
        link = self.root / "link"
        link.symlink_to(self.code, target_is_directory=True)
        for path in (link, Path(str(self.code).upper())):
            with self.subTest(path=path):
                self.refused("folder_changed", 0, path, admission=True)
        self.code.rename(self.root / "moved")
        self.refused("folder_missing", 0, self.code, admission=True)

    def test_admission_reads_no_entry_of_a_folder(self):
        with patch.object(rules.os, "scandir", side_effect=PermissionError):
            self.assertEqual(self.check(self.code, admission=True), [self.code])
            self.refused("folder_access_denied", 0, self.code)

    def test_the_folders_command_answers_json_on_stdin_and_stdout(self):
        reply = lambda value: folders_reply(self.home / ".local/state", json.dumps(value).encode())
        self.assertEqual(reply({"folders": [str(self.notes), str(self.code)]}), {"folders": [str(self.notes), str(self.code)]})
        self.assertEqual(reply({"folders": [str(self.code), str(self.home)]}), {"error": {"code": "folder_too_broad", "index": 1}})
        for value in ({}, {"folders": []}, {"folders": ["relative"]}, {"folders": [str(self.code)] * 33},
                      {"folders": [str(self.code)], "extra": 1}, {"folders": [str(self.code) + "\0"]}, []):
            with self.subTest(value=value):
                self.assertEqual(reply(value), {"error": {"code": "invalid_request"}})
        self.assertEqual(folders_reply(self.root, b"{"), {"error": {"code": "invalid_request"}})


class WorktreeGrantTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-rules-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.core, self.personal = self.root / "core", self.root / "personal"
        for path in (self.core, self.personal):
            path.mkdir()
        (self.core / "file.txt").write_text("core\n")
        self.git(self.core, "init", "-q", "-b", "main")
        self.git(self.core, "add", "file.txt")
        self.git(self.core, "commit", "-q", "-m", "Fixture")

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                              check=True, capture_output=True, text=True).stdout

    def grants(self, *folders, control=()):
        return rules.Scope(folders, control).grants()

    def test_only_reciprocal_worktrees_of_a_folder_repository_are_granted(self):
        real = self.root / "core-wt"
        self.git(self.core, "worktree", "add", "-q", "-b", "wt", str(real))
        # Workspace work can write registration metadata in a folder's repository; forge one for an unrelated folder.
        forged = self.core / ".git" / "worktrees" / "forged"
        forged.mkdir()
        for name, text in (("gitdir", f"{self.personal}/.git\n"), ("commondir", "../..\n"),
                           ("HEAD", "ref: refs/heads/main\n"), ("locked", "\n")):
            (forged / name).write_text(text)
        self.assertIn(f"worktree {self.personal}\n", self.git(self.core, "worktree", "list", "--porcelain") + "\n")
        self.assertEqual(self.grants(self.core), [self.core, real])
        # A worktree inside Core's control paths, or already inside a folder, adds nothing.
        self.assertEqual(self.grants(self.core, control=(real,)), [self.core])
        self.assertEqual(self.grants(self.core, self.root), [self.core, self.root])
        # A linked worktree that is a folder grants itself, never its sibling worktrees.
        sibling = self.root / "core-sibling"
        self.git(self.core, "worktree", "add", "-q", "-b", "sibling", str(sibling))
        self.assertEqual(self.grants(real), [real])


if __name__ == "__main__":
    unittest.main()
