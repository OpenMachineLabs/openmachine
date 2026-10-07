"""Tests for app behavior at the resilience layer."""

import sys
import unittest
import uuid
from openmachine import execution
from test.support.tasks_cases import StructuredTaskCaseFixture


class StructuredTaskTests(StructuredTaskCaseFixture, unittest.TestCase):
    def test_structured_association_blocks_new_launch_but_original_receipt_wins(self):
        self.create()
        identity = uuid.uuid4().hex
        code = 'from pathlib import Path; p=Path("count.txt"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'
        arguments = ("desktop", identity, str(self.work), [sys.executable, "-c", code])
        self.fault("supervised_admission_unavailable", self.app.start_execution, *arguments, task_id="structured-one")
        self.assertFalse((self.state / "executions" / identity).exists())
        # Seed original domain evidence, not an unguarded Application route.
        self.assertEqual(self.wait(execution.start(self.config, self.state, *arguments, task_id="structured-one"))["status"], "succeeded")
        self.path.write_text("unavailable canonical task")
        self.assertEqual(self.app.start_execution(*arguments, task_id="structured-one")["status"], "succeeded")
        self.assertEqual(self.app.read_execution("desktop", identity)["status"], "succeeded")
        self.fault("execution_conflict", self.app.start_execution, "desktop", identity, str(self.work),
                   [sys.executable, "-c", "print('changed')"], task_id="structured-one")
        self.assertEqual((self.work / "count.txt").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
