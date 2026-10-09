"""Tests for service behavior at the component layer."""

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from openmachine import service
from openmachine.foundation.state import save_json
from openmachine.service import checks as service_checks
from openmachine.service import processes as service_processes
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_pending_and_wrong_runtime_block_new_writes(self):
        self.record["pending"] = {"from": self.a, "to": self.b, "previous": None}
        self.save(self.record)
        self.assert_fault("activation_pending", lambda: service.guard_writes(self.state))
        self.record["pending"] = None
        self.save(self.record)
        self.assert_fault("service_identity_mismatch", lambda: service.guard_writes(self.state))
        with patch.object(sys, "prefix", str(service_state._runtime(self.state, self.a))):
            service.guard_writes(self.state)

    def test_corrupt_selection_is_not_replaced(self):
        value = {**self.record, "current": "../other"}
        self.save(value)
        self.assert_fault("service_state_invalid", lambda: service_state._record(self.state))
        self.assertEqual(json.loads((self.state / "service/service.json").read_text()), value)

    def test_missing_selection_with_launcher_is_not_an_empty_installation(self):
        (self.state / "service/service.json").unlink()
        (self.state / "service/launch.py").write_text(service_state.LAUNCHER)
        self.assert_fault("service_state_invalid", lambda: service.status(self.config, self.state))

    def test_incomplete_installation_metadata_rejects_missing_version(self):
        path = self.state / "service/versions" / self.a / "installation.json"
        value = service_state._json(path)
        del value["version"]
        save_json(path, value)
        self.assert_fault("incompatible_runtime", lambda: service_state._installed(self.state, self.a))

    def test_persistent_policy_requires_actual_new_reader_and_blocks_old_selection_format(self):
        record = self.policy_fixture()
        proof = service.check_state(self.config, self.state)
        self.assertTrue(proof["service_policy_present"])
        self.assertEqual(proof["management_contract"], 1)
        self.assertEqual(service_checks._candidate_state(Path(sys.executable), self.config, self.state), proof)
        for delta in ({"state_contract": 13}, {"service_policy_present": False},
                      {"service_policy_sha256": "0" * 64}, {"service_policy_format": True}):
            with patch.object(service_processes, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | delta), "")):
                self.assert_fault("incompatible_state", lambda: service_checks._candidate_state(Path(sys.executable), self.config, self.state))
        self.save(record | {"format": 1})
        self.assert_fault("service_state_invalid", lambda: service_state._record(self.state))
        self.save(record)
        self.assert_fault("runtime_stopped", lambda: service.guard_writes(self.state))
        self.assert_fault("runtime_stopped", lambda: service_runtime._start(self.state, record))


if __name__ == "__main__":
    unittest.main()
