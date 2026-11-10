"""Selected Projects scope: whole-map validation, per-binding and pairwise shape rules, inherited worktrees."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from openmachine.access import catalog, rules
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault

AUTHORITY = {"policy_revision": 7, "grant_id": "grant-fixture", "grant_revision": 3, "session_generation": 12}


class RulesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-rules-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.dev = self.root / "Development"
        self.core, self.cloud = self.dev / "core", self.dev / "cloud"
        self.personal = self.root / "personal"
        for path in (self.core, self.cloud, self.personal):
            path.mkdir(parents=True)
        for repo in (self.core, self.cloud):
            (repo / "file.txt").write_text(repo.name + "\n")
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "add", "file.txt")
            self.git(repo, "commit", "-q", "-m", "Fixture")
        self.state = self.root / "state"
        self.config = Settings.model_validate({"machines": {"local": {"transport": "local"}}})
        home = patch.object(rules.macos, "account_home", return_value=self.root / "home")
        home.start()
        self.addCleanup(home.stop)

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                              check=True, capture_output=True, text=True).stdout

    def binding(self, path, mode, *, workspace=None, relative="."):
        workspace = str(path) if workspace is None else str(workspace)
        target = catalog.inspect(self.config, "local", workspace, "inspect", relative)["project"]["target_id"]
        return {"machine": "local", "workspace_name": workspace, "relative_path": relative, "target_id": target, "mode": mode}

    def scope(self, *bindings):
        bindings = bindings or (self.binding(self.dev, "read_only"), self.binding(self.core, "full_access"),
                                self.binding(self.cloud, "full_access"))
        return {"protocol": 1, "authority": dict(AUTHORITY), "roots": list(bindings)}

    def compile(self, value=None):
        return rules.compile(self.config, self.state, "local", value or self.scope())

    def fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def test_malformed_foreign_or_duplicate_maps_are_rejected(self):
        valid = self.scope()
        for value in (dict(valid, protocol=2), dict(valid, unexpected=True), dict(valid, roots=[]),
                      dict(valid, authority=dict(valid["authority"], policy_revision=True)),
                      dict(valid, roots=[dict(valid["roots"][0], machine="other")]),
                      dict(valid, roots=[valid["roots"][1], dict(valid["roots"][1])])):
            with self.subTest(value=value):
                self.fault("scope_invalid", lambda: self.compile(value))

    def test_a_replaced_directory_is_never_rebound(self):
        value = self.scope()
        self.core.rename(self.root / "moved")
        self.core.mkdir()
        self.fault("scope_target_changed", lambda: self.compile(value))

    def test_nesting_is_a_union_but_misleading_or_broad_maps_fail(self):
        (self.core / "docs").mkdir()
        nested = self.compile(self.scope(self.binding(self.dev, "read_only"), self.binding(self.core, "full_access")))
        self.assertEqual(nested.grants(), [(self.dev, "read_only"), (self.core, "full_access")])
        inner = self.scope(self.binding(self.core, "full_access"), self.binding(self.core / "docs", "read_only"))
        self.fault("scope_conflict", lambda: self.compile(inner))
        alias = self.binding(self.dev, "full_access", workspace=self.root, relative="Development")
        self.fault("scope_conflict", lambda: self.compile(self.scope(self.binding(self.dev, "read_only"), alias)))
        # Two paths for one directory that resolution keeps apart: the macOS data-volume firmlink.
        data = Path("/System/Volumes/Data") / self.dev.relative_to("/")
        if data.is_dir():
            firmlink = self.binding(data, "full_access")
            self.fault("scope_conflict", lambda: self.compile(self.scope(self.binding(self.dev, "read_only"), firmlink)))
        for home in (self.dev, self.core):
            with self.subTest(home=home), patch.object(rules.macos, "account_home", return_value=home):
                self.fault("scope_too_broad", lambda: self.compile())
        with patch.object(rules, "control_paths", return_value=(self.cloud / "state",)):
            self.fault("scope_conflict", lambda: self.compile())

    def test_only_reciprocal_worktrees_inherit_their_repository_mode(self):
        real = self.dev / "core-wt"
        self.git(self.core, "worktree", "add", "-q", "-b", "wt", str(real))
        # Scoped work can write registration metadata in a Full repository; forge one for an unrelated folder.
        forged = self.core / ".git" / "worktrees" / "forged"
        forged.mkdir()
        for name, text in (("gitdir", f"{self.personal}/.git\n"), ("commondir", "../..\n"),
                           ("HEAD", "ref: refs/heads/main\n"), ("locked", "\n")):
            (forged / name).write_text(text)
        self.assertIn(f"worktree {self.personal}\n", self.git(self.core, "worktree", "list", "--porcelain") + "\n")
        grants = self.compile().grants()
        self.assertIn((real, "full_access"), grants)
        self.assertNotIn(self.personal, [path for path, _ in grants])
        # A worktree a Read-only repository holds stays read-only, even inside no selected folder.
        read_only = self.compile(self.scope(self.binding(self.core, "read_only")))
        self.assertEqual(read_only.grants(), [(self.core, "read_only"), (real, "read_only")])


if __name__ == "__main__":
    unittest.main()
