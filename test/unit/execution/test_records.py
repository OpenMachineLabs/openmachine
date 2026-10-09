"""Pure retained receipt validation without filesystem or process observation."""

from copy import deepcopy
import unittest

from openmachine.execution.records import validate_record
from openmachine.foundation.sanitize import Fault


class RecordTests(unittest.TestCase):
    def test_remote_format_validation_binds_original_id_without_changing_facts(self):
        identity = "a" * 32
        record = {"format_version": 3, "version": "0.1.0", "status": "succeeded", "exit_code": 0,
                  "created_at": 1, "finished_at": 2, "log_truncated": False, "output_incomplete": False,
                  "scope_unit": f"openmachine-{identity}.scope",
                  "context": {"target_identity": "b" * 64, "worker_sha256": "c" * 64},
                  "request": {"machine": "b" * 64, "cwd": "/unavailable/original", "expected_head_sha": "",
                              "argv": ["original-command"], "timeout_seconds": 10, "task_id": "original-task"}}
        original = deepcopy(record)
        validate_record(record, identity)
        self.assertEqual(record, original)
        for candidate, execution_id, code in (
            (record, "d" * 32, "corrupt_state"),
            (record | {"exit_code": None}, identity, "corrupt_state"),
            (record | {"format_version": 99}, identity, "unsupported_state_version"),
        ):
            before = deepcopy(candidate)
            with self.subTest(code=code, execution_id=execution_id), self.assertRaises(Fault) as caught:
                validate_record(candidate, execution_id)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(candidate, before)


if __name__ == "__main__":
    unittest.main()
