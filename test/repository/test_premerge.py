"""Clean-candidate cache and CI identity checks without product environment setup."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from test.support.repository import RepositoryFixture


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("premerge", ROOT / "scripts/premerge.py")
premerge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(premerge)


class PremergeChecks(RepositoryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.write("openmachine/github.py", "VALUE = 2\n")
        self.commit()
        self.environment = {"python": "fixture"}

    def run_candidate(self, **kwargs):
        with patch.object(premerge, "fingerprint", return_value=self.environment):
            return premerge.run_premerge(self.root, self.base, **kwargs)

    def test_cache_is_exact_for_scope_head_base_and_selected_checks(self):
        plan = premerge.runner.make_plan(self.root, self.base, committed=True)
        record = premerge.cache_record(plan, self.environment)
        self.assertTrue(premerge.cache_matches(record, plan, self.environment))
        for field in ("head_sha", "base_sha", "selection_sha256", "scope"):
            with self.subTest(field=field):
                self.assertFalse(premerge.cache_matches(record, {**plan, field: "changed"}, self.environment))
        self.assertFalse(premerge.cache_matches(record, plan, {"python": "different"}))
        path = premerge.cache_path(self.root, plan["head_sha"])
        self.assertTrue(path.is_relative_to((self.root / ".git").resolve()))

    def test_affected_pass_cannot_satisfy_full_and_force_invalidates_failed_attempt(self):
        with patch.object(premerge, "execute_checks", return_value={"status": "passed"}) as checks:
            first = self.run_candidate()
            self.assertEqual(first["status"], "passed")
            self.assertEqual(self.run_candidate()["status"], "cached")
            self.assertEqual(checks.call_count, 1)
            self.run_candidate(full=True)
            self.assertEqual(checks.call_count, 2)
        with patch.object(premerge, "execute_checks", side_effect=ValueError("a real failed rerun")):
            with self.assertRaisesRegex(ValueError, "failed rerun"):
                self.run_candidate(force=True)
        with patch.object(premerge, "execute_checks", return_value={"status": "passed"}) as checks:
            self.assertEqual(self.run_candidate()["status"], "passed")
            checks.assert_called_once()

    def test_dirty_changes_and_clean_same_tree_head_drift_cannot_be_cached(self):
        self.write("README.md", "# Dirty\n")
        with patch.object(premerge, "execute_checks") as checks:
            with self.assertRaisesRegex(ValueError, "clean committed"):
                self.run_candidate()
            checks.assert_not_called()
        self.git("restore", "README.md")

        def drift(*args):
            self.commit("test(fixture): advance the same tree")
            return {"status": "passed"}

        with patch.object(premerge, "execute_checks", side_effect=drift):
            with self.assertRaisesRegex(ValueError, "HEAD or the requested base"):
                self.run_candidate()
        common = self.root / ".git" / "openmachine" / "premerge"
        self.assertFalse(common.exists() and list(common.glob("*.json")))

    def test_ci_always_runs_and_binds_both_pull_request_parents(self):
        with patch.object(premerge, "execute_checks", return_value={"status": "passed"}) as checks:
            self.run_candidate()
            self.run_candidate(ci=True)
            self.run_candidate(ci=True)
            self.assertEqual(checks.call_count, 3)
        head = self.git("rev-parse", "HEAD")
        event = {"pull_request": {"base": {"sha": self.base}, "head": {"sha": head}}}
        self.assertEqual(premerge.ci_candidate(self.root, "pull_request", event, head), (self.base, False))
        with self.assertRaisesRegex(ValueError, "does not match"):
            premerge.ci_candidate(self.root, "pull_request", event, self.base)
        self.assertEqual(premerge.ci_candidate(self.root, "workflow_dispatch", {}, head), (head, True))

    def test_push_uses_before_after_and_rejects_missing_history(self):
        head = self.git("rev-parse", "HEAD")
        event = {"before": self.base, "after": head}
        self.assertEqual(premerge.ci_candidate(self.root, "push", event, head), (self.base, False))
        for bad in ({**event, "before": "0" * 40}, {**event, "after": self.base},
                    {**event, "deleted": True}):
            with self.subTest(event=bad):
                with self.assertRaises(ValueError):
                    premerge.ci_candidate(self.root, "push", bad, head)


if __name__ == "__main__":
    unittest.main()
