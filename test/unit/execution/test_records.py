"""Pure retained receipt validation without filesystem or process observation."""

from copy import deepcopy
import unittest

from openmachine.chat.execution.records import validate_record
from openmachine.foundation.sanitize import Fault


class RecordTests(unittest.TestCase):
    def test_retired_remote_receipt_format_is_rejected_without_modification(self):
        identity = "a" * 32
        record = {"format_version": 3, "status": "succeeded", "exit_code": 0,
                  "scope_unit": f"openmachine-{identity}.scope"}
        original = deepcopy(record)
        with self.assertRaises(Fault) as caught:
            validate_record(record, identity)
        self.assertEqual(caught.exception.code, "unsupported_state_version")
        self.assertEqual(record, original)

    def test_unknown_receipt_format_is_rejected(self):
        record = {"format_version": 99}
        with self.assertRaises(Fault) as caught:
            validate_record(record, "a" * 32)
        self.assertEqual(caught.exception.code, "unsupported_state_version")


if __name__ == "__main__":
    unittest.main()
