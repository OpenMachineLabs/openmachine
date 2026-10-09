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


if __name__ == "__main__":
    unittest.main()
