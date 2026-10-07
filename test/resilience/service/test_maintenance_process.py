"""Tests for service behavior at the resilience layer."""

import subprocess
import sys
import unittest
from openmachine.foundation.state import lease
from openmachine.service import maintenance
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_real_detached_maintenance_process_keeps_inherited_lock_and_original_record(self):
        record = self.operation_fixture("preparing")
        # This interrupted phase admits no effects. The real module startup
        # validates the inherited lock and exits without touching native jobs.
        original = maintenance._operation_path(self.state, record["operation_id"]).read_bytes()
        with lease(self.state / "service/operation.lock") as lock:
            process = subprocess.Popen([sys.executable, "-I", "-m", "openmachine.service.maintenance",
                str(self.state), record["operation_id"], str(lock.fileno())], cwd="/",
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(lock.fileno(),), start_new_session=True)
            self.assertEqual(process.communicate(timeout=10), (b"", b""))
            self.assertEqual(process.returncode, 0)
            self.assert_fault("resource_busy", lambda: maintenance.continue_operation(self.config, self.state, record["operation_id"]))
        self.assertEqual(maintenance._operation_path(self.state, record["operation_id"]).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
