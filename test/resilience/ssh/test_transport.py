"""Tests for ssh behavior at the resilience layer."""

import sys
import threading
import time
import unittest
from integrations.ssh import client
from openmachine.foundation.sanitize import Fault



class SSHTests(unittest.TestCase):
    def test_bounded_pipe_exchange_and_oversized_output(self):
        code, data = client._exchange([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"], b"hello")
        self.assertEqual((code, data), (0, b"hello"))
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaises(Fault) as caught:
                client._exchange([sys.executable, "-c", f"import sys; sys.stdin.buffer.read(); sys.{stream}.write('x'*2097152)"], b"hello")
            self.assertEqual(caught.exception.code, "response_limit")

    def test_observation_exchange_cancellation_ends_only_its_owned_transport(self):
        for close_streams in (False, True):
            with self.subTest(close_streams=close_streams):
                cancelled = threading.Event()
                timer = threading.Timer(0.1, cancelled.set)
                timer.start()
                started = time.monotonic()
                code = "import os,sys,time; sys.stdin.buffer.read(); "
                if close_streams:
                    code += "os.close(0); os.close(1); os.close(2); "
                code += "time.sleep(10)"
                try:
                    with self.assertRaises(Fault) as caught:
                        client._exchange([sys.executable, "-c", code], b"{}", cancelled=cancelled)
                finally:
                    timer.join()
                self.assertEqual(caught.exception.code, "observation_cancelled")
                self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
