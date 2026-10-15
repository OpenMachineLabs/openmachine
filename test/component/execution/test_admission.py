"""Tests for execution behavior at the component layer."""

import hashlib
import unittest
import uuid
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_disabled_machine_cannot_execute(self):
        self.config.machines["my-laptop"].execution_enabled = False
        with self.assertRaises(Fault) as caught:
            self.start('print("no")')
        self.assertEqual(caught.exception.code, "execution_disabled")
        self.assertFalse(self.state.exists())

    def test_expected_head_mismatch_prevents_launch(self):
        with self.assertRaises(Fault) as caught:
            self.start('from pathlib import Path; Path("should-not-exist").touch()', expected_head_sha="0" * 40)
        self.assertEqual(caught.exception.code, "context_mismatch")
        self.assertFalse((self.work / "should-not-exist").exists())

    def test_unsafe_state_directory_is_rejected_without_chmod(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaises(Fault) as caught:
            self.start('print("no")')
        self.assertEqual(caught.exception.code, "unsafe_state")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)

    def test_workspace_conflict_reports_launch_directory_and_canonical_root(self):
        nested = self.work / "nested"
        nested.mkdir()
        private_directory(self.state)
        locks = private_directory(self.state / "locks")
        key = hashlib.sha256(str(self.work).encode()).hexdigest()
        for git in (False, True):
            if git:
                self._git("init", "-q")
            identity = uuid.uuid4().hex
            with self.subTest(git=git), lease(locks / f"workspace-{key}.lock"), self.assertRaises(Fault) as caught:
                self.start('raise SystemExit("must not launch")', cwd=nested, execution_id=identity)
            self.assertEqual(caught.exception.code, "resource_busy")
            self.assertEqual(caught.exception.evidence,
                             {"cwd": str(nested), "workspace_lock_root": str(self.work)})
            self.assertIn("accepted task scope", str(caught.exception))
            self.assertFalse((self.state / "executions" / identity).exists())

    def test_other_lock_conflicts_do_not_suggest_changing_workspace(self):
        private_directory(self.state)
        locks = private_directory(self.state / "locks")
        resource = hashlib.sha256(b"device:0").hexdigest()
        for kind in ("execution", "resource"):
            identity = uuid.uuid4().hex
            name = f"{identity}.lock" if kind == "execution" else f"resource-{resource}.lock"
            with self.subTest(kind=kind), lease(locks / name), self.assertRaises(Fault) as caught:
                self.start('raise SystemExit("must not launch")', execution_id=identity, resource_keys=["device:0"])
            self.assertEqual(caught.exception.code, "resource_busy")
            self.assertIsNone(caught.exception.evidence)
            self.assertNotIn("cwd", str(caught.exception))
            self.assertFalse((self.state / "executions" / identity).exists())


if __name__ == "__main__":
    unittest.main()
