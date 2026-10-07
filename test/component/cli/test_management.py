"""Tests for cli behavior at the component layer."""

from contextlib import redirect_stdout
import io
import json
import sys
import unittest
from unittest.mock import patch
from openmachine.service import maintenance
from openmachine.service import runtime as service_runtime
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_manage_retire_routes_original_operation_id(self):
        from openmachine.cli import main
        argv = ["openmachine", "--config", str(self.config), "--state-dir", str(self.state), "manage",
                "--contract", "1", "retire", "--operation-id", "c" * 32]
        result = {"status": "retired", "operation": {"operation_id": "c" * 32}}
        with patch.object(maintenance, "dispatch", return_value=result) as dispatch, \
                patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        dispatch.assert_called_once_with(self.config, self.state, "retire", contract=1, identity="c" * 32)

    def test_manage_configure_accepts_complete_recovery_selections_and_rejects_partial_input(self):
        from openmachine.cli import main
        from openmachine.config import load
        path, state = self.root / "first.toml", self.root / "first-state"
        argv = ["openmachine", "--config", str(path), "--state-dir", str(state), "manage",
                "--contract", "1", "configure", "--workspace", str(self.root),
                "--recovery-connection", "OM", "--browser-account", "operator",
                "--browser-workspace", "Personal", "--recovery-model", "selected-model"]
        self.enterContext(patch.object(maintenance.sys, "platform", "darwin"))
        self.enterContext(patch.object(service_runtime, "_native", return_value={"loaded": False}))
        for invalid in (argv[:-2], [*argv[:-1], "bad\x7flabel"]):
            with patch.object(sys, "argv", invalid), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(), 1)
            self.assertEqual(json.loads(output.getvalue())["reason"], "invalid_config")
            self.assertFalse(path.exists())
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertIn("configure:chat_recovery", result["capabilities"])
        self.assertEqual(load(path).chat_recovery.workspace, "project")
        self.assertEqual(load(path).chat_recovery.connection, "OM")
        self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
