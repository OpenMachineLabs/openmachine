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
