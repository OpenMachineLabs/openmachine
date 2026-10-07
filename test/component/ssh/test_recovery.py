"""Tests for ssh behavior at the component layer."""

import unittest
from unittest.mock import patch
from integrations.ssh import recovery
from openmachine.foundation.sanitize import Fault
from test.support.ssh_cases import SSHCaseFixture


class SSHTests(SSHCaseFixture, unittest.TestCase):
    def test_shared_recovery_is_passive_bounded_and_fail_closed(self):
        state = self.root / "controller"
        self.assertEqual(recovery.read(state)["targets"], {})
        self.assertFalse(state.exists())
        episode = {"episode_id": "c" * 32, "started_at_ms": 1, "deadline_ms": 3600001,
                   "attempts": 1, "next_attempt_ms": 2001, "reason_code": "ssh_transport_unavailable"}
        recovery.replace(state, self.identity, None, episode)
        before = (state / "ssh-recovery.json").read_bytes()
        self.assertEqual(recovery.read(state)["targets"][self.identity], episode)
        recovery.replace(state, self.identity, episode, episode)
        self.assertEqual((state / "ssh-recovery.json").read_bytes(), before)
        with self.assertRaises(Fault) as caught:
            recovery.replace(state, self.identity, None, None)
        self.assertEqual(caught.exception.code, "ssh_recovery_conflict")
        self.assertEqual((state / "ssh-recovery.json").read_bytes(), before)
        for data in (before.replace(b'"attempts": 1', b'"attempts": true'),
                     before.replace(b'"deadline_ms": 3600001', b'"deadline_ms": 3600002'),
                     before.replace(b'"format": 1', b'"format": 1, "format": 1'), b"x" * (recovery.MAX_BYTES + 1)):
            (state / "ssh-recovery.json").write_bytes(data)
            with self.assertRaises(Fault):
                recovery.read(state)
            self.assertEqual((state / "ssh-recovery.json").read_bytes(), data)
        (state / "ssh-recovery.json").unlink()
        with self.assertRaises(Fault) as caught:
            recovery.read(state)
        self.assertEqual(caught.exception.code, "ssh_recovery_corrupt")
        with self.assertRaises(Fault):
            recovery.replace(state, self.identity, None, None)
        self.assertFalse((state / "ssh-recovery.json").exists())

    def test_shared_recovery_lost_durability_ack_never_reinitializes(self):
        state = self.root / "controller"
        original = recovery.save_json
        def lost(path, value):
            original(path, value)
            raise OSError("Lost acknowledgement")
        with patch.object(recovery, "save_json", side_effect=lost), self.assertRaises(Fault) as caught:
            recovery.replace(state, self.identity, None, None)
        self.assertEqual(caught.exception.code, "ssh_recovery_unverified")
        self.assertEqual(recovery.read(state)["targets"], {self.identity: None})


if __name__ == "__main__":
    unittest.main()
