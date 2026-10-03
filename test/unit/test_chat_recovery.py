"""Focused behavior checks for the same-Chat recovery delivery slice."""

import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from integrations import chatgpt, codex
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine import tasks
from openmachine.foundation.state import lease, save_json
from support_tasks import TaskFixture, task_contract


class ChatRecoveryTests(TaskFixture, unittest.TestCase):
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
        self.assertNotIn("\n", chatgpt.recovery_message("structured-one", recovery_id))

        current = self.app.read_task("structured-one")["record"]
        self.assertEqual(current["handoff"]["id"], handoff)
        self.assertIsNone(current["control"]["owner"])

        completed = self.delivery_result()
        marker_only = json.loads(json.dumps(completed))
        marker_only["report"]["evidence"] = marker_only["report"]["evidence"][:1]
        self.assertEqual(chatgpt.delivery_outcome(marker_only, chatgpt.get(self.state, "structured-one"))[0],
                         "outcome_unknown")
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
        with patch.object(codex, "read", return_value=self.delivery_result()):
            result = self.app._read_chat_recovery_delivery("structured-one")
        self.assertEqual(result["chat_recovery"]["delivery"]["status"], "sent_reported")

    def test_prelaunch_rejection_releases_only_proven_unsent_reservation(self):
        view = self.create()
        self.enable()
        with patch("time.time_ns", return_value=time.time_ns()) as clock:
            for code in ("task_stopped", "chat_recovery_conflict", "task_authority_expired"):
                with self.subTest(code=code), patch.object(self.app.chat_recovery, "_task",
                        side_effect=[(view, False), Fault(code, "Rejected before launch.")]), \
                        patch.object(codex, "start") as start:
                    self.fault(code, self.app._start_chat_recovery_delivery,
                               "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
                    start.assert_not_called()
                    delivery = chatgpt.get(self.state, "structured-one").delivery
                    self.assertEqual((delivery.status, delivery.attempts, delivery.executions), ("deferred", 0, []))
                clock.return_value += chatgpt.GRACE_MS * 1000000
            deadline = delivery.deadline_ms
            resource = hashlib.sha256(codex.RESOURCE.encode()).hexdigest()
            with lease(self.state / "locks" / f"resource-{resource}.lock"):
                self.fault("resource_busy", self.app._start_chat_recovery_delivery,
                           "structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
            self.assertEqual(chatgpt.get(self.state, "structured-one").delivery.attempts, 0)
            self.assertEqual(chatgpt.get(self.state, "structured-one").delivery.deadline_ms, deadline)
            clock.return_value += chatgpt.GRACE_MS * 1000000
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
        with patch.object(codex, "read", return_value=self.delivery_result()) as read:
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

    def test_automatic_no_execution_and_finish_obligations_require_real_progress(self):
        with patch("time.time_ns", return_value=time.time_ns()) as clock, \
                patch.object(codex, "start", return_value={"status": "accepted"}) as start:
            self.create()
            self.enable()
            tasks.update(self.state, "structured-one", lambda record, now: record.update(handoff=None))
            recovery = self.app.chat_recovery
            recovery.start()
            self.addCleanup(recovery.stop)
            clock.return_value += chatgpt.GRACE_MS * 1000000
            self.tick()
            first = chatgpt.get(self.state, "structured-one").delivery
            self.assertEqual(start.call_count, 1)
            self.assertEqual(self.view()["record"]["operations"], [])
            with patch.object(codex, "read", return_value=self.delivery_result()):
                recovery.read_delivery("structured-one")
            for action in ("claim", "record_decision"):
                clock.return_value += 1000000
                arguments = self.control_arguments(action, **({"handoff_id": self.view()["record"]["handoff"]["id"]}
                    if action == "claim" else {"stage_id": "verify", "outcome": "accepted", "evidence": [], "reason": "Fixture decision."}))
                if action == "claim":
                    arguments["update_id"] = first.claim_update_id
                result = self.app.control_task(**arguments)
                recovery.external_activity("control_task", arguments, result)
                self.assertEqual(self.view()["chat_recovery"]["delivery_state"],
                                 "claim_observed" if action == "claim" else "continuation_observed")
            # The accepted final stage still needs finish. A read or claim alone
            # cannot create another episode; the actual accepted decision can.
            clock.return_value += chatgpt.GRACE_MS * 1000000
            self.tick()
            second = chatgpt.get(self.state, "structured-one").delivery
            self.assertEqual(start.call_count, 2)
            self.assertNotEqual(first.recovery_id, second.recovery_id)
            self.assertEqual(self.view()["record"]["plan"]["stages"][0]["state"], "accepted")
            recovery.stop()
            recovery.tick()
            self.assertEqual(start.call_count, 2)

    def test_activity_and_restart_grace_do_not_rewrite_task_or_delivery_budget(self):
        self.create()
        self.enable()
        recovery = self.app.chat_recovery
        with patch("time.time_ns", return_value=time.time_ns() + 10000000) as clock:
            recovery.start()
            original = self.path.read_bytes()
            binding = chatgpt.get(self.state, "structured-one")
            recovery.external_activity("get_capabilities", {"task_id": "structured-one"}, self.app.get_capabilities())
            self.app.read_task("structured-one")
            self.assertEqual(chatgpt.get(self.state, "structured-one"), binding)
            recovery.external_activity("read_task", {}, self.view())
            self.assertGreater(chatgpt.get(self.state, "structured-one").activity_at_ms, binding.activity_at_ms)
            self.assertEqual(self.path.read_bytes(), original)
            clock.return_value += chatgpt.GRACE_MS * 1000000 - 1000000
            self.fault("chat_recovery_caller_active", recovery._quiet, chatgpt.get(self.state, "structured-one"))
            clock.return_value += 1000000
            recovery._quiet(chatgpt.get(self.state, "structured-one"))
            recovery.stop()
            recovery.start()
            self.fault("chat_recovery_caller_active", recovery._quiet, chatgpt.get(self.state, "structured-one"))
            recovery.stop()

    def test_retry_budget_and_original_ids_survive_restart_and_handoff_changes(self):
        self.create()
        self.enable()
        with patch("time.time_ns", return_value=time.time_ns()) as clock, \
                patch.object(codex, "start", return_value={"status": "accepted"}) as start:
            episode, deadline, identities = None, None, []
            for attempt in range(1, 4):
                identity = uuid.uuid4().hex
                self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, identity)
                current = chatgpt.get(self.state, "structured-one").delivery
                episode, deadline = episode or current.recovery_id, deadline or current.deadline_ms
                identities.append(identity)
                self.assertEqual((current.recovery_id, current.deadline_ms, current.attempts), (episode, deadline, attempt))
                with patch.object(codex, "read", return_value=self.delivery_result("OM_RECOVERY_NO_SEND:generating")):
                    self.app._read_chat_recovery_delivery("structured-one")
                self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
                self.assertEqual(start.call_count, attempt)
                tasks.update(self.state, "structured-one", lambda record, now: record.update(handoff=None))
                self.app = create_application(self.config, self.state)
                clock.return_value += chatgpt.GRACE_MS * 1000000
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
            current = chatgpt.get(self.state, "structured-one").delivery
            self.assertEqual((current.status, current.executions, current.deadline_ms), ("exhausted", identities, deadline))
            self.assertEqual(start.call_count, 3)

    def test_running_original_and_unknown_receipt_are_distinct_without_replay(self):
        self.accept_plan()
        self.enable()
        arguments = self.start_arguments("from pathlib import Path; Path('counter').write_text('1')")
        self.wait(self.app.start_execution(**arguments))
        record = self.view()["record"]
        with patch("time.time_ns", return_value=(record["control"]["owner"]["expires_at_ms"] + 1) * 1000000):
            before = self.path.read_bytes()
            with patch.object(self.app.task_admission, "_read", return_value={"status": "running"}):
                self.fault("chat_recovery_work_running", self.app.chat_recovery._task, "structured-one")
            with patch.object(self.app.task_admission, "_read", side_effect=Fault("execution_not_found", "Unknown original receipt.")):
                self.assertTrue(self.app.chat_recovery._task("structured-one")[1])
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(self.app.chat_recovery._task("structured-one")[1])
        self.control("pause", reason="Explicit human wait.")
        self.fault("task_stopped", self.app.chat_recovery._task, "structured-one")
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_legacy_state_and_no_send_evidence_fail_closed(self):
        self.create()
        self.enable()
        with patch.object(codex, "start", return_value={"status": "accepted"}):
            self.app._start_chat_recovery_delivery("structured-one", uuid.uuid4().hex, uuid.uuid4().hex)
        binding = chatgpt.get(self.state, "structured-one")
        result = self.delivery_result("OM_RECOVERY_NO_SEND:generating")
        self.assertEqual(chatgpt.delivery_outcome(result, binding)[0], "deferred")
        result["native"]["snapshot"] = None
        self.assertEqual(chatgpt.delivery_outcome(result, binding)[0], "outcome_unknown")
        path = self.state / "chat_recovery.json"
        legacy = json.loads(path.read_bytes())
        legacy["format"] = 1
        item = legacy["bindings"]["structured-one"]
        item.pop("activity_at_ms")
        for key in chatgpt._DELIVERY_FIELDS:
            item["delivery"].pop(key)
        save_json(path, legacy)
        before = path.read_bytes()
        proof = chatgpt.check_state(self.state)
        self.assertEqual(proof["chat_recovery_format"], 1)
        self.assertEqual(proof["chat_recovery_sha256"], hashlib.sha256(chatgpt.canonical(legacy).encode()).hexdigest())
        self.assertEqual(path.read_bytes(), before)
        with patch.object(codex, "read", side_effect=Fault("execution_not_found", "No launch proof.")):
            self.assertEqual(self.app._read_chat_recovery_delivery("structured-one")["chat_recovery"]["delivery"]["status"], "outcome_unknown")
        self.assertEqual(chatgpt.check_state(self.state)["chat_recovery_format"], 2)
        self.assertEqual(chatgpt.get(self.state, "structured-one").delivery.execution_id, binding.delivery.execution_id)

    def test_recovery_slot_is_drained_under_the_observer_lease(self):
        entered, release = threading.Event(), threading.Event()
        recovery, observer = self.app.chat_recovery, self.app.task_observer
        def blocked_scan():
            entered.set()
            release.wait(5)
        with patch.object(recovery, "_scan", side_effect=blocked_scan) as scan:
            observer.start()
            try:
                self.assertTrue(entered.wait(2))
                recovery.tick()
                self.assertEqual(scan.call_count, 1)
                observer.stop()
                self.fault("resource_busy", lease(self.state / "locks/task-observer.lock").__enter__)
            finally:
                release.set()
                observer.stop()
                observer._thread.join(5)
        self.assertFalse(recovery.busy)
        with lease(self.state / "locks/task-observer.lock"):
            self.assertTrue(recovery._stop.is_set())
