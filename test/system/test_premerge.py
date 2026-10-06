"""Focused tests for the repository premerge driver."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("premerge", ROOT / "scripts/premerge.py")
premerge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(premerge)


class PremergeChecks(unittest.TestCase):
    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def repository(self) -> tuple[tempfile.TemporaryDirectory, Path, str, str]:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        self.git(root, "init", "-q")
        self.git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-qm", "test(premerge): establish fixture base")
        base = self.git(root, "rev-parse", "HEAD")
        self.git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-qm", "test(premerge): add fixture candidate")
        head = self.git(root, "rev-parse", "HEAD")
        return directory, root, base, head

    def test_resolve_commit_and_dirty_guard_are_explicit(self):
        directory, root, base, head = self.repository()
        with directory:
            self.assertEqual(premerge.resolve_commit(root, "HEAD"), head)
            with self.assertRaisesRegex(ValueError, "not available locally"):
                premerge.resolve_commit(root, "missing-ref")
            premerge.require_ancestor(root, base, head)
            with self.assertRaisesRegex(ValueError, "not an ancestor"):
                premerge.require_ancestor(root, head, base)
            (root / "dirty.txt").write_text("dirty\n")
            with self.assertRaisesRegex(ValueError, "clean committed worktree"):
                premerge.require_clean(root)

    def test_cache_is_git_local_and_bound_to_candidate_identity(self):
        directory, root, base, head = self.repository()
        with directory:
            environment = {"python": "fixture"}
            path = premerge.cache_path(root, head)
            self.assertTrue(path.is_relative_to((root / ".git").resolve()))
            record = premerge.cache_record(head, base, environment)
            premerge.write_cache(path, record)
            self.assertEqual(json.loads(path.read_text()), record)
            self.assertTrue(premerge.cache_matches(record, head, base, environment))
            self.assertFalse(premerge.cache_matches(record, "f" * 40, base, environment))
            self.assertFalse(premerge.cache_matches(record, head, "e" * 40, environment))
            self.assertFalse(premerge.cache_matches(record, head, base, {"python": "changed"}))

    def test_matching_cache_skips_checks_and_force_reruns(self):
        directory, root, base, head = self.repository()
        with directory:
            environment = {"python": "fixture"}
            premerge.write_cache(premerge.cache_path(root, head),
                                 premerge.cache_record(head, base, environment))
            with patch.object(premerge, "fingerprint", return_value=environment), \
                    patch.object(premerge, "execute_checks") as checks:
                result = premerge.run_premerge(root, base)
                self.assertEqual(result["status"], "cached")
                checks.assert_not_called()
            with patch.object(premerge, "fingerprint", return_value=environment), \
                    patch.object(premerge, "execute_checks") as checks:
                result = premerge.run_premerge(root, base, force=True)
                self.assertEqual(result["status"], "passed")
                checks.assert_called_once_with(root)


if __name__ == "__main__":
    unittest.main()
