"""Tests for execution behavior at the component layer."""

import sys
import unittest
import uuid
from openmachine.foundation.state import private_directory, save_json
from test.support.execution_cases import ExecutionCaseFixture


class ExecutionTests(ExecutionCaseFixture, unittest.TestCase):
    def test_legacy_retry_preserves_grace_and_never_rewrites_the_receipt(self):
        private_directory(self.state)
        private_directory(self.state / "executions")
        directory = private_directory(self.state / "executions" / uuid.uuid4().hex)
        code = 'print("legacy")'
        record = {"version": "0.1.0", "status": "succeeded", "exit_code": 0, "created_at": 1,
                  "finished_at": 2, "log_truncated": False, "output_incomplete": False,
                  "context": {"worktree": str(self.work)},
                  "request": {"machine": "local", "cwd": str(self.work), "argv": [sys.executable, "-c", code],
                              "expected_head_sha": "", "timeout_seconds": 5}}
        save_json(directory / "record.json", record)
        (directory / "output.log").write_text("legacy\n")
        before = (directory / "record.json").read_bytes()
        for grace in (None, 0.5):
            result = self.start(code, execution_id=directory.name, termination_grace_seconds=grace)
            self.assertEqual(result["termination_grace_seconds"], 0.5)
            self.assertIsNone(result["command"])
            self.assertIsNone(result["cleanup"])
        self.assertEqual((directory / "record.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
