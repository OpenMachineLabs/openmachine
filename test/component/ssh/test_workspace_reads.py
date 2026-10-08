"""Tests for ssh behavior at the component layer."""

from copy import deepcopy
import unittest
from unittest.mock import Mock, patch
import uuid
from integrations.ssh import PROTOCOL, client, endpoint
from openmachine import __version__, workspace
from openmachine.app.machines import Machines
from openmachine.config import Settings
from openmachine.workspace.git import context
from test.support.workspace import WorkspaceFixture



class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
    def test_target_line_and_filter_arguments_share_read_only_domain_code(self):
        self.file.write_bytes(b"padding\n" * 20000 + b"answer = 41\n")
        identity = "e" * 64
        archive = self.root / ("a" * 64 + ".pyz")
        request = {"protocol": PROTOCOL, "version": __version__, "identity": identity,
                   "execution_enabled": False, "workspace_write_enabled": False,
                   "workspaces": {"project": str(self.work)}, "operation": "read_workspace_file",
                   "arguments": {"workspace": "project", "path": "source.py", "start_line": 20001, "line_count": 1}}
        with patch.object(endpoint, "_identity", return_value=(identity, self.root)):
            response = endpoint._dispatch(request, archive)
            self.assertEqual(response["result"]["text"], "answer = 41\n")
            self.assertGreater(response["result"]["offset"], 131072)
            response = endpoint._dispatch(request | {"operation": "inspect_workspace",
                "arguments": {"workspace": "project", "action": "search", "query": "answer", "path_glob": "*.py"}}, archive)
            self.assertEqual(response["result"]["matches"][0]["line"], 20001)
            self.fault("protocol_mismatch", lambda: endpoint._dispatch(request | {"protocol": 4}, archive))
            self.fault("invalid_request", lambda: endpoint._dispatch(request | {
                "arguments": request["arguments"] | {"execute": True}}, archive))
        self.assertFalse((self.root / ".local").exists())


class WorktreeRouteTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.initialize_git()
        self.target = self.root / "remote-tree"
        self.git("-c", "core.hooksPath=/dev/null", "worktree", "add", "--quiet", "-b", "remote-task", str(self.target), "HEAD")
        self.identity = "e" * 64
        self.archive = self.root / ("a" * 64 + ".pyz")
        self.request = {"protocol": PROTOCOL, "version": __version__, "identity": self.identity,
                        "execution_enabled": False, "workspace_write_enabled": True,
                        "workspaces": {"project": str(self.work), "trees": str(self.root)}}

    def dispatch(self, operation, arguments):
        request = self.request | {"operation": operation, "arguments": arguments}
        original = deepcopy(request)
        with patch.object(endpoint, "_identity", return_value=(self.identity, self.root)):
            result = endpoint._dispatch(request, self.archive)["result"]
        self.assertEqual(request, original, "Routing must retain the caller's original worktree selector.")
        return result

    def read_target(self):
        return self.dispatch("read_workspace_file", {"workspace": "project", "worktree": str(self.target), "path": "source.py"})

    def test_target_worktree_reads_edits_writes_patches_and_original_receipts_share_one_root(self):
        original = self.file.read_bytes()
        read = self.read_target()
        self.assertEqual(read["context"]["cwd"], str(self.target))
        expected = {"expected_workspace_identity": read["context"]["workspace_identity"],
                    "expected_head_sha": read["context"]["head_sha"]}
        selection = {"workspace": "project", "worktree": str(self.target), "path": "source.py"}
        edit = selection | expected | {"edits": [{"old_text": "41", "new_text": "42"}],
            "expected_sha256": read["sha256"], "change_id": uuid.uuid4().hex}
        changed = self.dispatch("edit_workspace_file", edit)
        replay = self.dispatch("edit_workspace_file", edit)
        self.assertEqual(changed["status"], "applied")
        self.assertEqual(replay["created_at"], changed["created_at"])
        read = self.read_target()
        self.dispatch("write_workspace_file", selection | expected | {"content": "answer = 43\n",
            "expected_sha256": read["sha256"], "change_id": uuid.uuid4().hex})
        read = self.read_target()
        patch_id = uuid.uuid4().hex
        applied = self.dispatch("apply_workspace_patch", expected | {"workspace": "project", "worktree": str(self.target),
            "patch_text": "*** Begin Patch\n*** Update File: source.py\n@@\n-answer = 43\n+answer = 44\n*** End Patch\n",
            "preconditions": [{"path": "source.py", "expected_sha256": read["sha256"]}], "change_id": patch_id})
        self.assertEqual(applied["status"], "applied")
        self.assertEqual((self.target / "source.py").read_text(), "answer = 44\n")
        self.assertEqual(self.file.read_bytes(), original)
        receipt = self.dispatch("read_workspace_change", {"change_id": patch_id, "observe_current": True})
        self.assertEqual(receipt["context"]["root"], str(self.target))
        selected = self.dispatch("inspect_workspace", {"workspace": "project", "worktree": str(self.target), "action": "status"})
        self.assertEqual(selected["context"]["worktree"], str(self.target))
        listed = self.dispatch("inspect_workspace", {"workspace": "project", "action": "worktrees"})
        self.assertIn(str(self.target), [item["worktree"] for item in listed["entries"]])

    def test_remote_config_selection_keeps_original_grants_and_rejects_outside_paths(self):
        config = endpoint._RemoteConfig(self.identity, self.request["workspaces"], False, True)
        selected = workspace.select_worktree(config, self.identity, "project", str(self.target))
        self.assertEqual(config.workspaces["project"].root, str(self.work))
        self.assertEqual(selected.workspaces["project"].root, str(self.target))
        self.request["workspaces"] = {"project": str(self.work)}
        self.fault("path_denied", self.read_target)
        self.request["workspaces"]["trees"] = str(self.root)
        self.request["workspace_write_enabled"] = False
        observed = self.read_target()
        self.fault("workspace_write_disabled", lambda: self.dispatch("edit_workspace_file", {
            "workspace": "project", "worktree": str(self.target), "path": "source.py",
            "edits": [{"old_text": "41", "new_text": "42"}], "change_id": uuid.uuid4().hex,
            "expected_workspace_identity": observed["context"]["workspace_identity"],
            "expected_head_sha": observed["context"]["head_sha"], "expected_sha256": observed["sha256"]}))

    def test_creation_dispatch_uses_target_identity_and_original_worker_archive(self):
        arguments = {"workspace_name": "project", "destination": str(self.root / "new-remote-tree"),
                     "base_ref": "HEAD", "new_branch": "new-remote-task", "execution_id": uuid.uuid4().hex,
                     "expected_workspace_identity": context(self.work)["workspace_identity"], "worktree": "", "task_id": ""}
        self.request["execution_enabled"] = True
        result = {"execution_id": arguments["execution_id"], "status": "accepted"}
        with patch.object(endpoint, "_scope_ready", return_value="255") as readiness, \
             patch.object(endpoint.execution, "create_worktree", return_value=result) as create:
            self.assertEqual(self.dispatch("create_worktree", arguments), result)
            readiness.assert_not_called()
        config, state, identity = create.call_args.args
        self.assertIsInstance(config, endpoint._RemoteConfig)
        self.assertEqual(identity, self.identity)
        self.assertEqual(state, self.root / ".local/state/openmachine")
        self.assertEqual(create.call_args.kwargs, arguments | {"remote_worker": self.archive, "validate_new": readiness})
        with patch.object(endpoint, "_identity", return_value=(self.identity, self.root)), \
             patch.object(endpoint.execution, "create_worktree") as create:
            self.fault("protocol_mismatch", lambda: endpoint._dispatch(self.request | {"protocol": PROTOCOL - 1,
                "operation": "create_worktree", "arguments": arguments}, self.archive))
            self.fault("invalid_request", lambda: endpoint._dispatch(self.request | {"operation": "create_worktree",
                "arguments": arguments | {"argv": ["sh"]}}, self.archive))
        create.assert_not_called()

    def test_controller_forwards_selectors_without_resolving_target_paths(self):
        config = Settings.model_validate({"schema_version": 1,
            "machines": {"ssh": {"transport": "ssh", "ssh_alias": "fixture", "ssh_worker": str(self.archive),
                                  "ssh_identity": self.identity, "execution_enabled": True}},
            "workspaces": {"project": {"machine": "ssh", "root": "/remote-only/project"},
                           "trees": {"machine": "ssh", "root": "/remote-only/trees"}}})
        exchange = Mock(return_value={"status": "routed"})
        machines = Machines(config, self.state, exchange)
        selected = "/remote-only/trees/isolated"
        result = machines.read_workspace_file("ssh", "project", "source.py", worktree=selected)
        self.assertEqual(result["status"], "routed")
        self.assertEqual(exchange.call_args.kwargs["worktree"], selected)
        machines.inspect_workspace("ssh", "project", "worktrees", worktree=selected)
        self.assertEqual(exchange.call_args.kwargs["worktree"], selected)
        machines.write_workspace_file("ssh", "project", "source.py", "next", "a" * 40, "b" * 64,
                                      expected_workspace_identity="c" * 64, worktree=selected)
        self.assertEqual(exchange.call_args.kwargs["worktree"], selected)
        machines.edit_workspace_file("ssh", "project", "source.py", [{"old_text": "old", "new_text": "new"}],
                                     "c" * 64, "a" * 40, "b" * 64, worktree=selected)
        self.assertEqual(exchange.call_args.kwargs["worktree"], selected)
        machines.apply_workspace_patch("ssh", "project", "patch", "c" * 64, "a" * 40, [], worktree=selected)
        self.assertEqual(exchange.call_args.kwargs["worktree"], selected)
        create = {"workspace_name": "project", "destination": "/remote-only/trees/new", "base_ref": "HEAD",
                  "new_branch": "new-task", "execution_id": uuid.uuid4().hex, "expected_workspace_identity": "c" * 64,
                  "worktree": selected}
        machines.create_worktree("ssh", **create)
        self.assertEqual(exchange.call_args.args[2], "create_worktree")
        self.assertEqual(exchange.call_args.kwargs["destination"], create["destination"])
        with patch.object(client, "_exchange", side_effect=TimeoutError) as timed_out:
            self.fault("outcome_unknown", lambda: client.call(config, "ssh", "create_worktree", **create))
        self.assertEqual(timed_out.call_count, 1)


if __name__ == "__main__":
    unittest.main()
