"""Shared disposable fixtures; no test cases or test discovery."""

import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from integrations import chatgpt, codex
from openmachine.bootstrap import create_application
from openmachine.config import ChatRecoveryConfig
from openmachine.foundation.sanitize import Fault
from openmachine import tasks
from openmachine.foundation.state import lease, save_json
from test.support.tasks import TaskFixture, task_contract



class ChatRecoveryCaseFixture(TaskFixture):
    def setUp(self):
        super().setUp()
        self.config.chat_recovery = ChatRecoveryConfig(workspace="project", connection="Fixture OM",
            browser_account="Fixture Account", browser_workspace="Personal", model="Fixture Model")
        self.app = create_application(self.config, self.state)
        self.request["contract"]["recovery_policy"] = "task_chat"  # Recovery-specific suite opts in explicitly.

    def receiver_fixture(self):
        """Actual registration/reservation with no native browser effects."""
        self.create()
        return self.reserve_receiver("structured-one")

    def reserve_receiver(self, task_id):
        binding = chatgpt.get(self.state, task_id)
        identity = binding.preparation.preparation_id
        chatgpt.reserve_preparation(self.state, task_id, identity, binding.revision, uuid.uuid4().hex)
        return {"task_id": task_id, "preparation_id": identity}

    def preparation_result(self, task_id="structured-one", url=None):
        """Synthetic native-adapter facts; the independent read proof is real."""
        binding = chatgpt.get(self.state, task_id)
        proof = chatgpt.preparation_response(binding, self.app.read_task(task_id)["record"])
        destination = url or "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
        return {"status": "completed", "execution_status": "succeeded", "exit_code": 0, "failure": None,
            "report": {"summary": "Fixture only", "final_url": destination, "tab_id": "fixture-tab",
                "pending_action": "", "evidence": [chatgpt.preparation_message(task_id,
                    binding.preparation.preparation_id, self.config.chat_recovery.connection),
                    proof["receipt"], "Receiver setup turn complete.", "Configured receiver route verified."]},
            "native": {"calls": 1, "origins": [chatgpt.ORIGIN], "snapshot": {
                "url": destination, "tab_id": "fixture-tab", "call_index": 1}}}

    def delivery_result(self, summary=""):
        """Synthetic native-adapter facts, not live browser acceptance."""
        binding = chatgpt.get(self.state, "structured-one")
        return {"status": "blocked" if summary else "completed", "execution_status": "succeeded",
            "exit_code": 0, "failure": None,
            "report": {"summary": summary, "final_url": binding.chat_url, "tab_id": "fixture-tab",
                "pending_action": "", "evidence": ["No send action was taken."] if summary else [
                    "[OM auto-resume: " + binding.delivery.recovery_id + "]",
                    chatgpt.recovery_message(binding.task_id, binding.delivery.recovery_id)]},
            "native": {"calls": 1, "origins": [chatgpt.ORIGIN], "snapshot": {
                "url": binding.chat_url, "tab_id": "fixture-tab", "call_index": 1}}}

    def tick(self):
        self.app.task_observer._tick()
        self.app.chat_recovery._thread.join(5)
        self.assertFalse(self.app.chat_recovery.busy)
        self.assertIsNone(self.app.chat_recovery._failure)

    def enable(self, *, task_id="structured-one", expected_revision=None):
        """Complete a synthetic receiver, or exercise the real explicit override."""
        if expected_revision is not None:
            return self.app.configure_chat_recovery(task_id, uuid.uuid4().hex, expected_revision, {"action": "enable"})
        arguments = self.reserve_receiver(task_id)
        payload = self.app.read_task(**arguments)
        self.app.chat_recovery.external_activity("read_task", arguments, payload)
        with patch.object(codex, "read", return_value=self.preparation_result(task_id)), \
                patch.object(self.app.machines, "read_execution", return_value={
                    "status": "succeeded", "cleanup": {"status": "verified"}}):
            self.app.chat_recovery.read_preparation(task_id)
        return self.app.read_task(task_id)["chat_recovery"]
