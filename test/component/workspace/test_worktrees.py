"""Registered worktree selection and fixed creation plans preserve original work."""

from copy import deepcopy
import os
from pathlib import Path
import shlex
import subprocess
import unittest
import uuid

from openmachine import workspace
from openmachine.config import Workspace
from openmachine.foundation.sanitize import Fault
from openmachine.workspace import worktrees
from openmachine.workspace.git import context
from test.support.workspace import WorkspaceFixture


class WorktreeTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.initialize_git()
        self.target = self.root / "isolated"

    def grant(self):
        self.config.workspaces["trees"] = Workspace(machine="local", root=str(self.root))

    def plan(self, **updates):
        self.grant()
        request = dict(destination=str(self.target), base_ref="HEAD", new_branch="isolated-task",
                       expected_workspace_identity=context(self.work)["workspace_identity"])
        return worktrees.prepare_creation(self.config, "local", "project", **(request | updates))

    def run_plan(self, plan):
        result = subprocess.run(plan["argv"], stdin=subprocess.DEVNULL, capture_output=True,
                                env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")})
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return worktrees.verify_creation(plan)

    def test_discovery_reports_existing_registration_and_grants_without_changing_source(self):
        self.git("worktree", "add", "--quiet", "--detach", str(self.target), "HEAD")
        self.file.write_text("unfinished source work\n")
        index = (self.work / ".git/index").read_bytes()
        first = workspace.inspect(self.config, "local", "project", "worktrees", limit=1)
        self.assertEqual(first["entries"][0]["worktree"], str(self.work))
        self.assertTrue(first["entries"][0]["authorized"])
        second = workspace.inspect(self.config, "local", "project", "worktrees",
                                   offset=first["next_offset"], expected_snapshot=first["snapshot"])
        self.assertEqual(second["entries"][0]["worktree"], str(self.target))
        self.assertTrue(second["entries"][0]["detached"])
        self.assertFalse(second["entries"][0]["authorized"])
        self.assertEqual(self.file.read_text(), "unfinished source work\n")
        self.assertEqual((self.work / ".git/index").read_bytes(), index)
        self.grant()
        listed = workspace.inspect(self.config, "local", "project", "worktrees")
        self.assertTrue(all(item["authorized"] for item in listed["entries"]))

    def test_discovery_snapshot_changes_after_registration_and_rejects_filters(self):
        first = workspace.inspect(self.config, "local", "project", "worktrees")
        self.git("worktree", "add", "--quiet", "--detach", str(self.target), "HEAD")
        self.fault("source_changed", lambda: workspace.inspect(self.config, "local", "project", "worktrees",
                                                              expected_snapshot=first["snapshot"]))
        self.fault("invalid_filter", lambda: workspace.inspect(self.config, "local", "project", "worktrees", path="source.py"))
        self.fault("invalid_range", lambda: workspace.inspect(self.config, "local", "project", "worktrees", offset=4))

    def test_explicit_selection_is_a_request_copy_and_supports_original_change_readback(self):
        original = self.file.read_bytes()
        plan = self.plan()
        self.run_plan(plan)
        selected = worktrees.select(self.config, "local", "project", str(self.target))
        self.assertIsNot(selected, self.config)
        self.assertEqual(self.config.workspaces["project"].root, str(self.work))
        self.assertEqual(selected.workspaces["project"].root, str(self.target))
        self.assertIs(worktrees.select(self.config, "local", "project"), self.config)
        observed = workspace.read_text_file(selected, "local", "project", "source.py")
        change_id = uuid.uuid4().hex
        changed = workspace.edit_text_file(selected, self.state, "local", "project", "source.py",
            [{"old_text": "41", "new_text": "42"}], observed["context"]["workspace_identity"],
            observed["context"]["head_sha"], observed["sha256"], change_id=change_id)
        self.assertEqual(changed["status"], "applied")
        self.assertEqual(self.file.read_bytes(), original)
        self.assertIn(b"42", (self.target / "source.py").read_bytes())
        receipt = workspace.read_change(self.config, self.state, "local", change_id, observe_current=True)
        self.assertEqual(receipt["context"]["root"], str(self.target))
        self.assertEqual(receipt["current"]["sha256"], workspace.read_text_file(selected, "local", "project", "source.py")["sha256"])
        self.config.workspaces.pop("trees")
        self.fault("scope_unavailable", lambda: workspace.read_change(self.config, self.state, "local", change_id))

    def test_selection_requires_same_repository_registration_and_existing_scope(self):
        self.git("worktree", "add", "--quiet", "--detach", str(self.target), "HEAD")
        self.fault("path_denied", lambda: worktrees.select(self.config, "local", "project", str(self.target)))
        self.grant()
        unrelated = self.root / "unrelated"
        subprocess.run(["git", "clone", "--quiet", str(self.work), str(unrelated)], check=True, capture_output=True)
        self.fault("unknown_worktree", lambda: worktrees.select(self.config, "local", "project", str(unrelated)))
        nested = self.work / "nested"
        nested.mkdir()
        self.config.workspaces["partial"] = Workspace(machine="local", root=str(nested))
        self.fault("git_unavailable", lambda: worktrees.select(self.config, "local", "partial", str(self.target)))
        alias = self.root / "alias"
        alias.symlink_to(self.target, target_is_directory=True)
        self.fault("path_denied", lambda: worktrees.select(self.config, "local", "project", str(alias)))

    def test_selected_inspection_reads_target_without_changing_config(self):
        self.run_plan(self.plan())
        (self.target / "source.py").write_text("isolated version\n")
        selected = workspace.inspect(self.config, "local", "project", "read", "source.py", worktree=str(self.target))
        original = workspace.inspect(self.config, "local", "project", "read", "source.py")
        self.assertEqual(selected["text"], "isolated version\n")
        self.assertIn("41", original["text"])
        self.assertEqual(self.config.workspaces["project"].root, str(self.work))

    def test_creation_pins_explicit_base_and_does_not_copy_uncommitted_content(self):
        head = self.git("rev-parse", "HEAD")
        self.file.write_text("unfinished source work\n")
        (self.work / "untracked.txt").write_text("keep here\n")
        index = (self.work / ".git/index").read_bytes()
        plan = self.plan()
        self.assertFalse(self.target.exists())
        self.assertEqual(plan["base_commit"], head)
        created = self.run_plan(plan)
        self.assertEqual(created["branch"], "isolated-task")
        self.assertEqual(created["head_sha"], head)
        self.assertIn(b"41", (self.target / "source.py").read_bytes())
        self.assertFalse((self.target / "untracked.txt").exists())
        self.assertEqual(self.file.read_text(), "unfinished source work\n")
        self.assertEqual((self.work / ".git/index").read_bytes(), index)

    def test_detached_creation_remains_pinned_when_source_head_advances(self):
        plan = self.plan(new_branch="")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "Advance source")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), plan["base_commit"])
        worktrees.validate_creation(plan)
        created = self.run_plan(plan)
        self.assertIsNone(created["branch"])
        self.assertEqual(created["head_sha"], plan["base_commit"])

    def test_creation_rejects_ungranted_occupied_control_and_symlink_paths(self):
        identity = context(self.work)["workspace_identity"]
        self.fault("path_denied", lambda: worktrees.prepare_creation(self.config, "local", "project",
            str(self.target), "HEAD", "isolated-task", identity))
        self.grant()
        self.target.mkdir()
        (self.target / "preserve").write_text("unrelated\n")
        self.fault("destination_exists", lambda: self.plan())
        self.assertEqual((self.target / "preserve").read_text(), "unrelated\n")
        for relative in (".git/unsafe", ".ssh/unsafe", "../escape"):
            self.fault("path_denied", lambda relative=relative: self.plan(destination=str(self.work / relative)))
        alias = self.root / "alias"
        alias.symlink_to(self.work, target_is_directory=True)
        self.fault("path_denied", lambda: self.plan(destination=str(alias / "new")))

    def test_creation_rejects_existing_branch_invalid_ref_and_stale_identity(self):
        self.fault("branch_exists", lambda: self.plan(new_branch="main"))
        self.fault("invalid_branch", lambda: self.plan(new_branch="../unsafe"))
        self.fault("invalid_ref", lambda: self.plan(base_ref="missing-reference"))
        self.fault("context_mismatch", lambda: self.plan(expected_workspace_identity="0" * 64))
        self.assertFalse(self.target.exists())

    def test_creation_rejects_nested_destinations_in_source_or_foreign_worktrees(self):
        self.fault("nested_workspace", lambda: self.plan(destination=str(self.work / "nested-tree")))
        other = self.root / "other-repository"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(other)], check=True, capture_output=True)
        self.fault("nested_workspace", lambda: self.plan(destination=str(other / "nested-tree")))
        self.assertFalse((self.work / "nested-tree").exists())
        self.assertFalse((other / "nested-tree").exists())

    def test_prelaunch_rejects_a_new_git_repository_in_the_destination_parent(self):
        plan = self.plan()
        subprocess.run(["git", "init", "--quiet", "-b", "container", str(self.root)], check=True, capture_output=True)
        self.fault("nested_workspace", lambda: worktrees.validate_creation(plan))
        self.assertFalse(self.target.exists())

    def test_creation_disables_external_filters_and_checkout_hooks(self):
        sentinel = self.root / "hook-ran"
        hook = self.root / "hook.sh"
        hook.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\ncat\n')
        hook.chmod(0o700)
        attributes = self.work / ".gitattributes"
        attributes.write_text("source.py filter=fixture\n")
        self.git("add", ".gitattributes")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Filter fixture")
        for name in ("filter.fixture.clean", "filter.fixture.smudge", "filter.fixture.process", "core.fsmonitor"):
            self.git("config", name, str(hook))
        self.git("config", "filter.fixture.required", "true")
        hooks = self.work / ".git/hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "post-checkout").write_bytes(hook.read_bytes())
        (hooks / "post-checkout").chmod(0o700)
        plan = self.plan()
        self.assertFalse(sentinel.exists())
        self.run_plan(plan)
        self.assertFalse(sentinel.exists())
        self.assertIn(b"41", (self.target / "source.py").read_bytes())

    def test_prelaunch_rechecks_new_destination_branch_and_filter_settings(self):
        plan = self.plan()
        self.target.mkdir()
        self.fault("destination_exists", lambda: worktrees.validate_creation(plan))
        self.target.rmdir()
        self.git("branch", "isolated-task")
        self.fault("branch_exists", lambda: worktrees.validate_creation(plan))
        self.git("branch", "-D", "isolated-task")
        self.git("config", "filter.later.smudge", "false")
        self.fault("context_mismatch", lambda: worktrees.validate_creation(plan))

    def test_creation_rejects_filters_hidden_behind_conditional_gitdir_configuration(self):
        sentinel = self.root / "late-filter-ran"
        hook = self.root / "late-filter.sh"
        hook.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\ncat\n')
        hook.chmod(0o700)
        included = self.root / "conditional-config"
        included.write_text('[filter "late"]\n smudge = ' + str(hook) + '\n')
        self.git("config", "includeIf.gitdir:**/worktrees/**.path", str(included))
        self.fault("git_config_unavailable", lambda: self.plan())
        self.assertFalse(self.target.exists())
        self.assertFalse(sentinel.exists())

    def test_creation_rejects_each_automatic_fetch_marker_before_source_selection(self):
        self.grant()
        identity = context(self.work)["workspace_identity"]
        for key, value in (("extensions.partialClone", "origin"), ("remote.fixture.promisor", "false"),
                           ("remote.fixture.partialCloneFilter", "blob:none")):
            with self.subTest(key=key):
                self.git("config", key, value)
                try:
                    for selected in ("", str(self.work)):
                        self.fault("git_config_unavailable", lambda selected=selected: worktrees.prepare_creation(
                            self.config, "local", "project", str(self.target), "HEAD", "isolated-task", identity, selected))
                    self.assertIs(worktrees.select(self.config, "local", "project"), self.config)
                    self.assertFalse(self.target.exists())
                finally:
                    self.git("config", "--unset", key)

    def test_missing_promisor_head_cannot_fetch_during_creation_or_explicit_selection(self):
        self.grant()
        self.git("config", "uploadpack.allowFilter", "true")
        partial = self.root / "partial-clone"
        subprocess.run(["git", "-c", "protocol.file.allow=always", "clone", "--quiet", "--filter=blob:none",
                        "--no-checkout", self.work.as_uri(), str(partial)], check=True, capture_output=True)
        missing = subprocess.check_output(["git", "-C", str(partial), "rev-list", "--objects", "--missing=print", "HEAD"])
        self.assertIn(b"?", missing, "The local fixture must retain a missing promised object.")
        identity = context(partial)["workspace_identity"]
        self.assertIsNotNone(identity)
        sentinel = self.root / "upload-pack-ran"
        upload_pack = self.root / "observe-upload-pack.sh"
        upload_pack.write_text("#!/bin/sh\nprintf attempted > " + shlex.quote(str(sentinel)) +
                               '\nexec git-upload-pack "$@"\n')
        upload_pack.chmod(0o700)
        subprocess.run(["git", "-C", str(partial), "config", "remote.origin.uploadpack", shlex.quote(str(upload_pack))],
                       check=True, capture_output=True)
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "Missing promised head")
        # Updating this fixture ref directly avoids validating (and fetching) its
        # missing commit before the operation under test.
        (partial / ".git/refs/heads/main").write_text(self.git("rev-parse", "HEAD") + "\n")
        objects = partial / ".git/objects"
        before = {str(path.relative_to(objects)): path.read_bytes() for path in objects.rglob("*") if path.is_file()}
        config = deepcopy(self.config)
        config.workspaces["project"].root = str(partial)
        for selected in ("", str(partial)):
            with self.subTest(selected=bool(selected)):
                self.fault("git_config_unavailable", lambda selected=selected: worktrees.prepare_creation(
                    config, "local", "project", str(self.target), "HEAD", "isolated-task", identity, selected))
        self.fault("git_config_unavailable", lambda: worktrees.resolve_root(config, "local", "project", str(partial)))
        self.assertFalse(sentinel.exists())
        self.assertFalse(self.target.exists())
        self.assertEqual(before, {str(path.relative_to(objects)): path.read_bytes() for path in objects.rglob("*") if path.is_file()})

    def test_prelaunch_rejects_new_promisor_configuration_without_invalidating_the_receipt(self):
        plan = self.plan()
        self.git("config", "remote.later.promisor", "true")
        self.fault("git_config_unavailable", lambda: worktrees.validate_creation(plan))
        worktrees.validate_creation_plan(plan)
        self.assertFalse(self.target.exists())

    def test_retained_plan_cannot_replace_fixed_git_command_or_locks(self):
        plan = self.plan()
        for updates in ({"argv": ["sh", "-c", "exit 0"]}, {"resource_keys": []},
                        {"branch": "-B"}, {"destination": str(self.work / ".git/unsafe")},
                        {"common_identity": [True, 1]}):
            with self.subTest(updates=updates):
                changed = deepcopy(plan)
                changed.update(updates)
                self.fault("invalid_creation_plan", lambda: worktrees.validate_creation_plan(changed))
        worktrees.validate_creation_plan(plan)
        self.assertFalse(self.target.exists())

    def test_result_verification_rejects_a_changed_created_head(self):
        plan = self.plan()
        self.run_plan(plan)
        subprocess.run(["git", "-C", str(self.target), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "Unexpected advance"],
                       check=True, capture_output=True)
        self.fault("context_mismatch", lambda: worktrees.verify_creation(plan))


if __name__ == "__main__":
    unittest.main()
