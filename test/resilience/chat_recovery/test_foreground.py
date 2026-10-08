"""Tests for chat_recovery behavior at the resilience layer."""

import unittest
from unittest.mock import patch
import uuid
from integrations import codex
from test.support.chat_recovery_cases import ChatRecoveryCaseFixture


class ChatRecoveryTests(ChatRecoveryCaseFixture, unittest.TestCase):
    def test_foreground_effects_do_not_wait_for_recovery_readiness(self):
        self.accept_plan()
        self.app.write_workspace_file(**self.write_arguments())
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")
        arguments = self.start_arguments("from pathlib import Path; Path('counter').write_text('1')")
        with patch.object(codex, "start", side_effect=AssertionError("Foreground work must not launch recovery.")):
            self.wait(self.app.start_execution(**arguments))
        self.assertEqual(self.view()["facts"]["operation_count"], 2)
        self.control("pause", reason="Retain the original result while recovery is unavailable.")
        self.config.chat_recovery.model = "Changed Fixture Model"
        with patch.object(self.app.machines, "start_execution", side_effect=AssertionError("Never replay")):
            original = self.app.start_execution(**arguments)
        self.assertEqual(original["status"], "succeeded")
        self.assertFalse(original["task_operation_replayed"])
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_unconfigured_recovery_does_not_block_foreground_effects(self):
        self.config.chat_recovery = None
        created = self.accept_plan()
        self.assertEqual(created["chat_recovery"]["state"], "blocked")
        self.assertEqual(created["chat_recovery"]["blocker"], "chat_recovery_controller_unconfigured")
        arguments = self.start_arguments()
        self.assertEqual(self.wait(self.app.start_execution(**arguments))["status"], "succeeded")

    def test_known_delivery_blocker_does_not_block_foreground_effects(self):
        self.accept_plan(claim=False)
        self.enable()
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        with patch.object(codex, "read", return_value=self.delivery_result("OM_RECOVERY_NO_SEND:blocked")):
            self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(self.view()["chat_recovery"]["state"], "blocked")
        self.control("claim", handoff_id=self.view()["record"]["handoff"]["id"])
        self.assertEqual(self.wait(self.app.start_execution(**self.start_arguments()))["status"], "succeeded")
        self.assertEqual(self.view()["chat_recovery"]["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
