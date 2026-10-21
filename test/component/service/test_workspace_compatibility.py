"""Tests for service behavior at the component layer."""

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from openmachine import workspace
from openmachine.service import checks as service_checks
from openmachine.service import processes as service_processes
from test.support.workspace import WorkspaceFixture



class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
    def test_old_candidate_ignoring_new_store_is_rejected_by_actual_gate(self):
        self.apply()
        ignored = {"protocol": 1, "state_contract": 3}
        process = subprocess.CompletedProcess([], 0, json.dumps(ignored), "")
        with patch.object(service_processes, "run", return_value=process), patch.object(service_checks, "load", return_value=self.config):
            self.fault("incompatible_state", lambda: service_checks._candidate_state(Path(sys.executable), self.root / "config", self.state, 3))
        self.assertEqual(workspace.check_change_state(self.state)["workspace_change_records"], 1)


class WorkspaceReferenceTests(WorkspaceFixture, unittest.TestCase):
    def test_registered_worktree_blocks_removing_its_last_root_grant(self):
        self.initialize_git()
        self.git("worktree", "add", "--detach", str(self.root / "linked"))
        candidate = self.config.model_copy(deep=True)
        del candidate.workspaces["project"]
        self.fault("workspace_in_use", lambda: service_checks.workspace_bindings_safe(
            self.state, self.config, candidate, "other"))

    def test_unreadable_original_root_fails_closed_on_workspace_rebinding(self):
        self.apply()
        self.work.rename(self.root / "old")
        self.work.mkdir()
        candidate = self.config.model_copy(deep=True)
        del candidate.workspaces["project"]
        self.fault("workspace_reference_unavailable", lambda: workspace.preserve_change_routes(
            self.state, self.config, candidate, {("local", "project")}))

    def test_original_change_survives_safe_rename_but_blocks_root_replacement_or_removal(self):
        original = self.apply()
        change_id = original["change_id"]
        changed = {("local", "project")}
        for root in (None, self.root / "different"):
            candidate = self.config.model_copy(deep=True)
            if root is None:
                del candidate.workspaces["project"]
            else:
                root.mkdir()
                candidate.workspaces["project"].root = str(root)
            with self.subTest(root=root):
                self.fault("workspace_in_use", lambda: workspace.preserve_change_routes(
                    self.state, self.config, candidate, changed))
        renamed = self.config.model_copy(deep=True)
        renamed.workspaces["renamed"] = renamed.workspaces.pop("project")
        workspace.preserve_change_routes(self.state, self.config, renamed, changed)
        self.assertEqual(workspace.read_change(renamed, self.state, "local", change_id)["workspace"], "renamed")
        self.assertTrue(self.file.exists())


if __name__ == "__main__":
    unittest.main()
