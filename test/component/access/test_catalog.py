"""The folder catalog: stable bindings, fail-closed discovery and the access catalog command."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openmachine.access import catalog
from openmachine.access.__main__ import catalog_reply
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault

GIT = ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null")


class CatalogFixture(object):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-catalog-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "source.py").write_text("print('fixture')\n")
        self.config = Settings.model_validate({"machines": {"local": {"transport": "local"}}})

    def git(self, *args, cwd=None):
        subprocess.run(["git", *GIT, "-C", str(cwd or self.work), *args], check=True, capture_output=True)

    def initialize_git(self):
        self.git("init", "-q", "-b", "main")
        self.git("add", "source.py")
        self.git("commit", "-q", "-m", "Initial")

    def fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def target(self, path=".", **kwargs):
        return catalog.inspect(self.config, "local", str(self.work), "inspect", path, **kwargs)["project"]

    def listing(self, **kwargs):
        return catalog.inspect(self.config, "local", str(self.work), **kwargs)


class CatalogTests(CatalogFixture, unittest.TestCase):
    def test_folder_and_repository_have_distinct_stable_identities(self):
        first = self.target()
        self.assertEqual((first["kind"], first["name"], first["workspace_name"]), ("folder", "work", str(self.work)))
        self.initialize_git()
        repository = self.target()
        self.assertEqual(repository["kind"], "git_repo")
        self.assertNotEqual(repository["target_id"], first["target_id"])
        self.fault("project_changed", lambda: self.target(expected_target_id=first["target_id"]))

    def test_root_is_the_account_desktop_without_fallback(self):
        home = self.root / "home"
        desktop = home / "Desktop"
        desktop.mkdir(parents=True)
        root = lambda: catalog.inspect(self.config, "local", action="root")
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
        self.fault("invalid_project_request", lambda: catalog.inspect(self.config, "local"))
        self.fault("invalid_project_request", lambda: catalog.inspect(self.config, "local", str(self.work), "root"))

    def test_listing_is_metadata_only_and_nonrecursive(self):
        (self.work / "folder/deeper").mkdir(parents=True)
        (self.work / "repo").mkdir()
        subprocess.run(["git", "init", "-q", str(self.work / "repo")], check=True)
        for name in (".env", ".SSH", "credentials", "private.key"):
            (self.work / name).mkdir()
        (self.work / "link").symlink_to(self.root, target_is_directory=True)
        with patch.object(catalog, "context", side_effect=AssertionError("listing probed Git")):
            rows = self.listing()["directories"]
        self.assertEqual(rows, [{"relative_path": "folder", "name": "folder"}, {"relative_path": "repo", "name": "repo"}])
        self.assertEqual(self.target("repo")["kind"], "git_repo")

    def test_identity_survives_branch_commit_and_ordinary_directory_edits(self):
        self.initialize_git()
        before = self.target()["target_id"]
        self.git("checkout", "-q", "-b", "next")
        (self.work / "source.py").write_text("changed\n")
        self.git("commit", "-q", "-am", "Next")
        (self.work / "new-folder").mkdir()
        self.assertEqual(before, self.target(expected_target_id=before)["target_id"])
        self.git("checkout", "-q", "--detach")
        self.assertEqual(before, self.target()["target_id"])

    def test_unborn_repository_binding_survives_first_commit(self):
        self.git("init", "-q", "-b", "main")
        before = self.target()["target_id"]
        self.git("add", "source.py")
        self.git("commit", "-q", "-m", "Initial")
        self.assertEqual(before, self.target(expected_target_id=before)["target_id"])

    def test_linked_worktree_and_nested_repository_have_distinct_bindings(self):
        self.initialize_git()
        self.git("worktree", "add", "-q", "-b", "linked", str(self.work / "linked"))
        target = self.target("linked")
        self.assertEqual(target["kind"], "git_worktree")
        self.assertNotEqual(target["target_id"], self.target()["target_id"])
        self.git("checkout", "-q", "--detach", cwd=self.work / "linked")
        self.assertEqual(target, self.target("linked", expected_target_id=target["target_id"]))
        (self.work / "nested").mkdir()
        plain = self.target("nested")["target_id"]
        subprocess.run(["git", "init", "-q", str(self.work / "nested")], check=True)
        self.fault("project_changed", lambda: self.target("nested", expected_target_id=plain))
        self.assertEqual(self.target("nested")["kind"], "git_repo")

    def test_directory_ancestor_and_git_control_replacement_require_reselection(self):
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
        before = self.target()["target_id"]
        self.work.rename(self.root / "original")
        self.work.mkdir()
        self.fault("project_changed", lambda: self.target(expected_target_id=before))
        self.git("init", "-q")
        before = self.target()["target_id"]
        (self.work / ".git").rename(self.work / "old-git")
        self.git("init", "-q")
        self.fault("project_changed", lambda: self.target(expected_target_id=before))

    def test_selectors_are_canonical_absolute_directories_only(self):
        (self.root / "alias").symlink_to(self.work, target_is_directory=True)
        for selector in ("project", "work", str(self.root / "alias"), str(self.work) + "/../work", str(self.root / ".ssh")):
            with self.subTest(selector=selector):
                self.fault("path_denied", lambda: catalog.inspect(self.config, "local", selector, "inspect"))
        self.fault("unknown_machine", lambda: catalog.inspect(self.config, "remote", str(self.work), "inspect"))
        self.fault("source_unavailable", lambda: catalog.root_for(str(self.work / "source.py")))

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

    def test_git_failures_bare_repositories_and_control_links_fail_closed(self):
        (self.work / "broken").mkdir()
        (self.work / "broken/.git").write_text("gitdir: /missing\n")
        self.fault("project_unavailable", lambda: self.target("broken"))
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
        self.assertEqual((last["has_more"], last["next_offset"]), (False, None))
        (self.work / "b").mkdir()
        self.fault("source_changed", lambda: self.listing(offset=1, expected_snapshot=first["snapshot"]))
        self.fault("invalid_project_request", lambda: self.listing(offset=1))
        self.fault("invalid_range", lambda: self.listing(offset=100, expected_snapshot=self.listing()["snapshot"]))

    def test_scan_and_path_budgets(self):
        for name in (".env.one", ".env.two", "other.txt"):
            (self.work / name).touch()
        with patch.object(catalog, "MAX_SCAN_ENTRIES", 2):
            self.fault("catalog_limit", self.listing)
        self.fault("catalog_limit", lambda: self.target("/".join(["d"] * 33)))
        self.fault("catalog_limit", lambda: self.target("d" * 1025))

    def test_input_contract(self):
        for arguments in ({"limit": True}, {"limit": 0}, {"limit": 51}, {"offset": -1}, {"offset": True}):
            self.fault("invalid_range", lambda: self.listing(**arguments))
        for arguments in ({"action": "register"}, {"expected_snapshot": "bad"}, {"expected_target_id": "f" * 64}):
            self.fault("invalid_project_request", lambda: self.listing(**arguments))
        self.fault("invalid_project_request", lambda: catalog.inspect(self.config, "local", action="inspect"))

    def test_replacement_during_observation_returns_no_binding(self):
        (self.work / "selected").mkdir()
        real, replaced = catalog.context, []

        def race(path):
            result = real(path)
            if not replaced:
                replaced.append(path)
                path.rename(self.work / "displaced")
                path.mkdir()
            return result

        with patch.object(catalog, "context", side_effect=race):
            self.fault("project_changed", lambda: self.target("selected"))

    def test_catalog_does_not_run_repository_filters_or_hooks(self):
        self.initialize_git()
        sentinel, program = self.work / "unexpected", self.work / "hook.sh"
        program.write_text("#!/bin/sh\ntouch " + str(sentinel) + "\ncat\n")
        program.chmod(0o700)
        (self.work / ".gitattributes").write_text("*.py filter=fixture diff=fixture\n")
        for setting in ("filter.fixture.clean", "filter.fixture.process", "diff.fixture.command", "core.fsmonitor"):
            self.git("config", setting, str(program))
        self.target()
        self.listing()
        self.assertFalse(sentinel.exists())

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


class CatalogCommandTests(CatalogFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config_path = self.root / "config.toml"
        self.config_path.write_text('[machines.local]\ntransport = "local"\n')
        self.state = self.root / "state"

    def reply(self, request):
        raw = request if isinstance(request, bytes) else json.dumps(request).encode()
        return catalog_reply(self.config_path, self.state, raw)

    def binding(self, path, mode="full_access", relative="."):
        target = catalog.inspect(self.config, "local", str(path), "inspect", relative)["project"]["target_id"]
        return {"machine": "local", "workspace_name": str(path), "relative_path": relative, "target_id": target, "mode": mode}

    def test_root_list_and_inspect_answer_protocol_2(self):
        home = self.root / "home"
        (home / "Desktop").mkdir(parents=True)
        with (patch.object(catalog.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(home))),
              patch.object(catalog.sys, "platform", "darwin")):
            # A root request without machine, or a list without a directory, uses the one configured machine.
            for request in ({}, {"action": "root"}, {"action": "list"}, {"action": "root", "machine": "local"}):
                with self.subTest(request=request):
                    self.assertEqual(self.reply(request)["root"]["workspace_name"], str(home / "Desktop"))
        (self.work / "child").mkdir()
        listed = self.reply({"machine": "local", "workspace_name": str(self.work)})
        self.assertEqual(listed["directories"], [{"relative_path": "child", "name": "child"}])
        inspected = self.reply({"action": "inspect", "machine": "local", "workspace_name": str(self.work), "path": "child"})
        self.assertEqual(inspected, {"protocol": 2, "project": self.target("child")})

    def test_validate_echoes_bindings_after_identity_and_shape_rules(self):
        (self.work / "full").mkdir()
        (self.work / "read").mkdir()
        projects = [self.binding(self.work / "full"), self.binding(self.work / "read", "read_only")]
        self.assertEqual(self.reply({"action": "validate", "projects": projects}), {"protocol": 2, "validated": projects})
        self.assertEqual(self.reply({"action": "validate", "projects": []}), {"protocol": 2, "validated": []})
        changed = projects[0] | {"target_id": "0" * 64}
        nested = [self.binding(self.work, "full_access"), self.binding(self.work / "read", "read_only")]
        other = projects[0] | {"machine": "remote"}
        for bindings in ([changed], nested, [other], [projects[0], projects[0]]):
            with self.subTest(bindings=bindings):
                self.assertEqual(self.reply({"action": "validate", "projects": bindings}), {"error": {"code": "target_unverified"}})

    def test_validate_refuses_a_git_target_below_a_root(self):
        (self.work / "repo").mkdir()
        self.git("init", "-q", cwd=self.work / "repo")
        below = self.binding(self.work, relative="repo")
        self.assertEqual(self.reply({"action": "validate", "projects": [below]}), {"error": {"code": "target_unverified"}})
        (self.work / "repo/file.txt").write_text("fixture\n")
        self.git("add", "file.txt", cwd=self.work / "repo")
        self.git("commit", "-q", "-m", "Initial", cwd=self.work / "repo")
        self.git("worktree", "add", "-q", "-b", "linked", str(self.work / "linked"), cwd=self.work / "repo")
        linked = self.binding(self.work, relative="linked")
        self.assertEqual(self.reply({"action": "validate", "projects": [linked]}), {"error": {"code": "target_unverified"}})
        # The echo is the request's own bytes order, which Cloud compares as JSON text.
        own = dict(reversed(self.binding(self.work / "repo").items()))
        self.assertEqual(json.dumps(self.reply({"action": "validate", "projects": [own]})),
                         json.dumps({"protocol": 2, "validated": [own]}))
        (self.work / "folder").mkdir()
        folder = self.binding(self.work, relative="folder")
        self.assertEqual(self.reply({"action": "validate", "projects": [folder]}), {"protocol": 2, "validated": [folder]})
        # A binding overlapping the state directory Core was given is refused, as sessions refuse it.
        (self.state / "selected").mkdir(parents=True)
        inside = self.binding(self.state / "selected")
        self.assertEqual(self.reply({"action": "validate", "projects": [inside]}), {"error": {"code": "target_unverified"}})

    def test_error_codes_map_to_the_owner_actionable_set(self):
        passed = ["desktop_missing", "desktop_redirected", "desktop_access_denied", "desktop_unsupported", "directory_access_denied"]
        cases = {**{code: code for code in passed}, "project_changed": "target_unverified",
                 "project_unavailable": "target_unverified", "scope_conflict": "target_unverified",
                 "scope_too_broad": "target_unverified", "path_denied": "catalog_unavailable",
                 "source_changed": "catalog_unavailable", "unknown_machine": "catalog_unavailable"}
        for code, expected in cases.items():
            with self.subTest(code=code), patch.object(catalog, "inspect", side_effect=Fault(code, "fixture")):
                self.assertEqual(self.reply({"action": "root"}), {"error": {"code": expected}})

    def test_invalid_requests_configuration_and_the_64_kib_bounds(self):
        for raw in (b"", b"\xff", b"[]", b'{"action": "validate"}', b'{"action": "validate", "projects": [], "x": 1}',
                    b'{"unexpected": 1}', b'{"offset": "1"}', b"[" * 60000,
                    b'{"action": "inspect", "machine": "local", "workspace_name": "/", "offset": "0"}',
                    json.dumps({"action": "validate", "projects": [{}] * 33}).encode(), b" " * 65537):
            with self.subTest(raw=raw[:40]):
                self.assertEqual(self.reply(raw), {"error": {"code": "catalog_unavailable"}})
        padded = json.dumps({"action": "inspect", "machine": "local", "workspace_name": str(self.work)}).encode()
        self.assertIn("project", self.reply(padded + b" " * (65536 - len(padded))))
        self.assertEqual(self.reply(padded + b" " * (65537 - len(padded))), {"error": {"code": "catalog_unavailable"}})
        # 33 valid bindings exceed the bound before any is inspected.
        (self.work / "one").mkdir()
        one = self.binding(self.work / "one")
        with patch.object(catalog, "inspect", side_effect=AssertionError("inspected")):
            self.assertEqual(self.reply({"action": "validate", "projects": [one] * 33}), {"error": {"code": "catalog_unavailable"}})
        self.config_path.write_text('schema_version = 1\n[machines.local]\ntransport = "local"\n')
        self.assertEqual(self.reply({}), {"error": {"code": "catalog_unavailable"}})
        self.config_path.write_text('[machines.local]\ntransport = "local"\n')
        # The printed reply, newline included, stays within 64 KiB.
        for size, expected in ((65499, "directories"), (65500, "error")):
            with self.subTest(size=size), patch.object(catalog, "inspect", return_value={"protocol": 2, "directories": ["x" * size]}):
                reply = self.reply({"action": "root"})
                self.assertIn(expected, reply)
                if expected == "directories":
                    self.assertEqual(len(json.dumps(reply).encode()) + 1, 65536)
        with patch.object(catalog, "inspect", side_effect=FileNotFoundError):
            self.assertEqual(self.reply({"action": "root"}), {"error": {"code": "catalog_unavailable"}})

    def test_launcher_reads_stdin_and_exits_zero_for_results_and_errors(self):
        for request, key in (({"action": "inspect", "machine": "local", "workspace_name": str(self.work)}, "project"),
                             ({"action": "inspect", "machine": "local", "workspace_name": "relative"}, "error")):
            result = subprocess.run([sys.executable, "-I", "-m", "openmachine.access", "--config", str(self.config_path),
                                     "--state-dir", str(self.state), "catalog"], input=json.dumps(request).encode(),
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(key, json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
