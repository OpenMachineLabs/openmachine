"""Tests for execution behavior at the component layer."""

import unittest
from openmachine.execution import logs
from openmachine.foundation.sanitize import Fault
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_malformed_log_segment_is_reported_as_corrupt_output(self):
        directory = self.root / "bad-log-fixture"
        directory.mkdir()
        with logs.Writer(directory) as stream:
            stream.write(b"original")
        (directory / "output-invalid.log").write_text("broken")
        with self.assertRaises(Fault) as caught:
            logs.read(directory, 0, 100)
        self.assertEqual(caught.exception.code, "corrupt_output")

    def test_segment_crossing_tail_retention_and_absolute_offsets(self):
        directory = self.root / "log-fixture"
        directory.mkdir()
        with logs.Writer(directory) as stream:
            stream.write(b"a" * (logs.SEGMENT_BYTES - 3))
            stream.write(b"boundary")
            page = logs.read(directory, logs.SEGMENT_BYTES - 3, 8)
            self.assertEqual(page["output"], "boundary")
            stream.write(b"x" * logs.MAX_BYTES)
            stream.write(b"final-message")
        paths = list(directory.glob("output-*.log"))
        self.assertLessEqual(len(paths), 4)
        self.assertLessEqual(sum(path.stat().st_size for path in paths), logs.MAX_BYTES)
        gap = logs.read(directory, 0, 7)
        self.assertTrue(gap["output_gap"])
        tail = logs.read(directory, gap["log_bytes"] - 13, 13)
        self.assertEqual(tail["output"], "final-message")
        self.assertEqual(tail["next_offset"], gap["log_bytes"])


if __name__ == "__main__":
    unittest.main()
