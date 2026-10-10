"""Resource units, visible hierarchy and bounded OS-reader failure semantics."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from openmachine.execution import resources
from openmachine.foundation.sanitize import Fault


class ResourceTests(unittest.TestCase):
    def test_cgroup_mount_root_ancestors_and_partial_quota_are_not_unlimited(self):
        with tempfile.TemporaryDirectory() as temporary:
            mount = Path(temporary)
            leaf = mount / "leaf"
            leaf.mkdir()
            for directory, quota in ((mount, "200000 100000"), (leaf, "max 100000")):
                (directory / "cpu.max").write_text(quota)
                (directory / "memory.max").write_text("4096")
                (directory / "memory.current").write_text("1024")
            original = resources._read
            def read(path):
                if str(path) == "/proc/self/cgroup":
                    return "0::/group/leaf"
                if str(path) == "/proc/self/mountinfo":
                    return f"29 24 0:26 /group {mount} rw - cgroup2 cgroup rw"
                return original(path)
            with patch.object(resources.sys, "platform", "linux"), patch.object(resources, "_read", side_effect=read):
                result = resources._cgroup(10)
                self.assertTrue(result["complete"])
                self.assertEqual(result["known_cpu_upper_bound_cores"], 2)
                self.assertEqual(result["groups"][0]["memory_limit"]["value"]["limit_bytes"], 4096)
                bounded = resources._cgroup(1)
                self.assertFalse(bounded["complete"])
                self.assertIsNone(bounded["known_cpu_upper_bound_cores"])
                (leaf / "cpu.max").write_text("invalid")
                failed = resources._cgroup(10)
                self.assertEqual(failed["groups"][0]["cpu"]["status"], "unavailable")
                self.assertFalse(failed["complete"])
                self.assertEqual(failed["known_cpu_upper_bound_cores"], 2)

    def test_cgroup_membership_drift_is_explicit_and_v1_not_inferred(self):
        with patch.object(resources.sys, "platform", "linux"):
            with patch.object(resources, "_read", return_value="2:cpu:/group"):
                result = resources.sample("fixture", "process", "cores", lambda: resources._cgroup(1))
            self.assertEqual(result["status"], "unsupported")
            calls = 0
            def moving(path):
                nonlocal calls
                if str(path) == "/proc/self/cgroup":
                    calls += 1
                    return "0::/" if calls == 1 else "0::/moved"
                if str(path) == "/proc/self/mountinfo":
                    return "29 24 0:26 / /fixture rw - cgroup2 cgroup rw"
                raise OSError("Unavailable fixture counter")
            with patch.object(resources, "_read", side_effect=moving):
                result = resources.sample("fixture", "process", "cores", lambda: resources._cgroup(1))
            self.assertEqual(result["error"]["code"], "source_changed")
            self.assertFalse(result["complete"])

    def test_memory_units_missing_availability_and_malformed_pressure(self):
        with patch.object(resources.sys, "platform", "linux"):
            with patch.object(resources, "_read", return_value="MemTotal: 1000 kB\nMemFree: 100 kB"):
                value = resources._memory()
            self.assertEqual(value["total_bytes"], 1024000)
            self.assertIsNone(value["available_bytes"])
            self.assertFalse(value["complete"])
            for text in ("", "some avg10=nan avg60=0 avg300=0 total=1", "not a pressure table"):
                with self.subTest(text=text), patch.object(resources, "_read", return_value=text):
                    result = resources.sample("fixture", "OS", "PSI", resources._pressure)
                self.assertEqual(result["status"], "unavailable")
            text = "some avg10=1.5 avg60=0 avg300=0 total=200\nfull avg10=0 avg60=0 avg300=0 total=10"
            with patch.object(resources, "_read", return_value=text):
                self.assertEqual(resources._pressure()["some"]["total"], 200)

    def test_fixed_reader_enforces_output_and_process_deadlines(self):
        cases = (("print('x' * 1000)", 5, "observation_limit"),
                 ("import time; time.sleep(10)", 0.05, "observation_timeout"))
        for program, seconds, code in cases:
            with self.subTest(code=code), patch.object(resources, "MAX_SOURCE_BYTES", 128), \
                 patch.object(resources, "COMMAND_SECONDS", seconds), self.assertRaises(Fault) as caught:
                resources._command((sys.executable, "-I", "-S", "-c", program))
            self.assertEqual(caught.exception.code, code)

    def test_unavailable_socket_table_never_becomes_zero_listeners(self):
        with patch.object(resources.sys, "platform", "linux"), \
             patch.object(resources, "_read", side_effect=OSError("Unreadable procfs")):
            value = resources.services([8123])
        self.assertTrue(all(item["status"] == "unavailable" and item["value"] is None for item in value.values()))


if __name__ == "__main__":
    unittest.main()
