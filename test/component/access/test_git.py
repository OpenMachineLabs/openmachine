"""Git identity observation fails closed: classifications, missing Git and unsupported object formats."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from openmachine.access import git

GIT = ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null")


class GitContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-git-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve() / "work"
        self.work.mkdir()
        (self.work / "file.txt").write_text("fixture\n")

    def git(self, *args):
        subprocess.run(["git", *GIT, "-C", str(self.work), *args], check=True, capture_output=True)

    def test_classifications_and_missing_git_fail_closed(self):
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(git.context(self.work)["git_state"], "non_git")
        self.git("init", "-q", "-b", "main")
        self.assertEqual(git.context(self.work)["git_state"], "unborn")
        self.git("add", "file.txt")
        self.git("commit", "-q", "-m", "Initial")
        self.assertEqual(git.context(self.work)["git_state"], "worktree")
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(git.context(self.work)["git_state"], "unavailable")
        self.git("checkout", "--detach", "-q")
        self.assertEqual(git.context(self.work)["git_state"], "detached")
        (self.work / ".git/HEAD").write_text("broken HEAD")
        self.assertEqual(git.context(self.work)["git_state"], "unavailable")

    def test_an_unsupported_object_format_is_unavailable(self):
        self.git("init", "-q", "-b", "main")
        actual = git.capture

        def unsupported(root, args):
            raw, code, clipped = actual(root, args)
            if "--show-object-format" in args:
                raw = raw.replace(b"sha1\n", b"sha256\n")
            return raw, code, clipped

        with patch.object(git, "capture", side_effect=unsupported):
            result = git.context(self.work)
        self.assertEqual((result["git_state"], result["context_error"]), ("unavailable", "unsupported_git_object_format"))

    def test_an_incomplete_worktree_list_is_unavailable(self):
        self.git("init", "-q", "-b", "main")
        for result in ((b"", 128, False), (b"worktree " + bytes(self.work), 0, True)):
            with self.subTest(result=result), patch.object(git, "capture", return_value=result):
                with self.assertRaises(git.Fault) as raised:
                    git.registered_worktrees(self.work)
                self.assertEqual(raised.exception.code, "git_unavailable")


if __name__ == "__main__":
    unittest.main()
