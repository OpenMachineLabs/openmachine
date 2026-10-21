"""Tests for cli behavior at the component layer."""

from contextlib import redirect_stderr, redirect_stdout
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

    def test_manage_update_routes_paired_configuration_consent_and_preserves_legacy_calls(self):
        from openmachine.cli import main
        for guarded in ("legacy", "config", "selection"):
            with self.subTest(guarded=guarded):
                source, candidate = self.root / "payload", self.root / "candidate.toml"
                argv = ["openmachine", "--config", str(self.config), "--state-dir", str(self.state), "manage", "update",
                        "--operation-id", "a" * 32, "--payload", str(source), "--sha256", "b" * 64,
                        "--candidate-config", str(candidate)]
                consent = {}
                if guarded != "legacy":
                    argv += ["--config-sha256", "c" * 64, "--candidate-sha256", "d" * 64]
                    consent = {"expected_config_sha256": "c" * 64, "expected_candidate_sha256": "d" * 64}
                if guarded == "selection":
                    argv += ["--expected-selection-sha256", "e" * 64]
                    consent["expected_selection_sha256"] = "e" * 64
                with patch.object(maintenance, "dispatch", return_value={"status": "accepted"}) as dispatch, \
                        patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), 0)
                dispatch.assert_called_once_with(self.config, self.state, "update", contract=1, identity="a" * 32,
                    source=source, sha256="b" * 64, candidate_config=candidate, **consent)

    def test_manage_update_rejects_unpaired_selection_before_dispatch(self):
        from openmachine.cli import main
        argv = ["openmachine", "--config", str(self.config), "--state-dir", str(self.state), "manage", "update",
                "--operation-id", "a" * 32, "--payload", str(self.root / "payload"), "--sha256", "b" * 64,
                "--expected-selection-sha256", "e" * 64]
        with patch.object(maintenance, "dispatch") as dispatch, patch.object(sys, "argv", argv), \
                redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as exit:
            main()
        self.assertEqual(exit.exception.code, 2)
        self.assertIn("requires both configuration digests", output.getvalue())
        dispatch.assert_not_called()

    def test_manage_update_rejects_incomplete_consent_before_dispatch(self):
        from openmachine.cli import main
        for flag in ("--config-sha256", "--candidate-sha256"):
            with self.subTest(flag=flag):
                argv = ["openmachine", "--config", str(self.config), "--state-dir", str(self.state), "manage", "update",
                        "--operation-id", "a" * 32, "--payload", str(self.root / "payload"), "--sha256", "b" * 64, flag, "c" * 64]
                with patch.object(maintenance, "dispatch") as dispatch, patch.object(sys, "argv", argv), \
                        redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as exit:
                    main()
                self.assertEqual(exit.exception.code, 2)
                self.assertIn("must be supplied together", output.getvalue())
                dispatch.assert_not_called()
