"""Focused behavior checks for the same-Chat recovery delivery slice."""

import hashlib
import json
import time
import unittest
from unittest.mock import patch
import uuid

from integrations import chatgpt, codex
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from support_tasks import TaskFixture, task_contract


class ChatRecoveryTests(TaskFixture, unittest.TestCase):
    def enable(self, *, task_id="structured-one", expected_revision=0,
               chat_url="https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"):
        record = self.app.read_task(task_id)["record"]
        return self.app.configure_chat_recovery(task_id, uuid.uuid4().hex, expected_revision, {
            "action": "enable",
            "chat_url": chat_url,
            "machine": "desktop",
            "workspace": "project",
            "authorization_ref": "fixture:consent",
            "expires_at_ms": record["contract"]["expires_at_ms"] - 1000,
        })

    def test_default_policy_binding_disable_and_restart(self):
        self.create()
        before = self.app.read_task("structured-one")
        recovery = before["chat_recovery"]
        self.assertEqual(recovery["state"], "waiting_for_binding")
        self.assertTrue(recovery["default_enabled"])
        self.assertFalse(recovery["bound"])
        self.assertFalse((self.state / "chat_recovery.json").exists())

        enabled = self.enable()
        self.assertEqual((enabled["state"], enabled["binding_revision"]), ("bound", 1))
        self.assertTrue(self.app.read_task("structured-one")["chat_recovery"]["bound"])

        # A new update ID consumes a revision, but does not renew authority.
        same = self.enable(expected_revision=1)
        self.assertEqual(same["binding_revision"], 2)
        self.assertEqual(same["expires_at_ms"], enabled["expires_at_ms"])
        original = (self.state / "chat_recovery.json").read_bytes()

        self.app = create_application(self.config, self.state)
        restarted = self.app.read_task("structured-one")["chat_recovery"]
        self.assertEqual((restarted["state"], restarted["binding_revision"]), ("bound", 2))
        self.assertEqual((self.state / "chat_recovery.json").read_bytes(), original)

        disabled = self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 2,
            {"action": "disable"})
        self.assertEqual((disabled["state"], disabled["binding_revision"]), ("disabled", 3))
        self.assertEqual(self.app.read_task("structured-one")["chat_recovery"]["state"], "disabled")

    def test_binding_rejects_ambiguous_routes_authority_and_shared_conversation(self):
        self.create()
        for url in ("https://chatgpt.com/", "https://chatgpt.com/share/abc",
                    "https://example.com/c/11111111-2222-3333-4444-555555555555",
                    "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555?x=1"):
            with self.subTest(url=url):
                with self.assertRaises(Fault) as error:
                    self.enable(chat_url=url)
                self.assertEqual(error.exception.code, "invalid_chat_recovery_binding")
        record = self.app.read_task("structured-one")["record"]
        with self.assertRaises(Fault) as error:
            self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 0, {
                "action": "enable",
                "chat_url": "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
                "machine": "desktop", "workspace": "project",
                "authorization_ref": "not-authorized",
                "expires_at_ms": record["contract"]["expires_at_ms"] - 1000,
            })
        self.assertEqual(error.exception.code, "chat_recovery_authority_invalid")

        self.enable()
        self.app.create_task(**task_contract(self.context, "structured-two"))
        with self.assertRaises(Fault) as error:
            self.enable(task_id="structured-two")
        self.assertEqual(error.exception.code, "chat_recovery_conversation_busy")

    def test_private_delivery_keeps_business_handoff_and_uses_fixed_message(self):
        created = self.create()
        handoff = created["record"]["handoff"]["id"]
        self.enable()
        recovery_id, execution_id = uuid.uuid4().hex, uuid.uuid4().hex
        calls = []

        def start(config, state, launch, machine, identity, cwd, goal, start_url,
                  allowed_origins, mode, browser, timeout, expected_head, task_context):
            calls.append(dict(machine=machine, identity=identity, cwd=cwd, goal=goal,
                              start_url=start_url, allowed_origins=allowed_origins,
                              mode=mode, browser=browser, timeout=timeout,
                              task_context=task_context, launch=launch))
            return {"status": "accepted", "execution_id": identity}

        with patch.object(codex, "start", side_effect=start):
            started = self.app._start_chat_recovery_delivery(
                "structured-one", recovery_id, execution_id)
        self.assertEqual(started["delivery_execution_id"], execution_id)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["browser"], "chrome")
        self.assertEqual(call["allowed_origins"], ["https://chatgpt.com"])
        self.assertIsNone(call["task_context"])
        self.assertIn("[OM auto-resume: " + recovery_id + "]", call["goal"])
        self.assertIn(chatgpt.claim_update_id(recovery_id), call["goal"])
        self.assertLess(len(chatgpt.recovery_message("structured-one", recovery_id).encode()), 2048)

        current = self.app.read_task("structured-one")["record"]
        self.assertEqual(current["handoff"]["id"], handoff)
        self.assertIsNone(current["control"]["owner"])

        completed = {"status": "completed", "execution_status": "succeeded",
                     "execution_id": execution_id, "machine": "desktop", "exit_code": 0}
        with patch.object(codex, "read", return_value=completed):
            read = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(read["chat_recovery"]["delivery"]["status"], "sent_reported")

        # The same semantic handoff never launches another browser delivery.
        with patch.object(codex, "start", side_effect=AssertionError("must not relaunch")):
            reused = self.app._start_chat_recovery_delivery(
                "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        self.assertEqual(reused["chat_recovery"]["delivery"]["status"], "sent_reported")

    def test_delivery_uncertainty_is_retained_without_browser_replay(self):
        self.create()
        self.enable()
        recovery_id, execution_id = uuid.uuid4().hex, uuid.uuid4().hex
        with patch.object(codex, "start", return_value={"status": "accepted",
                                                        "execution_id": execution_id}):
            self.app._start_chat_recovery_delivery("structured-one", recovery_id, execution_id)
        with patch.object(codex, "read", return_value={"status": "timed_out",
                                                       "execution_status": "timed_out"}):
            read = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(read["chat_recovery"]["delivery"]["status"], "outcome_unknown")
        with patch.object(codex, "read", side_effect=AssertionError("terminal fact must be retained")):
            retained = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(retained["chat_recovery"]["delivery"]["status"], "outcome_unknown")

        proof = chatgpt.check_state(self.state)
        self.assertTrue(proof["chat_recovery_present"])
        self.assertNotIn("chatgpt.com/c/", json.dumps(proof))
        original_delivery = chatgpt.get(self.state, "structured-one").delivery
        self.enable(expected_revision=1,
                    chat_url="https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.app = create_application(self.config, self.state)
        self.assertEqual(chatgpt.get(self.state, "structured-one").delivery, original_delivery)
        with patch.object(codex, "start", side_effect=AssertionError("must not resend after rebind")):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)

    def test_dispatch_is_bounded_by_expiry_and_requires_binding_before_handoff(self):
        self.create()
        with patch.object(self.app.chat_recovery, "_ensure_handoff") as handoff:
            with self.assertRaises(Fault):
                self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
            handoff.assert_not_called()
        self.enable()
        binding = chatgpt.get(self.state, "structured-one")
        execution_id = uuid.uuid4().hex
        with patch.object(chatgpt, "now_ms", return_value=binding.expires_at_ms - 2500), patch.object(
                codex, "start", return_value={"status": "accepted", "execution_id": execution_id}) as start:
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, execution_id)
        self.assertEqual(start.call_args.args[11], 2)

    def test_missing_established_lock_is_not_recreated_by_configuration(self):
        self.create()
        self.enable()
        path = self.state / "chat_recovery.json"
        before = path.read_bytes()
        lock = self.state / "locks/chat-recovery.lock"
        lock.unlink()
        with self.assertRaises(Fault) as error:
            self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 1, {"action": "disable"})
        self.assertEqual(error.exception.code, "chat_recovery_store_invalid")
        self.assertFalse(lock.exists())
        self.assertEqual(path.read_bytes(), before)

    def test_nonterminal_delivery_states_remain_observable(self):
        self.create()
        self.enable()
        identity = uuid.uuid4().hex
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, identity)
        for status in ("accepted", "preparing", "running", "stopping", "finishing"):
            with self.subTest(status=status), patch.object(codex, "read", return_value={"status": status}):
                result = self.app._read_chat_recovery_delivery("structured-one")
                self.assertEqual(result["execution_status"], status)
                self.assertEqual(result["chat_recovery"]["delivery"]["status"], "reserved")
        with patch.object(codex, "read", return_value={"status": "completed"}):
            result = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(result["chat_recovery"]["delivery"]["status"], "sent_reported")

    def test_prelaunch_rejection_releases_only_proven_unsent_reservation(self):
        view = self.create()
        self.enable()
        for code in ("task_stopped", "chat_recovery_conflict", "task_authority_expired"):
            with self.subTest(code=code), patch.object(self.app.chat_recovery, "_task",
                    side_effect=[(view, False), Fault(code, "Rejected before launch.")]), \
                    patch.object(codex, "start") as start:
                self.fault(code, self.app._start_chat_recovery_delivery,
                           "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
                start.assert_not_called()
                self.assertIsNone(chatgpt.get(self.state, "structured-one").delivery)
        resource = hashlib.sha256(codex.RESOURCE.encode()).hexdigest()
        with lease(self.state / "locks" / f"resource-{resource}.lock"):
            self.fault("resource_busy", self.app._start_chat_recovery_delivery,
                       "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        self.assertIsNone(chatgpt.get(self.state, "structured-one").delivery)
        identity = uuid.uuid4().hex
        with patch.object(self.app.machines, "start_execution",
                          side_effect=Fault("execution_start_unverified", "Original launch is uncertain.")):
            self.fault("execution_start_unverified", self.app._start_chat_recovery_delivery,
                       "structured-one", uuid.uuid4().hex, identity)
        retained = chatgpt.get(self.state, "structured-one").delivery
        self.assertEqual((retained.execution_id, retained.status), (identity, "reserved"))

    def test_reserved_delivery_keeps_its_original_controller_until_observed(self):
        self.create()
        self.enable()
        identity = uuid.uuid4().hex
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, identity)
        binding = chatgpt.get(self.state, "structured-one")
        fields = {key: getattr(binding, key) for key in (
            "task_id", "chat_url", "machine", "workspace", "workspace_identity",
            "authority_ref", "authority_sha256", "expires_at_ms")}
        fields.update(machine="another-controller")
        self.fault("chat_recovery_delivery_busy", chatgpt.configure, self.state,
            **fields, update_id=uuid.uuid4().hex, expected_revision=1,
            action="enable", request_sha256="a" * 64)
        self.assertEqual(chatgpt.get(self.state, "structured-one"), binding)
        # Disable must still work without changing the original receipt route.
        self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, 1, {"action": "disable"})
        self.app = create_application(self.config, self.state)
        with patch.object(codex, "read", return_value={"status": "completed"}) as read:
            self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(read.call_args.args[-2:], ("desktop", identity))

    def test_unchanged_policy_consumes_ids_but_exact_retries_preserve_bytes(self):
        self.create()
        self.enable()
        binding = chatgpt.get(self.state, "structured-one")
        enabled = {key: getattr(binding, key) for key in (
            "chat_url", "machine", "workspace", "expires_at_ms")}
        enabled.update(action="enable", authorization_ref=binding.authority_ref)
        disabled = {"action": "disable"}
        for change, conflicting in ((enabled, disabled), (disabled, enabled)):
            revision = chatgpt.get(self.state, "structured-one").revision
            self.app.configure_chat_recovery("structured-one", uuid.uuid4().hex, revision, change)
            identity = uuid.uuid4().hex
            arguments = ("structured-one", identity, revision + 1, change)
            result = self.app.configure_chat_recovery(*arguments)
            self.assertEqual(result["binding_revision"], revision + 2)
            before = (self.state / "chat_recovery.json").read_bytes()
            self.assertEqual(self.app.configure_chat_recovery(*arguments), result)
            self.fault("chat_recovery_change_conflict", self.app.configure_chat_recovery,
                       "structured-one", identity, revision + 1, conflicting)
            self.assertEqual((self.state / "chat_recovery.json").read_bytes(), before)
