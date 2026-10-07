"""Tests for execution behavior at the component layer."""

import unittest
from openmachine.execution import logs
from test.support.execution_cases import ExecutionCaseFixture


class ExecutionTests(ExecutionCaseFixture, unittest.TestCase):
    def test_output_presentation_flags_preserve_raw_bytes_and_rereads(self):
        directory = self.root / "presentation"
        directory.mkdir()
        data = "\u00e9\ufffd\n\t\x1b]52;c;fixture\x07\r\u0085".encode() + b"\xff" + b"token" + b"=fixture"
        with logs.Writer(directory) as writer:
            writer.write(data)
        page = logs.read(directory, 0, 65536)
        self.assertEqual(page["next_offset"], len(data))
        self.assertTrue(page["output_decode_replaced"] and page["output_redacted"] and page["output_controls_escaped"])
        self.assertFalse(page["output_gap"] or page["output_presentation_truncated"])
        self.assertIn("\\x1b]52", page["output"])
        self.assertNotIn("\x1b", page["output"])
        self.assertNotIn("\r", page["output"])
        self.assertIn("\n\t", page["output"])
        self.assertEqual(logs.read(directory, 0, 65536), page)
        for offset in (0, 1):
            part = logs.read(directory, offset, 1)
            self.assertTrue(part["output_decode_replaced"])
            self.assertEqual(part["next_offset"], offset + 1)
        self.assertFalse(logs.read(directory, 2, 3)["output_decode_replaced"])


if __name__ == "__main__":
    unittest.main()
