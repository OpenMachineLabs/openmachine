"""The manage-only CLI routes typed inputs to management and reports failure statuses by exit code."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from openmachine import cli

BASE = ["openmachine", "--config", "/c/config.toml", "--state-dir", "/s/state", "manage"]


class ManagementCliTests(unittest.TestCase):
    def run_cli(self, argv, status="accepted"):
        result = {"status": status}
        with patch.object(cli, "dispatch", return_value=result) as dispatch, patch.object(sys, "argv", BASE + argv), \
                redirect_stdout(io.StringIO()) as output:
            code = cli.main()
        self.assertEqual(json.loads(output.getvalue()), result)
        return code, dispatch

    def test_each_action_routes_exactly_its_inputs(self):
        payload = ["--operation-id", "a" * 32, "--payload", "/p/payload", "--sha256", "b" * 64]
        cases = (
            (["status"], "status", {}),
            (["configure"], "configure", {"verify_existing": False}),
            (["configure", "--verify-existing"], "configure", {"verify_existing": True}),
            (["install", *payload], "install", {"identity": "a" * 32, "source": Path("/p/payload"), "sha256": "b" * 64}),
            (["update", *payload, "--expected-selection-sha256", "e" * 64], "update",
             {"identity": "a" * 32, "source": Path("/p/payload"), "sha256": "b" * 64, "expected_selection_sha256": "e" * 64}),
            (["operation", "--operation-id", "c" * 32], "operation", {"identity": "c" * 32}),
            (["continue", "--operation-id", "c" * 32], "continue", {"identity": "c" * 32}),
            (["--contract", "2", "status"], "status", {}),
        )
        for argv, action, inputs in cases:
            with self.subTest(argv=argv):
                code, dispatch = self.run_cli(argv)
                self.assertEqual(code, 0)
                dispatch.assert_called_once_with(Path("/c/config.toml"), Path("/s/state"), action,
                                                 contract=2 if "--contract" in argv else 1, **inputs)

    def test_failure_statuses_exit_nonzero(self):
        for status in ("unknown", "incompatible", "busy", "rejected", "recovery_required"):
            with self.subTest(status=status):
                self.assertEqual(self.run_cli(["status"], status)[0], 1)
        for status in ("installed", "not_installed", "configured", "accepted", "completed", "continuation_required"):
            with self.subTest(status=status):
                self.assertEqual(self.run_cli(["status"], status)[0], 0)

    def test_retired_commands_and_missing_inputs_fail_before_dispatch(self):
        payload = ["--operation-id", "a" * 32, "--payload", "/p", "--sha256", "b" * 64]
        for argv in (BASE + ["update", *payload], BASE + ["bind", "--identity", "a" * 64], BASE + ["start"],
                     BASE + ["install", *payload, "--probe-workspace", "/w"], ["openmachine", "serve"],
                     ["openmachine", "manage", "status"]):
            with self.subTest(argv=argv), patch.object(cli, "dispatch") as dispatch, patch.object(sys, "argv", argv), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
                cli.main()
            self.assertEqual(exit.exception.code, 2)
            dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
