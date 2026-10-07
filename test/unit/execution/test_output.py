"""Tests for execution behavior at the unit layer."""

import json
import unittest
from openmachine import execution
from openmachine.foundation.sanitize import Fault



class ExecutionTests(unittest.TestCase):
    def test_serialized_output_bound_never_changes_consumed_offsets(self):
        original = {"output": "\ufffd" * 100, "offset": 9, "next_offset": 109,
                    "output_presentation_truncated": False}
        measure = lambda value: len(json.dumps(value).encode())
        bounded = execution.bound_output(original, measure, 200)
        self.assertLessEqual(measure(bounded), 200)
        self.assertEqual((bounded["offset"], bounded["next_offset"]), (9, 109))
        self.assertTrue(bounded["output_presentation_truncated"])
        self.assertFalse(original["output_presentation_truncated"])
        with self.assertRaises(Fault) as caught:
            execution.bound_output(original | {"context": "x" * 1000}, measure, 200)
        self.assertEqual(caught.exception.code, "response_limit")


if __name__ == "__main__":
    unittest.main()
