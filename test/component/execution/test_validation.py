"""Tests for execution behavior at the component layer."""

import unittest
from openmachine import execution
from openmachine.foundation.sanitize import Fault
from test.support.execution_cases import ExecutionCaseFixture


class ExecutionTests(ExecutionCaseFixture, unittest.TestCase):
    def test_grace_rejects_nonfinite_boolean_and_out_of_range_before_effect(self):
        for grace in (True, False, float("nan"), float("inf"), -0.1, 30.1, "5"):
            with self.subTest(grace=grace), self.assertRaises(Fault) as caught:
                self.start('from pathlib import Path; Path("effect").touch()', termination_grace_seconds=grace)
            self.assertEqual(caught.exception.code, "invalid_grace")
        self.assertFalse((self.work / "effect").exists())

    def test_wait_inputs_reject_invalid_values_without_observation(self):
        for value in (True, False, None, "1", -1, 5.01, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(value=str(value)[:20]), self.assertRaises(Fault) as caught:
                execution.validate_read(0, 1, value)
            self.assertEqual(caught.exception.code, "invalid_wait")
        for offset, size in ((True, 1), (0, False), (-1, 1), (0, 0), (0, 65537), (0.5, 1)):
            with self.subTest(offset=offset, size=size), self.assertRaises(Fault):
                execution.validate_read(offset, size, 0)
        for value in (0, 0.1, 5):
            execution.validate_read(0, 65536, value)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
