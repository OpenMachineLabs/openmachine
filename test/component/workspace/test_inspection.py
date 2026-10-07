"""Tests for workspace behavior at the component layer."""

import os
import unittest
from openmachine import workspace
from openmachine.workspace.git import MAX_READ
from openmachine.config import Workspace
from openmachine.foundation.sanitize import Fault
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_dotfiles_are_read_but_credentials_and_escaping_symlinks_are_not(self):
        (self.work / ".github").mkdir()
        (self.work / ".github/AGENTS.md").write_text("Read this instruction.\n")
        result = workspace.inspect(self.config, "my-laptop", "project", "read", ".github/AGENTS.md")
        self.assertEqual(result["text"], "Read this instruction.\n")
        (self.work / ".env").write_text("private fixture\n")
        (self.root / "outside").write_text("outside fixture\n")
        (self.work / "escape").symlink_to(self.root / "outside")
        (self.work / "hidden-secret").symlink_to(self.work / ".env")
        for path in (".env", "../outside", "escape", "hidden-secret"):
            with self.subTest(path=path), self.assertRaises(Fault):
                workspace.inspect(self.config, "my-laptop", "project", "read", path)
        names = [item["name"] for item in workspace.inspect(self.config, "my-laptop", "project", "files")["entries"]]
        self.assertNotIn(".env", names)

    def test_binary_and_fifo_reads_do_not_block(self):
        (self.work / "binary").write_bytes(b"a\0b")
        os.mkfifo(self.work / "fifo")
        for path in ("binary", "fifo"):
            with self.subTest(path=path), self.assertRaises(Fault):
                workspace.inspect(self.config, "my-laptop", "project", "read", path)

    def test_file_byte_pagination_and_listing(self):
        content = "x" * (MAX_READ * 3)
        (self.work / "large.txt").write_text(content)
        parts, offset = [], 0
        while True:
            page = workspace.inspect(self.config, "my-laptop", "project", "read", "large.txt", offset)
            parts.append(page["text"])
            offset = page["next_offset"]
            if not page["has_more"]:
                break
        self.assertEqual("".join(parts), content)
        self.assertEqual(len(parts), 6)
        for name in ("a", "b", "c"):
            (self.work / name).touch()
        page = workspace.inspect(self.config, "my-laptop", "project", "files", limit=2)
        self.assertEqual((len(page["entries"]), page["next_offset"]), (2, 2))

    def test_git_status_and_diff_preserve_index_and_distinguish_changes(self):
        self._repository()
        code = self.work / "code.py"
        code.write_text("value = 2\n")
        self._git("add", "code.py")
        code.write_text("value = 3\n")
        (self.work / "unrelated.txt").write_text("preserve me\n")
        (self.work / ".env").write_text("private fixture\n")
        index = (self.work / ".git/index").read_bytes()
        result = workspace.inspect(self.config, "my-laptop", "project", "status")
        items = {item["path"]: item for item in result["entries"]}
        self.assertEqual((items["code.py"]["index"], items["code.py"]["worktree"]), ("M", "M"))
        self.assertTrue(items["unrelated.txt"]["untracked"])
        self.assertNotIn(".env", items)
        unstaged = workspace.inspect(self.config, "my-laptop", "project", "diff")
        staged = workspace.inspect(self.config, "my-laptop", "project", "diff", staged=True)
        self.assertIn("+value = 3", unstaged["text"])
        self.assertIn("+value = 2", staged["text"])
        self.assertEqual((self.work / ".git/index").read_bytes(), index)
        self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve me\n")

    def test_staged_deletions_and_nested_workspace_scope(self):
        self._repository()
        sub = self.work / "sub"
        sub.mkdir()
        (sub / "deleted.txt").write_text("old content\n")
        (sub / ".env").write_text("secret fixture\n")
        self._git("add", "sub")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "nested")
        self._git("rm", "sub/deleted.txt")
        (sub / ".env").write_text("new secret fixture\n")
        self._git("add", "sub/.env")
        (self.work / "code.py").write_text("outside scope\n")
        self.config.workspaces["nested"] = Workspace(machine="my-laptop", root=str(sub))
        result = workspace.inspect(self.config, "my-laptop", "nested", "diff", staged=True)
        self.assertIn("-old content", result["text"])
        self.assertNotIn("secret fixture", result["text"])
        self.assertEqual(result["skipped_paths"], 1)
        direct = workspace.inspect(self.config, "my-laptop", "nested", "diff", "deleted.txt", staged=True)
        self.assertIn("deleted file", direct["text"])
        self.assertEqual(workspace.inspect(self.config, "my-laptop", "nested", "diff")["text"], "")
        status = workspace.inspect(self.config, "my-laptop", "nested", "status")
        self.assertEqual([entry["path"] for entry in status["entries"]], ["deleted.txt"])

    def test_git_pages_reject_changed_snapshot_and_literal_paths_are_safe(self):
        self._repository()
        (self.work / "code.py").write_text("changed line\n" * 10000)
        first = workspace.inspect(self.config, "my-laptop", "project", "diff")
        self.assertTrue(first["has_more"])
        second = workspace.inspect(self.config, "my-laptop", "project", "diff", offset=first["next_offset"],
                                   expected_snapshot=first["snapshot"])
        self.assertEqual(first["snapshot"], second["snapshot"])
        (self.work / "code.py").write_text("different\n")
        with self.assertRaises(Fault) as caught:
            workspace.inspect(self.config, "my-laptop", "project", "diff", expected_snapshot=first["snapshot"])
        self.assertEqual(caught.exception.code, "source_changed")
        for path in ("../code.py", ".env", str(self.work / "code.py")):
            with self.subTest(path=path), self.assertRaises(Fault):
                workspace.inspect(self.config, "my-laptop", "project", "diff", path)

    def test_git_readers_never_run_external_filters_or_fsmonitor(self):
        self._repository()
        hook = self.work / "reader-hook.sh"
        hook.write_text('#!/bin/sh\ntouch "' + str(self.work / 'hook-ran') + '"\ncat\n')
        hook.chmod(0o700)
        (self.work / ".gitattributes").write_text("*.py diff=fixture filter=fixture\n")
        for name in ("diff.fixture.command", "diff.fixture.textconv", "filter.fixture.clean", "filter.fixture.process", "core.fsmonitor"):
            self._git("config", name, str(hook))
        self._git("config", "filter.fixture.required", "true")
        (self.work / "code.py").write_text("value = 5\n")
        status = workspace.inspect(self.config, "my-laptop", "project", "status")
        diff = workspace.inspect(self.config, "my-laptop", "project", "diff")
        self.assertTrue(status["entries"])
        self.assertIn("+value = 5", diff["text"])
        self.assertTrue(any("filters" in warning for warning in diff["warnings"]))
        self.assertFalse((self.work / "hook-ran").exists())

    def test_literal_search_pages_skips_and_edit_detection(self):
        (self.work / ".github").mkdir()
        (self.work / ".github/AGENTS.md").write_text("needle instruction\n")
        (self.work / "source.py").write_text("needle first\nneedle second\n")
        (self.work / ".env").write_text("needle secret fixture\n")
        (self.work / "binary").write_bytes(b"needle\0")
        (self.work / "link").symlink_to(self.root / "outside")
        (self.root / "outside").write_text("needle outside\n")
        first = workspace.inspect(self.config, "my-laptop", "project", "search", query="needle", limit=1)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["matches"][0]["path"], ".github/AGENTS.md")
        self.assertGreater(first["skipped"]["excluded"], 0)
        self.assertEqual(first["skipped"]["binary"], 1)
        second = workspace.inspect(self.config, "my-laptop", "project", "search", query="needle", offset=1,
                                   expected_snapshot=first["snapshot"])
        self.assertEqual(len(second["matches"]), 2)
        (self.work / "source.py").write_text("needle changed\n")
        with self.assertRaises(Fault) as caught:
            workspace.inspect(self.config, "my-laptop", "project", "search", query="needle", expected_snapshot=first["snapshot"])
        self.assertEqual(caught.exception.code, "source_changed")

    def test_search_limits_and_non_git_failure_are_explicit(self):
        (self.work / "large.txt").write_text("x" * (workspace.MAX_CAPTURE + 1) + "needle")
        result = workspace.inspect(self.config, "my-laptop", "project", "search", query="needle")
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["skipped"]["oversized"], 1)
        with self.assertRaises(Fault) as caught:
            workspace.inspect(self.config, "my-laptop", "project", "status")
        self.assertEqual(caught.exception.code, "git_unavailable")


if __name__ == "__main__":
    unittest.main()
