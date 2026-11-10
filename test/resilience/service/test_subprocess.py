"""Management subprocesses have a bounded reply and deadline."""

import sys
import unittest

from openmachine.foundation.sanitize import Fault
from openmachine.service import processes


class SubprocessTests(unittest.TestCase):
    def test_reply_and_deadline_are_bounded(self):
        self.assertEqual(processes.run([sys.executable, "-c", 'print("bounded")']).stdout, "bounded\n")
        for code, argv, timeout in (("service_output_limit", [sys.executable, "-c", 'print("x" * 70000)'], 30),
                                    ("service_command_unverified", [sys.executable, "-c", "import time; time.sleep(10)"], 0.05)):
            with self.subTest(code=code), self.assertRaises(Fault) as caught:
                processes.run(argv, timeout=timeout)
            self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
