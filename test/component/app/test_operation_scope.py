"""Operation scope admission: whole-map validation and typed-tool enforcement before effects."""

import copy
import os
import re
import unittest
from unittest.mock import patch
import uuid

from openmachine.access import macos as seatbelt
from openmachine.access import rules as operation_scope
from test.support.operation_scope import ScopeFixture
from test.support.tasks import task_contract


class ScopeAdmissionTests(ScopeFixture, unittest.TestCase):
    def read(self, workspace, path, scope=None):
        return self.app.read_workspace_file("local", str(workspace), path, operation_scope=scope or self.scope())

    def test_malformed_foreign_or_duplicate_maps_are_rejected(self):
        valid = self.scope()
        cases = [dict(valid, protocol=2), dict(valid, unexpected=True), dict(valid, roots=[]),
                 dict(valid, authority=dict(valid["authority"], policy_revision=True)),
                 dict(valid, roots=[dict(valid["roots"][0], machine="other")]),
                 dict(valid, roots=[valid["roots"][1], dict(valid["roots"][1])])]
        for value in cases:
            with self.subTest(value=value):
                self.fault("scope_invalid", lambda: self.read(self.core, "file.txt", value))

    def test_a_replaced_directory_is_never_rebound(self):
        value = self.scope()
        self.core.rename(self.root / "moved")
        self.core.mkdir()
        self.fault("scope_target_changed", lambda: self.read(self.cloud, "file.txt", value))

    def test_nesting_is_a_union_but_misleading_or_broad_maps_fail(self):
        (self.core / "docs").mkdir()
        same_mode = self.scope(self.binding(self.dev, "read_only"), self.binding(self.desktop, "read_only"))
        self.assertEqual(self.read(self.desktop, "file.txt", same_mode)["text"], "desktop\n")
        inner = self.scope(self.binding(self.core, "full_access"), self.binding(self.core / "docs", "read_only"))
        self.fault("scope_conflict", lambda: self.read(self.core, "file.txt", inner))
        alias = self.binding(self.dev, "full_access", workspace=self.root, relative="Development")
        self.fault("scope_conflict", lambda: self.read(self.dev, "README", self.scope(self.binding(self.dev, "read_only"), alias)))
        for home in (self.dev, self.core):
            with self.subTest(home=home), patch.object(operation_scope.macos, "account_home", return_value=home):
                self.fault("scope_too_broad", lambda: self.read(self.dev, "README"))
        with patch.object(operation_scope, "control_paths", return_value=(self.cloud / "state",)):
            self.fault("scope_conflict", lambda: self.read(self.dev, "README"))

    def test_typed_reads_cover_the_machine_except_denied_areas(self):
        self.assertEqual(self.read(self.desktop, "file.txt")["text"], "desktop\n")
        self.assertEqual(self.read(self.root, "Development/core/file.txt")["text"], "core\n")
        self.assertEqual(self.read(self.personal, "notes.txt")["text"], "personal notes\n")
        self.assertEqual(self.read(self.developer, "dev.txt")["text"], "developer\n")
        for workspace, path in ((self.drive, "drive.txt"), (self.home, "Library/CloudStorage/GoogleDrive/drive.txt"),
                                (self.home, ".ssh/id_test")):
            with self.subTest(path=path):
                self.fault("path_out_of_scope", lambda: self.read(workspace, path))
        (self.desktop / "escape").symlink_to(self.drive, target_is_directory=True)
        self.fault("path_out_of_scope", lambda: self.app.inspect_workspace(
            "local", str(self.root), "files", "Development/desktop/escape", operation_scope=self.scope()))
        # A directory that holds a denied area cannot be searched or inspected as a whole.
        self.fault("path_out_of_scope", lambda: self.app.inspect_workspace(
            "local", str(self.root), "search", ".", query="SECRET", operation_scope=self.scope()))
        self.fault("path_out_of_scope", lambda: self.app.inspect_context(
            "local", str(self.home), operation_scope=self.scope()))
        selected = self.scope(self.binding(self.drive, "read_only"), self.binding(self.core, "full_access"))
        self.assertEqual(self.read(self.drive, "drive.txt", selected)["text"], "DRIVE-SECRET\n")

    def test_typed_writes_need_full_and_never_touch_protected_files(self):
        def write(workspace, path):
            read = self.app.read_workspace_file("local", str(workspace), "file.txt")
            return self.app.write_workspace_file("local", str(workspace), path, "changed\n", read["context"]["head_sha"],
                                                 "", uuid.uuid4().hex, read["context"]["workspace_identity"],
                                                 operation_scope=self.scope())
        self.fault("path_out_of_scope", lambda: write(self.desktop, "new.txt"))
        self.fault("path_out_of_scope", lambda: write(self.personal, "new.txt"))
        self.fault("path_out_of_scope", lambda: write(self.core, ".vscode/tasks.json"))
        self.assertEqual(write(self.core, "new.txt")["status"], "applied")
        self.assertEqual((self.core / "new.txt").read_text(), "changed\n")
        self.assertFalse((self.desktop / "new.txt").exists())
        patch_text = "*** Begin Patch\n*** Add File: desktop/new.txt\n+x\n*** End Patch\n"
        self.fault("path_out_of_scope", lambda: self.app.apply_workspace_patch(
            "local", str(self.dev), patch_text, "0" * 64, "", [{"path": "desktop/new.txt", "expected_sha256": ""}],
            change_id=uuid.uuid4().hex, operation_scope=self.scope()))

    def test_protected_files_match_the_process_profile(self):
        # Seatbelt matches these names case-insensitively, as the volume resolves them.
        profile = re.compile(f"{seatbelt.PROTECTED_REGEX}|{seatbelt.PROTECTED_IDE_REGEX}", re.IGNORECASE)
        for path in (".git", "sub/.git", ".git/hooks/pre-commit", ".git/config", ".git/config.worktree", ".git/commondir",
                     ".git/worktrees/wt/config.worktree", ".git/worktrees/wt/commondir", ".git/worktrees/wt/config",
                     ".git/worktrees/wt/modules/lib/config", ".git/modules/lib/hooks/post-checkout", ".git/modules/lib/config",
                     ".vscode/tasks.json", "a/.idea/x", ".GIT/CONFIG", "sub/.Git", ".VSCODE/tasks.json", ".vſcode/x",
                     ".git/hooKs/x"):
            self.assertTrue(operation_scope.protected(self.core / path), path)
            self.assertRegex(str(self.core / path), profile)
        for path in ("file.txt", ".git/refs/heads/config", ".git/info/exclude", ".git/worktrees/wt/gitdir", "config",
                     ".gitconfig", ".github/x"):
            self.assertFalse(operation_scope.protected(self.core / path), path)
            self.assertNotRegex(str(self.core / path), profile)

    def test_only_reciprocal_worktrees_inherit_full_access(self):
        real = self.dev / "core-wt"
        self.git(self.core, "worktree", "add", "-q", "-b", "wt", str(real))
        # Scoped work can write registration metadata in a Full repository; forge one for an unrelated folder.
        forged = self.core / ".git" / "worktrees" / "forged"
        forged.mkdir()
        for name, text in (("gitdir", f"{self.personal}/.git\n"), ("commondir", "../..\n"),
                           ("HEAD", "ref: refs/heads/main\n"), ("locked", "\n")):
            (forged / name).write_text(text)
        listed = self.git(self.core, "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {self.personal}\n", listed + "\n")
        compiled = operation_scope.compile(self.config, self.state, "local", self.scope())
        trees = compiled.process_request()["worktrees"]
        self.assertEqual([(item["path"], item["mode"]) for item in trees], [(str(real), "full_access")])
        self.assertEqual(compiled.mode(real), "full_access")
        self.assertIsNone(compiled.mode(self.personal))

    def test_new_worktrees_stay_beside_their_repository_or_inside_selected_folders(self):
        core_only = operation_scope.compile(self.config, self.state, "local", self.scope(self.binding(self.core, "full_access")))
        for parent in (self.dev, self.dev / ".worktrees", self.core / "nested"):
            core_only.require_worktree_parent(parent, self.core)
        for parent in (self.personal, self.root, self.dev / "other" / ".worktrees"):
            with self.subTest(parent=parent):
                self.fault("path_out_of_scope", lambda: core_only.require_worktree_parent(parent, self.core))
        # A repository directly in the home never makes the home a worktree parent.
        with patch.object(operation_scope.macos, "account_home", return_value=self.dev):
            beside_home = operation_scope.compile(self.config, self.state, "local",
                                                  self.scope(self.binding(self.core, "full_access")))
        self.fault("path_out_of_scope", lambda: beside_home.require_worktree_parent(self.dev, self.core))
        context = self.app.read_workspace_file("local", str(self.core), "file.txt")["context"]
        for destination in (self.dev / ".claude", self.personal / "wt"):
            with self.subTest(destination=destination):
                self.fault("path_out_of_scope", lambda: self.app.create_worktree(
                    "local", str(self.core), str(destination), "main", uuid.uuid4().hex,
                    context["workspace_identity"], "wt-" + uuid.uuid4().hex[:8], operation_scope=self.scope()))
        self.assertFalse((self.dev / ".claude").exists())

    def test_unconfinable_or_unsupported_work_starts_nothing(self):
        self.fault("scope_unconfined_capability", lambda: self.app.start_codex_task(
            "local", uuid.uuid4().hex, str(self.core), "Goal", "https://example.invalid/", ["https://example.invalid"],
            operation_scope=self.scope()))
        for cwd in (self.drive, self.home):
            self.fault("path_out_of_scope", lambda: self.app.start_execution(
                "local", uuid.uuid4().hex, str(cwd), ["true"], operation_scope=self.scope()))
        with patch.object(seatbelt, "unavailable_reason", return_value="platform_unsupported"):
            capability = self.app.get_capabilities()["operation_scope"]
            self.assertEqual(capability["process"], {"backend": None, "status": "unavailable", "reason": "platform_unsupported"})
            self.fault("scope_unsupported", lambda: self.app.start_execution(
                "local", uuid.uuid4().hex, str(self.core), ["true"], operation_scope=self.scope()))
            self.assertEqual(self.read(self.core, "file.txt")["text"], "core\n")
        self.assertFalse((self.state / "executions").exists() and any((self.state / "executions").iterdir()))
        self.assertEqual(self.app.get_capabilities()["operation_scope"]["tools"], list(operation_scope.TOOLS))

    def test_task_contract_targets_must_be_full(self):
        def create(target, identity):
            context = self.app.read_workspace_file("local", str(target), "file.txt")["context"]
            request = copy.deepcopy(task_contract(None, identity))
            request["contract"]["targets"] = [{"machine": "local", "workspace": str(target),
                "workspace_identity": context["workspace_identity"], "head_sha": context["head_sha"]}]
            return self.app.create_task(**request, operation_scope=self.scope())
        self.fault("path_out_of_scope", lambda: create(self.desktop, "scoped-read-only"))
        self.assertEqual(create(self.core, "scoped-full")["task_id"], "scoped-full")


if __name__ == "__main__":
    unittest.main()
