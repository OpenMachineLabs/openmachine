"""Private Events durability, admission and transport; mocked callbacks are not Work acceptance."""

import base64
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from integrations import events
from openmachine import service, tasks
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
import test_continuity as continuity
from openmachine.service import checks as service_checks


class EventsTests(unittest.TestCase):
    setUp = continuity.TaskObserverTests.setUp
    create = continuity.StructuredTaskTests.create
    fault = continuity.ContinuityTests.fault
    view = continuity.TaskAdmissionTests.view
    control_arguments = continuity.TaskAdmissionTests.control_arguments
    control = continuity.TaskAdmissionTests.control
    context_for = continuity.TaskAdmissionTests.context_for
    accept_plan = continuity.TaskAdmissionTests.accept_plan
    write_arguments = continuity.TaskAdmissionTests.write_arguments
    until = continuity.TaskObserverTests.until

    def params(self, **changes):
        return {"name": events.EVENT, "arguments": {"task_id": "structured-one"},
            "delivery": {"mode": "webhook", "url": "https://callback.example/event",
                         "secret": "whsec_" + base64.b64encode(b"k" * 32).decode()},
            "cursor": None, "ttlMs": 3600000, **changes}

    def callback(self, url, body, identity, subscription, seconds):
        value = json.loads(body)
        if value.get("type") == "verification":
            return 200, events.canonical({"challenge": value["challenge"]}), None
        return 204, b"", None

    def subscribe(self, params=None):
        with patch.object(events, "post", side_effect=self.callback):
            return self.app.subscribe_event(params or self.params())

    def fact(self):
        return self.app.events.facts("structured-one")[0]

    def deliver(self, subscription):
        self.app.deliver_task_event(subscription["id"], "structured-one", threading.Event())

    def offer(self):
        record = tasks.read(self.state, "structured-one")["record"]
        handoff = record["handoff"]
        return {"payload": {"task_id": record["task_id"], "handoff_id": handoff["id"],
                    "task_revision": record["revision"], "plan_revision": record["plan"]["revision"], "reason": handoff["reason"]},
                "occurred_at_ms": handoff["created_at_ms"], "epoch": record["control"]["epoch"],
                "expires_at_ms": handoff["expires_at_ms"]}

    def test_verification_idempotency_rotation_and_private_read_surface(self):
        self.accept_plan(claim=False)
        with patch.object(events, "post", side_effect=self.callback) as post:
            first = self.app.subscribe_event(self.params())
            raw = events.read(self.state)["subscriptions"][first["id"]]
            second = self.app.subscribe_event(self.params())
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(post.call_count, 1)
            self.assertEqual(events.read(self.state)["subscriptions"][first["id"]]["verified_at_ms"], raw["verified_at_ms"])
            rotated = self.params()
            rotated["delivery"]["secret"] = "whsec_" + base64.b64encode(b"n" * 32).decode()
            third = self.app.subscribe_event(rotated)
            self.assertEqual(third["id"], first["id"])
            self.assertEqual(post.call_count, 2)
        current = events.read(self.state)["subscriptions"][first["id"]]
        self.assertEqual(current["previous_secret"], raw["secret"])
        self.assertLessEqual(current["rotation_until_ms"] - current["verified_at_ms"], 300000)
        self.assertEqual((self.state / "events.json").stat().st_mode & 0o777, 0o600)
        visible = json.dumps(self.view())
        for private in (current["secret"], current["url"], current["previous_secret"]):
            self.assertNotIn(private, visible)
        self.assertIsNone(first["cursor"])
        self.assertFalse(first["truncated"])

    def test_challenge_failure_and_cancelled_verification_do_not_activate(self):
        self.create()
        for reply in ((200, b'{"challenge":"wrong"}', None), (302, b"", None), (200, b"not JSON", None)):
            with patch.object(events, "post", return_value=reply):
                self.fault("callback_challenge_failed", self.app.subscribe_event, self.params())
            self.assertFalse((self.state / "events.json").exists())
        stop = threading.Event()
        stop.set()
        with patch.object(events, "post", side_effect=self.callback):
            self.fault("callback_timeout", self.app.subscribe_event, self.params(), stop)
        self.assertFalse((self.state / "events.json").exists())

    def test_cancelled_verifications_keep_both_slots_until_their_threads_exit(self):
        self.create()
        arrived, errors, threads = [], [], []
        release, cancelled = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def callback(*args):
            arrived.append(1)
            release.wait(5)
            return self.callback(*args)
        def subscribe():
            try:
                self.app.subscribe_event(self.params(), cancelled)
            except Fault as error:
                errors.append(error.code)
        with patch.object(events, "post", side_effect=callback):
            for _ in range(2):
                thread = threading.Thread(target=subscribe)
                threads.append(thread)
                thread.start()
            self.until(lambda: len(arrived) == 2)
            cancelled.set()
            self.fault("callback_busy", self.app.subscribe_event, self.params())
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["callback_timeout", "callback_timeout"])
        self.assertFalse((self.state / "events.json").exists())

    def test_invalid_filters_keys_and_addresses_have_no_subscription_effect(self):
        self.create()
        for delta in ({"arguments": {}}, {"arguments": {"task_id": "structured-one", "all": True}},
                      {"cursor": "replay"}, {"ttlMs": True}, {"principal": "forged"}, {"principal": None}):
            self.fault("invalid_event_request", self.app.subscribe_event, self.params(**delta))
        params = self.params()
        params["delivery"]["secret"] = "whsec_invalid"
        self.fault("invalid_event_secret", self.app.subscribe_event, params)
        params = self.params()
        params["delivery"]["url"] = "https://127.0.0.1/event"
        self.fault("callback_address", self.app.subscribe_event, params)
        self.assertFalse((self.state / "events.json").exists())

    def test_stalled_dns_is_killed_and_reaped_before_releasing_verification_slots(self):
        self.create()
        popen, post, children = subprocess.Popen, events.post, []
        def stalled(*args, **kwargs):
            child = popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            return child
        def short_deadline(url, body, identity, subscription, seconds):
            return post(url, body, identity, subscription, 0.1)
        started = time.monotonic()
        with patch.object(events.subprocess, "Popen", side_effect=stalled), \
             patch.object(events, "post", side_effect=short_deadline):
            for _ in range(3):
                self.fault("callback_timeout", self.app.subscribe_event, self.params())
                self.assertIsNotNone(children[-1].poll())
        self.assertEqual(len(children), 3)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse((self.state / "events.json").exists())

    def test_pause_during_verification_rechecks_scope_without_holding_task_lock(self):
        self.accept_plan(claim=False)
        def callback(*args):
            self.control("pause", reason="Pause during verification")
            return self.callback(*args)
        with patch.object(events, "post", side_effect=callback):
            self.fault("task_inactive", self.app.subscribe_event, self.params())
        self.assertFalse((self.state / "events.json").exists())
        self.assertEqual(self.view()["record"]["control"]["stop_intent"], "pause")

    def test_uncertain_send_restart_preserves_identity_payload_and_budget(self):
        self.accept_plan(claim=False)
        sub = self.subscribe()
        bodies = []
        def lost(url, body, *args):
            bodies.append(body)
            raise Fault("callback_timeout", "Lost response")
        with patch.object(events, "post", side_effect=lost), patch.object(events.random, "uniform", return_value=1):
            self.deliver(sub)
        first = self.fact()["delivery"]
        self.assertEqual(first["budget"]["attempts"], 1)
        self.assertIsNone(first["accepted_at_ms"])
        self.app = create_application(self.config, self.state)
        def received(url, body, *args):
            bodies.append(body)
            return 204, b"", None
        with patch.object(events, "now_ms", return_value=first["budget"]["next_attempt_ms"]), patch.object(events, "post", side_effect=received):
            self.deliver(sub)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(self.fact()["delivery"]["budget"]["attempts"], 2)
        self.assertEqual(self.fact()["delivery"]["budget"]["started_at_ms"], first["budget"]["started_at_ms"])
        with patch.object(events, "post", side_effect=AssertionError("Acknowledged events never redeliver")):
            self.deliver(sub)
        self.assertEqual(self.view()["observations"][0]["state"], "delivered")

    def test_read_claim_and_next_real_effect_are_distinct_and_duplicates_are_fenced(self):
        self.accept_plan(claim=False)
        sub = self.subscribe()
        with patch.object(events, "post", side_effect=self.callback):
            self.deliver(sub)
        canonical = self.path.read_bytes()
        private = (self.state / "events.json").read_bytes()
        self.assertEqual(self.view()["observations"][0]["state"], "delivered")
        self.assertEqual(self.path.read_bytes(), canonical)
        self.assertEqual((self.state / "events.json").read_bytes(), private)
        self.app.events.flush_reads()
        self.assertEqual(self.view()["observations"][0]["state"], "read_observed")
        args = self.control_arguments("claim", handoff_id=self.offer()["payload"]["handoff_id"])
        self.app.control_task(**args)
        self.assertEqual(self.view()["observations"][0]["state"], "claimed")
        mutation = self.write_arguments()
        result = self.app.write_workspace_file(**mutation)
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")
        self.assertEqual(self.view()["observations"][0]["state"], "next_intent_admitted")
        self.fault("task_conflict", self.app.control_task, **dict(args, update_id="9" * 32))
        again = self.app.write_workspace_file(**mutation)
        self.assertEqual(again["change_id"], result["change_id"])
        self.assertEqual(len(self.view()["record"]["operations"]), 1)

    def test_read_observation_during_callback_does_not_discard_acknowledgement(self):
        self.create()
        sub = self.subscribe()
        def callback(*args):
            self.view()
            self.app.events.flush_reads()
            return self.callback(*args)
        with patch.object(events, "post", side_effect=callback):
            self.deliver(sub)
        self.assertIsNotNone(self.fact()["delivery"]["accepted_at_ms"])
        self.assertIsNotNone(self.fact()["delivery"]["read_at_ms"])

    def test_revoked_generation_cannot_publish_into_a_recreated_subscription(self):
        self.create()
        sub = self.subscribe()
        offer = self.offer()
        job = self.app.events.reserve(sub["id"], self.app.task_admission.principal, offer)
        params = self.params()
        params.pop("cursor")
        params.pop("ttlMs")
        params["delivery"].pop("secret")
        self.app.unsubscribe_event(params)
        self.app.unsubscribe_event(params)
        again = self.subscribe()
        self.assertEqual(again["id"], sub["id"])
        self.app.events.complete(job, status=204)
        self.assertIsNone(self.fact()["delivery"])
        self.fault("events_superseded", self.app.events.deliver, job)

    def test_late_receipt_cannot_resume_paused_task_or_authorize_effect(self):
        self.accept_plan(claim=False)
        sub = self.subscribe()
        def callback(*args):
            self.control("pause", reason="Stop before delivery returns")
            return self.callback(*args)
        with patch.object(events, "post", side_effect=callback):
            self.deliver(sub)
        self.assertEqual(self.view()["observations"][0]["state"], "paused")
        self.assertIsNotNone(self.fact()["delivery"]["accepted_at_ms"])
        self.fault("task_handoff_conflict", self.control, "claim", handoff_id=self.fact()["delivery"]["payload"]["handoff_id"])
        with patch.object(events, "post", side_effect=AssertionError("Paused event dispatch")):
            self.fault("task_inactive", self.deliver, sub)

    def test_connection_backoff_and_permanent_refusal_do_not_fan_out(self):
        self.create()
        first = self.subscribe()
        second_params = self.params()
        second_params["delivery"]["url"] += "2"
        second = self.subscribe(second_params)
        with patch.object(events, "post", return_value=(503, b"", "120")) as post:
            self.deliver(first)
            self.deliver(second)
            self.assertEqual(post.call_count, 1)
        facts = self.app.events.facts("structured-one")
        failed = next(f for f in facts if f["delivery"] is not None)
        self.assertGreaterEqual(failed["connection"]["next_attempt_ms"] - failed["connection"]["started_at_ms"], 120000)
        self.assertEqual(self.app.events.due("structured-one", self.offer()["payload"]["handoff_id"], events.now_ms()), [])
        with patch.object(events, "now_ms", return_value=failed["connection"]["next_attempt_ms"]), patch.object(events, "post", return_value=(410, b"", None)):
            self.deliver(first)
        with patch.object(events, "post", side_effect=AssertionError("410 redelivery")):
            self.deliver(first)
        blocked = next(f for f in self.app.events.facts("structured-one") if f["subscription_id"] == first["id"])
        self.assertEqual(blocked["delivery"]["blocker"], "callback_http_410")

    def test_new_subscriptions_share_the_connection_cooldown(self):
        self.create()
        subscriptions = []
        for index in range(4):
            params = self.params()
            params["delivery"]["url"] += str(index)
            subscriptions.append(self.subscribe(params))
        timestamp = events.now_ms()
        with patch.object(events.random, "uniform", return_value=1):
            for attempt, sub in enumerate(subscriptions, 1):
                with patch.object(events, "now_ms", return_value=timestamp):
                    job = self.app.events.reserve(sub["id"], self.app.task_admission.principal, self.offer())
                    self.assertEqual(job["delivery"]["budget"]["attempts"], 1)
                    self.app.events.complete(job, status=503)
                    connection = self.fact()["connection"]
                    self.assertEqual(connection["attempts"], attempt)
                    self.assertEqual(connection["next_attempt_ms"] - timestamp, (1000, 2000, 4000, 60000)[attempt - 1])
                    timestamp = connection["next_attempt_ms"]

    def test_initial_plan_observer_restart_discovers_same_obligation_without_client(self):
        self.create()
        sub = self.subscribe()
        original = self.path.read_bytes()
        self.app = create_application(self.config, self.state)
        self.addCleanup(self.app.task_observer.stop)
        with patch.object(events, "post", side_effect=self.callback) as post:
            self.app.task_observer.start()
            self.until(lambda: self.fact()["delivery"] and self.fact()["delivery"]["accepted_at_ms"])
            self.app.task_observer.stop()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.fact()["subscription_id"], sub["id"])

    def test_transient_task_admission_conflict_is_not_a_permanent_delivery_failure(self):
        self.create()
        self.subscribe()
        authorize = self.app._validate_task_scope
        calls = []
        def current_scope(record):
            calls.append(record["revision"])
            if len(calls) == 1:
                raise Fault("task_conflict", "A material result just committed")
            return authorize(record)
        with patch.object(self.app, "_validate_task_scope", side_effect=current_scope), \
             patch.object(events, "post", side_effect=self.callback):
            self.app.task_observer.start()
            self.until(lambda: self.fact()["delivery"] and self.fact()["delivery"]["accepted_at_ms"])
            self.app.task_observer.stop()
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.fact()["delivery"]["budget"]["attempts"], 1)

    def test_retained_delivery_requires_actual_state_nine_reader_and_never_resets_corruption(self):
        self.create()
        self.subscribe()
        config = self.root / "events-config.toml"
        config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        proof = service.check_state(config, self.state)
        self.assertEqual(proof["state_contract"], 9)
        self.assertEqual(proof["events_subscriptions"], 1)
        for change in ({"state_contract": 8}, {"events_present": False}, {"events_sha256": "0" * 64}):
            with patch.object(service_checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | change), "")):
                self.fault("incompatible_state", service_checks._candidate_state, Path(sys.executable), config, self.state)
        path = self.state / "events.json"
        original = path.read_bytes()
        path.write_text('{"format":1,"format":1}')
        self.fault("events_corrupt", events.read, self.state)
        self.assertEqual(self.view()["observations"][0]["blocker"], "events_corrupt")
        self.assertEqual(path.read_text(), '{"format":1,"format":1}')
        path.write_bytes(original)
        path.unlink()
        self.fault("events_corrupt", events.read, self.state)

    def test_crash_before_send_and_retry_ceiling_never_reset_after_restart(self):
        self.create()
        sub = self.subscribe()
        principal, offer = self.app.task_admission.principal, self.offer()
        with patch.object(events.random, "uniform", return_value=1):
            job = self.app.events.reserve(sub["id"], principal, offer)
            first = deepcopy(job["delivery"])
            restarted = events.Events(self.state)
            self.assertIsNone(restarted.reserve(sub["id"], principal, offer))
            # Simulate loss after reservation: no new ID, no immediate resend.
            timestamp = first["budget"]["next_attempt_ms"]
            with patch.object(events, "now_ms", return_value=timestamp):
                job = restarted.reserve(sub["id"], principal, offer)
                self.assertEqual(job["delivery"]["id"], first["id"])
                self.assertEqual(job["delivery"]["budget"]["attempts"], 2)
                restarted.complete(job, status=503)
            for attempt in range(3, 61):
                timestamp = restarted.facts("structured-one")[0]["delivery"]["budget"]["next_attempt_ms"]
                with patch.object(events, "now_ms", return_value=timestamp):
                    job = events.Events(self.state).reserve(sub["id"], principal, offer)
                    self.assertIsNotNone(job)
                    self.assertEqual(job["delivery"]["budget"]["attempts"], attempt)
                    restarted.complete(job, status=503)
            with patch.object(events, "now_ms", return_value=timestamp + 60000):
                self.assertIsNone(events.Events(self.state).reserve(sub["id"], principal, offer))
                self.assertEqual(restarted.due("structured-one", offer["payload"]["handoff_id"], timestamp + 60000), [])
        final = self.fact()["delivery"]
        self.assertEqual(final["id"], first["id"])
        self.assertEqual(final["budget"]["deadline_ms"], first["budget"]["deadline_ms"])
        self.assertEqual(self.view()["observations"][0]["blocker"], "delivery_budget_exhausted")

    def test_unverified_reservation_never_contacts_callback(self):
        self.create()
        sub = self.subscribe()
        with patch.object(events, "save_json", side_effect=OSError("injected persistence failure")), \
             patch.object(events, "post", side_effect=AssertionError("Unaccounted callback")):
            self.fault("events_unverified", self.deliver, sub)
        self.assertIsNone(self.fact()["delivery"])

    def test_uncertain_ack_publication_is_not_overwritten_by_a_second_write(self):
        self.create()
        sub = self.subscribe()
        publish = self.app.events.complete
        def uncertain(*args, **kwargs):
            publish(*args, **kwargs)
            raise Fault("events_unverified", "Lost persistence acknowledgement")
        with patch.object(events, "post", side_effect=self.callback), \
             patch.object(self.app.events, "complete", side_effect=uncertain) as complete:
            self.fault("events_unverified", self.deliver, sub)
            self.assertEqual(complete.call_count, 1)
        self.assertIsNotNone(self.fact()["delivery"]["accepted_at_ms"])
        self.assertIsNone(self.fact()["delivery"]["blocker"])

    def test_delivery_slots_remain_owned_until_actual_exit_after_stop(self):
        self.create()
        self.subscribe()
        for name in ("second", "third"):
            params = self.params()
            params["delivery"]["url"] = "https://" + name + ".example/event"
            self.subscribe(params)
        arrived, release = [], threading.Event()
        self.addCleanup(release.set)
        def delayed(url, *args):
            arrived.append(url)
            release.wait(8)
            return 204, b"", None
        observer = self.app.task_observer
        with patch.object(events, "post", side_effect=delayed):
            observer.start()
            self.until(lambda: len(arrived) == 2)
            observer.stop()
            self.assertTrue(observer._thread.is_alive())
            self.fault("task_observer_busy", observer.start)
            self.assertEqual(len(arrived), 2)
            release.set()
            self.until(lambda: not observer._thread.is_alive())
        deliveries = [f["delivery"] for f in self.app.events.facts("structured-one") if f["delivery"]]
        self.assertEqual(len(deliveries), 2)
        self.assertTrue(all(d["accepted_at_ms"] is None and d["budget"]["attempts"] == 1 for d in deliveries))

    def test_refresh_after_verification_cache_expires_reverifies_and_preserves_delivery(self):
        self.create()
        sub = self.subscribe()
        with patch.object(events, "post", side_effect=self.callback):
            self.deliver(sub)
        original = events.read(self.state)["subscriptions"][sub["id"]]
        with patch.object(events, "now_ms", return_value=original["verified_at_ms"] + 300001), \
             patch.object(events, "post", side_effect=self.callback) as post:
            renewed = self.app.subscribe_event(self.params())
            self.assertEqual(post.call_count, 1)
        self.assertEqual(renewed["id"], sub["id"])
        self.assertEqual(self.fact()["delivery"], original["delivery"])

    def test_public_address_pinning_hostname_tls_signatures_and_no_redirect(self):
        params = self.params()["delivery"]
        job = {"id": "f" * 64, "secret": params["secret"], "previous_secret": "whsec_" + base64.b64encode(b"p" * 32).decode(),
               "rotation_until_ms": events.now_ms() + 300000}
        connection, raw, tls = Mock(), Mock(), Mock()
        connection.getresponse.return_value = Mock(status=302, read=Mock(return_value=b""), getheader=Mock(return_value=None))
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        body = b'{"bounded":"fixture"}'
        with patch.object(events, "_resolve", return_value=addresses), patch.object(events.socket, "socket", return_value=raw), \
             patch.object(events.ssl, "create_default_context", return_value=Mock(wrap_socket=Mock(return_value=tls))) as context, \
             patch.object(events.http.client, "HTTPSConnection", return_value=connection):
            status, _, _ = events.post(params["url"], body, "event-fixture", job, 5)
        self.assertEqual(status, 302)
        raw.connect.assert_called_once_with(("8.8.8.8", 443))
        context.return_value.wrap_socket.assert_called_once_with(raw, server_hostname="callback.example")
        connection.request.assert_called_once()
        headers = connection.request.call_args.kwargs["headers"]
        signed = b"event-fixture." + headers["webhook-timestamp"].encode() + b"." + body
        expected = " ".join("v1," + base64.b64encode(hmac.new(events.key(secret), signed, hashlib.sha256).digest()).decode()
                            for secret in (job["secret"], job["previous_secret"]))
        self.assertEqual(headers["webhook-signature"], expected)
        self.assertEqual(connection.request.call_args.kwargs["body"], body)
        self.assertEqual(headers["X-MCP-Subscription-Id"], job["id"])
