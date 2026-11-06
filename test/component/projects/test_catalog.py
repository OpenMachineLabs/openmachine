"""Local project metadata, stable bindings and fail-closed discovery."""

import json
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openmachine.config import Machine, Workspace
from openmachine.projects import catalog
from test.support.workspace import WorkspaceFixture


class ProjectCatalogTests(WorkspaceFixture, unittest.TestCase):
    def test_nonconfigured_repository_and_folder_have_native_stable_identities(self):
        self.config.workspaces.clear()
        self.config.machines["local"].local_access = "read_only"
        first = self.app.inspect_projects("local", str(self.work), "inspect")["project"]
        self.assertEqual(first["kind"], "folder")
        self.initialize_git()
        repo = self.app.inspect_projects("local", str(self.work), "inspect")["project"]
        self.assertEqual(repo["kind"], "git_repo")
        self.assertNotEqual(repo["target_id"], first["target_id"])
        self.fault("project_changed", lambda: self.app.inspect_projects("local", str(self.work), "inspect", expected_target_id=first["target_id"]))
        self.assertEqual(self.config.workspaces, {})

    def target(self, path=".", **kwargs):
        return self.app.inspect_projects("local", "project", "inspect", path, **kwargs)["project"]

    def listing(self, **kwargs):
        return self.app.inspect_projects("local", "project", **kwargs)

    def test_root_is_the_account_desktop_without_fallback(self):
        home = self.root / "home"
        desktop = home / "Desktop"
        desktop.mkdir(parents=True)
        root = lambda: self.app.inspect_projects("local", action="root")
        with (patch.object(catalog.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(home))),
              patch.object(catalog.sys, "platform", "darwin"), patch.dict(os.environ, {"HOME": str(self.work)})):
            self.assertEqual(root(), {"protocol": 2, "root": {"machine": "local", "workspace_name": str(desktop),
                                                              "relative_path": ".", "name": "Desktop"}})
            desktop.rename(home / "Moved")
            desktop.symlink_to(home / "Moved", target_is_directory=True)
            self.fault("desktop_redirected", root)
            desktop.unlink()
            self.fault("desktop_missing", root)
            desktop.write_text("")
            self.fault("desktop_redirected", root)
            with patch.object(catalog.os, "lstat", side_effect=PermissionError):
                self.fault("desktop_access_denied", root)
        with patch.object(catalog.sys, "platform", "linux"):
            self.fault("desktop_unsupported", root)
        # Configured roots, including installation probes, are never an implicit listing.
        self.fault("invalid_project_request", lambda: self.app.inspect_projects("local"))
        self.fault("invalid_project_request", lambda: self.app.inspect_projects("local", "project", "root"))

    def test_listing_is_metadata_only_nonrecursive_and_preserves_local_authority(self):
        (self.work / "folder/deeper").mkdir(parents=True)
        (self.work / "repo").mkdir()
        subprocess.run(["git", "init", "-q", str(self.work / "repo")], check=True)
        for name in (".env", ".SSH", "credentials", "private.key"):
            (self.work / name).mkdir()
        (self.work / "link").symlink_to(self.root, target_is_directory=True)
        self.config.machines["local"].execution_enabled = False
        self.config.machines["local"].workspace_write_enabled = False
        before = self.config.model_dump()
        with patch.object(catalog, "context", side_effect=AssertionError("listing probed Git")):
            rows = self.listing()["directories"]
        self.assertEqual(rows, [{"relative_path": "folder", "name": "folder"}, {"relative_path": "repo", "name": "repo"}])
        self.assertEqual(self.target("repo")["kind"], "git_repo")
        self.assertNotIn("answer =", json.dumps(rows))
        self.assertEqual(self.config.model_dump(), before)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.app.get_capabilities()["project_catalog"]["actions"], ["root", "list", "inspect"])
        self.assertEqual(self.app.read_workspace_file("local", "project", "source.py")["text"], self.file.read_bytes().decode())
        self.fault("workspace_write_disabled", self.apply)

    def test_identity_survives_branch_commit_and_ordinary_directory_edits(self):
        self.initialize_git()
        before = self.target()["target_id"]
        snapshot = self.app.read_workspace_file("local", "project", "source.py")["context"]["workspace_identity"]
        self.git("checkout", "-q", "-b", "next")
        self.assertNotEqual(snapshot, self.app.read_workspace_file("local", "project", "source.py")["context"]["workspace_identity"])
        self.file.write_text("changed\n")
        self.git("add", "source.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "Next")
        (self.work / "new-folder").mkdir()
        self.assertEqual(before, self.target(expected_target_id=before)["target_id"])
        self.git("checkout", "-q", "--detach")
        self.assertEqual(before, self.target()["target_id"])

    def test_unborn_repository_binding_survives_first_commit(self):
        self.git("init", "-q", "-b", "main")
        before = self.target()["target_id"]
        self.initialize_git()
        self.assertEqual(before, self.target(expected_target_id=before)["target_id"])

    def test_linked_worktree_and_nested_repository_have_distinct_bindings(self):
        self.initialize_git()
        self.git("worktree", "add", "-q", "-b", "linked", str(self.work / "linked"))
        target = self.target("linked")
        self.assertEqual(target["kind"], "git_worktree")
        self.assertNotEqual(target["target_id"], self.target()["target_id"])
        subprocess.run(["git", "-C", str(self.work / "linked"), "checkout", "-q", "--detach"], check=True)
        self.assertEqual(target, self.target("linked", expected_target_id=target["target_id"]))
        (self.work / "nested").mkdir()
        plain = self.target("nested")["target_id"]
        subprocess.run(["git", "init", "-q", str(self.work / "nested")], check=True)
        self.fault("project_changed", lambda: self.target("nested", expected_target_id=plain))
        self.assertEqual(self.target("nested")["kind"], "git_repo")

    def test_same_path_directory_and_ancestor_replacement_require_reselection(self):
        (self.work / "parent/child").mkdir(parents=True)
        original = self.target("parent/child")["target_id"]
        (self.work / "parent").rename(self.work / "old")
        (self.work / "parent").mkdir()
        (self.work / "old/child").rename(self.work / "parent/child")
        self.fault("project_changed", lambda: self.target("parent/child", expected_target_id=original))
        original = self.target("parent/child")["target_id"]
        (self.work / "parent/child").rename(self.work / "displaced")
        (self.work / "parent/child").mkdir()
        self.fault("project_changed", lambda: self.target("parent/child", expected_target_id=original))
        self.fault("project_changed", lambda: self.target("displaced", expected_target_id=original))

    def test_workspace_retarget_and_git_control_replacement_invalidate_bindings(self):
        before = self.target()["target_id"]
        self.work.rename(self.root / "original")
        self.work.mkdir()
        self.fault("project_changed", lambda: self.target(expected_target_id=before))
        self.git("init", "-q")
        before = self.target()["target_id"]
        (self.work / ".git").rename(self.work / "old-git")
        self.git("init", "-q")
        self.fault("project_changed", lambda: self.target(expected_target_id=before))
        self.config.workspaces["project"].root = str(self.root / "original")
        self.fault("project_changed", lambda: self.target(expected_target_id=before))

    def test_path_escape_links_and_credential_paths_are_rejected(self):
        (self.work / "link").symlink_to(self.root, target_is_directory=True)
        (self.work / "inside").mkdir()
        (self.work / "alias").symlink_to(self.work / "inside", target_is_directory=True)
        for path in ("../outside", str(self.root), "a/../b", "a//b", "a/./b", ".git", ".SSH", "x/.env", "x\\y", "bad\nname", "\ud800"):
            with self.subTest(path=path):
                self.fault("path_denied", lambda: self.target(path))
        for path in ("link", "link/anything", "alias", "missing", "source.py"):
            with self.subTest(path=path):
                self.fault("project_unavailable", lambda: self.target(path))

    def test_git_failures_bare_repositories_and_control_symlinks_fail_closed(self):
        (self.work / "broken").mkdir()
        (self.work / "broken/.git").write_text("gitdir: /missing\n")
        self.fault("project_unavailable", lambda: self.target("broken"))
        # Listing never inspects a child's Git control file; selection does.
        self.assertEqual(self.listing()["directories"], [{"relative_path": "broken", "name": "broken"}])
        subprocess.run(["git", "init", "--bare", "-q", str(self.work / "bare")], check=True)
        self.fault("project_unavailable", lambda: self.target("bare"))
        self.initialize_git()
        with patch.dict(os.environ, {"PATH": ""}):
            self.fault("project_unavailable", self.target)
        (self.work / ".git").rename(self.root / "git-dir")
        (self.work / ".git").symlink_to(self.root / "git-dir", target_is_directory=True)
        self.fault("project_unavailable", self.target)

    def test_pagination_is_sorted_bound_and_rejects_changed_directories(self):
        for name in ("z", "a", "m"):
            (self.work / name).mkdir()
        first = self.listing(limit=1)
        second = self.listing(offset=first["next_offset"], limit=1, expected_snapshot=first["snapshot"])
        last = self.listing(offset=second["next_offset"], expected_snapshot=second["snapshot"])
        self.assertEqual([page["directories"][0]["name"] for page in (first, second, last)], ["a", "m", "z"])
        self.assertFalse(last["has_more"])
        self.assertIsNone(last["next_offset"])
        (self.work / "b").mkdir()
        self.fault("source_changed", lambda: self.listing(offset=1, expected_snapshot=first["snapshot"]))
        self.fault("invalid_project_request", lambda: self.listing(offset=1))
        self.fault("invalid_range", lambda: self.listing(offset=100, expected_snapshot=self.listing()["snapshot"]))

    def test_scan_and_response_budgets_include_hidden_and_non_directory_entries(self):
        for name in (".env.one", ".env.two", "other.txt"):
            (self.work / name).touch()
        with patch.object(catalog, "MAX_SCAN_ENTRIES", 2):
            self.fault("catalog_limit", self.listing)
        with patch.object(catalog, "MAX_RESULT", 10):
            self.fault("catalog_limit", self.target)
        self.fault("catalog_limit", lambda: self.target("/".join(["d"] * 33)))
        self.fault("catalog_limit", lambda: self.target("d" * 1025))

    def test_input_contract_rejects_non_native_target_without_local_fallback(self):
        from pydantic import ValidationError
        for arguments in ({"limit": True}, {"limit": 0}, {"limit": 51}, {"offset": -1}, {"offset": True}):
            self.fault("invalid_range", lambda: self.listing(**arguments))
        for arguments in ({"action": "register"}, {"expected_snapshot": "bad"}, {"expected_target_id": "f" * 64}):
            self.fault("invalid_project_request", lambda: self.listing(**arguments))
        self.fault("invalid_project_request", lambda: self.app.inspect_projects("local", action="inspect"))
        self.fault("unknown_workspace", lambda: self.app.inspect_projects("local", "absent"))
        with self.assertRaises(ValidationError):
            Machine(transport="ssh", ssh_alias="legacy-target")
        with patch.object(catalog, "root_for", side_effect=AssertionError("controller fallback")):
            self.fault("unknown_machine", lambda: self.app.inspect_projects("retired-remote", "retired-remote"))

    def test_replacement_during_observation_returns_no_binding(self):
        (self.work / "selected").mkdir()
        real = catalog.context
        replaced = False

        def race(path):
            nonlocal replaced
            result = real(path)
            if not replaced:
                replaced = True
                path.rename(self.work / "displaced")
                path.mkdir()
            return result

        with patch.object(catalog, "context", side_effect=race):
            self.fault("project_changed", lambda: self.target("selected"))

    def test_catalog_does_not_run_repository_filters_or_hooks(self):
        self.initialize_git()
        sentinel = self.work / "unexpected"
        program = self.work / "hook.sh"
        program.write_text("#!/bin/sh\ntouch " + str(sentinel) + "\ncat\n")
        program.chmod(0o700)
        (self.work / ".gitattributes").write_text("*.py filter=fixture diff=fixture\n")
        for setting in ("filter.fixture.clean", "filter.fixture.process", "diff.fixture.command", "core.fsmonitor"):
            self.git("config", setting, str(program))
        self.target()
        self.listing()
        self.assertFalse(sentinel.exists())

    def test_configured_aliases_and_unrelated_configuration_do_not_confuse_identity(self):
        before = self.target()["target_id"]
        self.config.workspaces["alias"] = Workspace(machine="local", root=str(self.work))
        self.config.machines["local"].execution_enabled = False
        self.assertEqual(before, self.target(expected_target_id=before)["target_id"])
        alias = self.app.inspect_projects("local", "alias", "inspect")["project"]
        self.assertNotEqual(before, alias["target_id"])
        approved = self.root / "approved"
        approved.symlink_to(self.work, target_is_directory=True)
        self.config.workspaces["project"].root = str(approved)
        selected = self.target()["target_id"]
        (self.root / "different").mkdir()
        approved.unlink()
        approved.symlink_to(self.root / "different", target_is_directory=True)
        self.fault("project_changed", lambda: self.target(expected_target_id=selected))

    def test_worktree_control_file_replacement_and_hardlinks_fail_closed(self):
        self.initialize_git()
        self.git("worktree", "add", "-q", "-b", "linked", str(self.work / "linked"))
        original = self.target("linked")["target_id"]
        marker = self.work / "linked/.git"
        raw = marker.read_bytes()
        marker.rename(self.work / "saved-marker")
        marker.write_bytes(raw)
        self.fault("project_changed", lambda: self.target("linked", expected_target_id=original))
        os.link(marker, self.work / "hardlink")
        self.fault("project_unavailable", lambda: self.target("linked"))
