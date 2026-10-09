"""Tests for connections behavior at the component layer."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from integrations import connections, credentials
from integrations.github import auth, operations
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.config import GitHubConfig
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import checks
from openmachine.tasks import records
from test.support.connections_cases import ConnectionCaseFixture
from test.support.tasks import replace_task_record


class ConnectionTests(ConnectionCaseFixture, unittest.TestCase):
    def test_catalog_is_passive_and_disabled_provider_stays_disabled(self):
        self.assertEqual(self.app.list_integrations()["connections"], [])
        self.assertEqual(self.app.get_capabilities()["github"]["legacy_gh"], "retired")
        self.assertFalse(self.state.exists())
        disabled = create_application(self.config.model_copy(update={"github": None}), self.state)
        self.fault("github_not_allowed", disabled.connect_integration, "1" * 32)
        self.start.assert_not_called()

    def test_native_persistence_restart_and_secret_free_public_results(self):
        item = self.connect()
        self.stop()
        restarted = create_application(self.config, self.state)
        self.assertEqual(restarted.list_integrations()["connections"], [item])
        self.assertEqual(restarted.github_read("repository", "Example/repo")["actor"], self.actor)
        self.assertEqual(self.start.call_count, 1)
        self.assertFalse(self.app.list_integrations()["renewal"]["running"])
        public = json.dumps(self.app.list_integrations()) + (self.state / "connections.json").read_text()
        self.assertNotIn(self.device["device_code"], public)
        for token in self.tokens:
            self.assertNotIn(token, public)
        self.assertEqual((self.state / "connections.json").stat().st_mode & 0o777, 0o600)

    def test_original_id_deduplicates_and_rejects_changed_intent(self):
        self.run_owner()
        identity = uuid.uuid4().hex
        created, release, second_started = threading.Event(), threading.Event(), threading.Event()
        original_guard = connections._guard
        @contextmanager
        def initializing(path, **kwargs):
            if not kwargs.get("shared") and not path.exists():
                # Expose the real file-creation -> flock window before first publish.
                path.touch(mode=0o600)
                created.set()
                self.assertTrue(release.wait(3))
            with original_guard(path, **kwargs):
                yield
        def second_request():
            second_started.set()
            return self.app.connect_integration(identity)
        with patch.object(connections, "_guard", initializing), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.app.connect_integration, identity)
            try:
                self.assertTrue(created.wait(3))
                second = pool.submit(second_request)
                self.assertTrue(second_started.wait(3))
                time.sleep(0.05)
                self.assertFalse(second.done(), "A concurrent request must wait for first publication")
            finally:
                release.set()
            results = [first.result(timeout=3), second.result(timeout=3)]
        self.assertEqual({r["login_id"] for r in results}, {identity})
        self.start.assert_called_once()
        self.assertEqual(self.result(identity)["status"], "succeeded")
        self.fault("integration_request_conflict", self.app.connect_integration, identity, method="existing_gh")
        self.assertEqual(self.app.cancel_integration_login(identity)["status"], "succeeded")

    def test_new_gh_onboarding_is_removed_and_official_app_is_required(self):
        self.fault("integration_unsupported", self.app.connect_integration, "1" * 32, method="existing_gh")
        self.fault("invalid_integration_request", self.app.connect_integration, "bad")
        with patch.object(auth, "CLIENT_ID", ""):
            self.fault("integration_configuration_required", self.app.connect_integration, "2" * 32)
        self.start.assert_not_called()

    def test_cancel_inflight_login_never_establishes_account(self):
        self.run_owner()
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.pair(), 1
        self.poll.side_effect = blocked
        identity = uuid.uuid4().hex
        self.app.connect_integration(identity)
        self.assertTrue(entered.wait(3))
        try:
            self.assertEqual(self.app.cancel_integration_login(identity)["status"], "cancelled")
        finally:
            release.set()
            self.stop()
        self.assertEqual(self.app.list_integrations()["connections"], [])
        self.assertEqual(self.app.cancel_integration_login(identity)["status"], "cancelled")

    def test_disconnect_tombstone_revision_and_reauthorization(self):
        item = self.connect()
        self.fault("integration_connection_changed", self.app.disconnect_integration, item["id"], 99)
        first = self.app.disconnect_integration(item["id"], item["authorization_revision"])
        second = self.app.disconnect_integration(item["id"], item["authorization_revision"])
        self.assertEqual(first["connection"], second["connection"])
        self.assertEqual(first["credential_cleanup"], "deleted")
        self.assertEqual(second["credential_cleanup"], "absent")
        self.fault("integration_authentication_required", self.app.github_read, "repository", "Example/repo")
        again = self.connect(connection_id=item["id"])
        self.assertEqual(again["id"], item["id"])
        self.assertGreater(again["authorization_revision"], item["authorization_revision"])
        self.assertEqual(len(list((self.state / "credentials/github").glob("*.json"))), 1)

    def test_late_reauthorization_cannot_undo_second_disconnect(self):
        item = self.connect()
        disconnected = self.app.disconnect_integration(item["id"], item["authorization_revision"])["connection"]
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.pair(), 1
        self.poll.side_effect = blocked
        identity = uuid.uuid4().hex
        self.app.connect_integration(identity, connection_id=item["id"])
        self.assertTrue(entered.wait(3))
        try:
            self.app.disconnect_integration(item["id"], disconnected["authorization_revision"])
        finally:
            release.set()
            self.stop()
        self.assertEqual(self.app.list_integrations()["connections"][0]["status"], "disconnected")
        self.assertEqual(self.result(identity)["status"], "cancelled")

    def test_wrong_account_and_multiple_accounts_do_not_overwrite(self):
        one = self.connect()
        self.app.disconnect_integration(one["id"], one["authorization_revision"])
        self.actor = {"id": 2, "login": "other"}
        identity = uuid.uuid4().hex
        self.app.connect_integration(identity, connection_id=one["id"])
        self.assertEqual(self.result(identity)["error_code"], "github_identity_changed")
        two = self.connect()
        self.actor = {"id": 1, "login": "operator"}
        self.connect(connection_id=one["id"])
        self.fault("integration_selection_required", self.app.github_read, "repository", "Example/repo")
        self.assertEqual(self.app.github_read("repository", "Example/repo", connection_id=two["id"])["actor"]["id"], 2)

    def test_request_rechecks_disconnect_and_both_resource_policies(self):
        item = self.connect()
        send = self.app.connections.request("Example/repo")
        wider = self.config.model_copy(update={"github": GitHubConfig(repositories=["Example/repo", "Other/repo"])})
        self.fault("github_not_allowed", create_application(wider, self.state).github_read, "repository", "Other/repo")
        self.app.disconnect_integration(item["id"], item["authorization_revision"])
        self.calls.clear()
        with self.assertRaises(Fault) as caught:
            send("GET", "repos/Example/repo")
        self.assertEqual(caught.exception.code, "integration_disconnected")
        self.assertEqual(caught.exception.effect, "known_no_effect")
        self.assertFalse(self.calls)

    def test_admission_and_credential_preparation_errors_precede_business_dispatch(self):
        self.connect()
        self.stop()
        send = self.app.connections.request("Example/repo", write=True)
        self.calls.clear()
        cases = (
            (self.app.connections, "admit", Fault("resource_busy", "Fixture.", effect="effect_unknown"), "resource_busy"),
            (connections, "_guard", Fault("resource_busy", "Fixture.", effect="effect_unknown"), "resource_busy"),
            (self.app.connections, "_token", OSError("Fixture."), "integration_store_unavailable"),
        )
        for owner, name, failure, code in cases:
            with self.subTest(phase=name), patch.object(owner, name, side_effect=failure), self.assertRaises(Fault) as caught:
                operations._mutate("Example/repo", "POST", "repos/Example/repo/issues/1/comments",
                                   {"body": "Once."}, lambda _: "repos/Example/repo/issues/comments/50", transport=send)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.effect, "known_no_effect")
            self.assertFalse(self.calls)
        self.refresh.assert_not_called()

    def test_refresh_reply_loss_is_no_business_effect_and_keeps_uncertain_credentials(self):
        item = self.connect()
        self.stop()
        self.expire_access(item)
        send = self.app.connections.request("Example/repo", write=True)
        self.calls.clear()
        self.refresh.side_effect = Fault("github_response_unknown", "Refresh reply was lost.", effect="effect_unknown")
        with self.assertRaises(Fault) as caught:
            operations._mutate("Example/repo", "POST", "repos/Example/repo/issues/1/comments",
                               {"body": "Once."}, lambda _: "repos/Example/repo/issues/comments/50", transport=send)
        self.assertEqual(caught.exception.code, "github_response_unknown")
        self.assertEqual(caught.exception.effect, "known_no_effect")
        self.assertFalse(self.calls)
        self.assertIsNone(self.comment)
        self.refresh.assert_called_once()
        retained, pair = self.stored(item)
        self.assertEqual((retained.status, pair.phase), ("reauthorization_required", "refreshing"))

    def test_business_reply_loss_keeps_the_same_code_unknown_without_refresh_or_replay(self):
        item = self.connect()
        self.stop()
        send = self.app.connections.request("Example/repo", write=True)
        self.calls.clear()

        def lost(token, method, endpoint, payload=None):
            self.api(token, method, endpoint, payload)
            raise Fault("github_response_unknown", "Business reply was lost.", effect="effect_unknown")

        with patch.object(auth, "request", side_effect=lost):
            result = operations._mutate("Example/repo", "POST", "repos/Example/repo/issues/1/comments",
                                        {"body": "Once."}, lambda _: "repos/Example/repo/issues/comments/50", transport=send)
        self.assertEqual((result["status"], result["error_code"]), ("write_outcome_unknown", "github_response_unknown"))
        self.assertEqual(self.calls, [("POST", "repos/Example/repo/issues/1/comments")])
        self.assertEqual(self.comment["body"], "Once.")
        self.refresh.assert_not_called()
        retained, pair = self.stored(item)
        self.assertEqual((retained.status, pair.phase), ("connected", "ready"))

    def test_first_401_refresh_failure_is_no_business_effect_without_replay(self):
        item = self.connect()
        self.stop()
        send = self.app.connections.request("Example/repo", write=True)
        self.calls.clear()
        self.refresh.side_effect = Fault("github_response_unknown", "Refresh reply was lost.", effect="effect_unknown")

        def rejected(token, method, endpoint, payload=None):
            self.calls.append((method, endpoint))
            raise Fault("github_authentication_required", "The provider rejected this request.")

        with patch.object(auth, "request", side_effect=rejected), self.assertRaises(Fault) as caught:
            operations._mutate("Example/repo", "POST", "repos/Example/repo/issues/1/comments",
                               {"body": "Once."}, lambda _: "repos/Example/repo/issues/comments/50", transport=send)
        self.assertEqual(caught.exception.code, "github_response_unknown")
        self.assertEqual(caught.exception.effect, "known_no_effect")
        self.assertEqual(self.calls, [("POST", "repos/Example/repo/issues/1/comments")])
        self.assertIsNone(self.comment)
        self.refresh.assert_called_once()
        retained, pair = self.stored(item)
        self.assertEqual((retained.status, pair.phase), ("reauthorization_required", "refreshing"))

    def test_refresh_failure_before_readback_keeps_acknowledged_write_unverified(self):
        item = self.connect()
        self.stop()
        send = self.app.connections.request("Example/repo", write=True)
        self.calls.clear()
        self.refresh.side_effect = Fault("github_response_unknown", "Readback refresh reply was lost.", effect="effect_unknown")

        def acknowledged(token, method, endpoint, payload=None):
            result = self.api(token, method, endpoint, payload)
            if method == "POST":
                self.expire_access(item)
            return result

        with patch.object(auth, "request", side_effect=acknowledged):
            result = operations._mutate("Example/repo", "POST", "repos/Example/repo/issues/1/comments",
                                        {"body": "Once."}, lambda _: "repos/Example/repo/issues/comments/50", transport=send)
        self.assertEqual(result["status"], "write_unverified")
        self.assertEqual(result["error_code"], "github_response_unknown")
        self.assertEqual(result["receipt"]["id"], 50)
        self.assertEqual(self.calls, [("POST", "repos/Example/repo/issues/1/comments")])
        self.assertEqual(self.comment["body"], "Once.")
        self.refresh.assert_called_once()
        retained, pair = self.stored(item)
        self.assertEqual((retained.status, pair.phase), ("reauthorization_required", "refreshing"))

    def test_explicitly_unknown_401_does_not_refresh_or_replay_business_write(self):
        self.connect()
        self.stop()
        send = self.app.connections.request("Example/repo", write=True)
        self.calls.clear()

        def lost(token, method, endpoint, payload=None):
            self.api(token, method, endpoint, payload)
            raise Fault("github_authentication_required", "This business request may have taken effect.", effect="effect_unknown")

        with patch.object(auth, "request", side_effect=lost):
            result = operations._mutate("Example/repo", "POST", "repos/Example/repo/issues/1/comments",
                                        {"body": "Once."}, lambda _: "repos/Example/repo/issues/comments/50", transport=send)
        self.assertEqual((result["status"], result["error_code"]), ("write_outcome_unknown", "github_authentication_required"))
        self.assertEqual(self.calls, [("POST", "repos/Example/repo/issues/1/comments")])
        self.assertEqual(self.comment["body"], "Once.")
        self.refresh.assert_not_called()

    def test_reply_loss_after_refresh_keeps_second_business_request_unknown(self):
        item = self.connect()
        self.stop()
        send = self.app.connections.request("Example/repo", write=True)
        self.calls.clear()
        attempts = []

        def reply(token, method, endpoint, payload=None):
            if method == "POST":
                attempts.append(endpoint)
                if len(attempts) == 1:
                    self.calls.append((method, endpoint))
                    raise Fault("github_authentication_required", "The provider rejected the first request.")
                self.api(token, method, endpoint, payload)
                raise Fault("github_response_unknown", "The second business reply was lost.", effect="effect_unknown")
            return self.api(token, method, endpoint, payload)

        with patch.object(auth, "request", side_effect=reply):
            result = operations._mutate("Example/repo", "POST", "repos/Example/repo/issues/1/comments",
                                        {"body": "Once."}, lambda _: "repos/Example/repo/issues/comments/50", transport=send)
        self.assertEqual((result["status"], result["error_code"]), ("write_outcome_unknown", "github_response_unknown"))
        self.assertEqual(self.calls, [("POST", "repos/Example/repo/issues/1/comments"), ("GET", "user"),
                                      ("POST", "repos/Example/repo/issues/1/comments")])
        self.assertEqual(self.comment["body"], "Once.")
        self.refresh.assert_called_once()
        self.assertEqual(self.stored(item)[1].phase, "ready")

    def test_acknowledged_request_cleanup_failure_stays_unknown(self):
        self.connect()
        self.stop()
        send = self.app.connections.request("Example/repo", write=True)
        self.calls.clear()

        @contextmanager
        def fail_on_exit():
            yield
            raise Fault("resource_busy", "Admission cleanup failed after acknowledgment.", effect="known_no_effect")

        with patch.object(self.app.connections, "admit", fail_on_exit):
            result = operations._mutate("Example/repo", "POST", "repos/Example/repo/issues/1/comments",
                                        {"body": "Once."}, lambda _: "repos/Example/repo/issues/comments/50", transport=send)
        self.assertEqual((result["status"], result["error_code"]), ("write_outcome_unknown", "resource_busy"))
        self.assertEqual(self.calls, [("POST", "repos/Example/repo/issues/1/comments")])
        self.assertEqual(self.comment["body"], "Once.")

    def test_concurrent_refresh_is_single_and_preserves_authority(self):
        item = self.connect()
        before = self.expire_access(item).binding()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: self.app.github_read("repository", "Example/repo"), range(4)))
        self.assertTrue(all(v["actor"] == self.actor for v in values))
        self.refresh.assert_called_once()
        self.assertEqual(self.stored(item)[0].binding(), before)

    def test_network_before_refresh_restores_original_grant_without_logout(self):
        item = self.connect()
        original = self.expire_access(item)
        self.refresh.side_effect = Fault("github_network_unavailable", "Fixture.")
        self.fault("github_network_unavailable", self.app.github_read, "repository", "Example/repo")
        self.assertEqual(self.stored(item)[1].phase, "ready")
        self.assertEqual(self.stored(item)[0].binding(), original.binding())
        self.refresh.side_effect = lambda *a: self.pair()
        self.assertEqual(self.app.github_read("repository", "Example/repo")["actor"], self.actor)

    def test_uncertain_refresh_never_reuses_old_rotating_grant(self):
        item = self.connect()
        self.expire_access(item)
        self.refresh.side_effect = Fault("github_response_unknown", "Fixture.")
        self.fault("github_response_unknown", self.app.github_read, "repository", "Example/repo")
        self.assertEqual(self.stored(item)[1].phase, "refreshing")
        self.fault("integration_authentication_required", create_application(self.config, self.state).github_read, "repository", "Example/repo")
        self.refresh.assert_called_once()

    def test_rotated_pair_is_persisted_before_verification_and_business_use(self):
        item = self.connect()
        self.expire_access(item)
        with patch.object(auth, "read_actor", side_effect=Fault("github_network_unavailable", "Fixture.")):
            self.fault("github_network_unavailable", self.app.github_read, "repository", "Example/repo")
        self.assertEqual(self.stored(item)[1].phase, "unverified")
        self.assertEqual(self.app.github_read("repository", "Example/repo")["actor"], self.actor)
        self.refresh.assert_called_once()
        self.assertEqual(self.stored(item)[1].phase, "ready")

    def test_failed_persistence_does_not_expose_or_repeat_new_grant(self):
        item = self.connect()
        self.expire_access(item)
        real = credentials.save
        def save(state, identity, store, value):
            if value.phase == "unverified":
                raise Fault("integration_credential_store_unavailable", "Fixture.")
            return real(state, identity, store, value)
        with patch.object(credentials, "save", side_effect=save):
            self.fault("integration_credential_store_unavailable", self.app.github_read, "repository", "Example/repo")
        self.assertEqual(self.stored(item)[1].phase, "refreshing")
        self.fault("github_reauthorization_required", self.app.github_read, "repository", "Example/repo")
        self.refresh.assert_called_once()

    def test_runtime_maintains_refresh_deadline_without_ui(self):
        item = self.connect()
        self.stop()
        value, pair = self.stored(item)
        deadline = connections.now_ms() + 86400000
        credentials.save(self.state, value.credential_ref, value.credential_store, pair.model_copy(update={"refresh_expires_at_ms": deadline}))
        with self.app.connections._writing() as data:
            data.connections[item["id"]].refresh_expires_at_ms = deadline
            self.app.connections._save(data)
        self.run_owner()
        end = time.monotonic() + 3
        while not self.refresh.called and time.monotonic() < end:
            time.sleep(0.01)
        self.stop()
        self.refresh.assert_called_once()
        self.assertGreater(self.stored(item)[1].refresh_expires_at_ms, deadline)

    def test_missing_corrupt_or_unsafe_state_is_not_reset(self):
        self.connect()
        path = self.state / "connections.json"
        original = path.read_bytes()
        for raw in (b"{}", b'{"format":2}', b'{"format":2,"format":2}', b"[]"):
            path.write_bytes(raw)
            self.fault("integration_store_invalid", self.app.list_integrations)
            self.assertEqual(path.read_bytes(), raw)
        path.write_bytes(original)
        path.chmod(0o644)
        self.fault("integration_store_invalid", self.app.list_integrations)
        path.chmod(0o600)
        path.unlink()
        self.fault("integration_store_invalid", self.app.connect_integration, uuid.uuid4().hex)
        self.assertFalse(path.exists())

    def test_initializing_writer_is_not_reported_as_corruption(self):
        private_directory(self.state)
        private_directory(self.state / "locks")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with lease(self.state / "locks/connections.lock"):
                future = pool.submit(self.app.list_integrations)
                time.sleep(0.03)
                self.assertFalse(future.done())
                save_json(self.state / "connections.json", dict(format=2, connections={}, attempts={}))
            self.assertEqual(future.result(2)["connections"], [])

    def test_interrupted_attempt_read_is_passive_and_capacity_retains_ids(self):
        self.run_owner()
        self.device["interval"] = 3
        identity = uuid.uuid4().hex
        self.app.connect_integration(identity)
        with patch.object(connections, "MAX_ATTEMPTS", 1):
            self.fault("integration_capacity", self.app.connect_integration, uuid.uuid4().hex)
        self.stop()
        raw = (self.state / "connections.json").read_bytes()
        self.assertEqual(self.result(identity)["error_code"], "runtime_interrupted")
        self.assertEqual((self.state / "connections.json").read_bytes(), raw)
        self.start.assert_called_once()

    def test_native_tasks_bind_account_and_never_replay_business_write(self):
        self.connect()
        self.request["contract"].update(repositories=["Example/repo"], tools=["github_comment"])
        record = self.create()["record"]
        self.assertEqual(record["contract"]["github_connections"]["Example/repo"]["account_id"], 1)
        self.control("claim", handoff_id=record["handoff"]["id"])
        args = dict(action="create", repository="Example/repo", number=1, body="Fixture.", task_context=self.context_for("a" * 32))
        self.assertEqual(self.app.github_comment(**args)["status"], "verified")
        self.app.github_comment(**args)
        self.assertEqual(sum(m == "POST" for m, _ in self.calls), 1)
        self.assertEqual(self.view()["record"]["operations"][0]["locator"]["connection"], record["contract"]["github_connections"]["Example/repo"])

    def test_legacy_task_identity_and_serialization_are_not_migrated(self):
        self.request["contract"].update(repositories=["Example/repo"], tools=["github_comment"])
        old = tasks.create(self.state, self.request["task_id"], **{k: v for k, v in self.request.items() if k != "task_id"})["record"]
        # Seed an actual historical shape; the current constructor creates format 4.
        old["format"] = 1
        old.pop("control_revision", None)
        old.pop("effect_scopes", None)
        for operation in old["operations"]:
            operation.pop("effect_scope", None)
            if operation["result"] is not None:
                operation["result"].pop("failure_code", None)
                operation["result"].pop("effect", None)
        old = replace_task_record(self.state, old)
        raw = self.task_bytes()
        self.assertEqual(old["format"], 1)
        observed = {key: value for key, value in self.view()["record"].items() if key not in {"_facts", "_decisions"}}
        self.assertEqual(records.canonical(records.Record.model_validate(observed).model_dump()), records.canonical(old))
        self.fault("integration_legacy_connection", self.control, "claim", handoff_id=old["handoff"]["id"])
        self.assertEqual(self.calls, [])
        self.actor = {"id": 2, "login": "other"}
        native = self.connect()
        self.fault("task_scope_denied", self.app.github_comment, "create", "Example/repo", "Fixture.", 1,
                   connection_id=native["id"], task_context=self.context_for("c" * 32))
        self.assertNotIn("github_connections", self.view()["record"]["contract"])
        self.calls.clear()
        self.fault("integration_legacy_connection", self.app._github_call, "read",
                   dict(action="repository", repository="Example/repo", connection_id=native["id"]),
                   historical_unbound=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.task_bytes(), raw)

    def test_retired_bound_history_is_readable_without_dispatch_or_native_fallback(self):
        self.connect()
        self.request["contract"].update(repositories=["Example/repo"], tools=["github_comment"])
        record = self.create()["record"]
        self.control("claim", handoff_id=record["handoff"]["id"])
        args = dict(action="create", repository="Example/repo", number=1, body="Fixture.",
                    task_context=self.context_for("a" * 32))
        self.assertEqual(self.app.github_comment(**args)["status"], "verified")
        self.stop()
        old = self.view()["record"]
        old["format"] = 2
        old.pop("control_revision", None)
        old.pop("effect_scopes", None)
        for operation in old["operations"]:
            operation.pop("effect_scope", None)
            if operation["result"] is not None:
                operation["result"].pop("failure_code", None)
                operation["result"].pop("effect", None)
        # Seed retained pre-native authority, including its original receipt.
        binding = dict(connection_id="e" * 32, account_id=1, authorization_revision=1)
        old["contract"]["github_connections"]["Example/repo"] = binding
        old["operations"][0]["locator"]["connection"] = binding
        old = replace_task_record(self.state, old)
        retained = connections.Connection(id=binding["connection_id"], provider="github", backend="gh",
            account_id=1, account_login="operator", authorization_revision=1, status="connected",
            policy=self.config.github, verified_at_ms=connections.now_ms())
        attempt_id = "d" * 32
        attempt = connections.Attempt(intent=dict(provider="github", method="existing_gh", connection_id=""),
            status="succeeded", created_at_ms=1, expires_at_ms=1000, connection_id=retained.id)
        save_json(self.state / "connections.json", connections.Store(format=1,
            connections={retained.id: retained}, attempts={attempt_id: attempt}).model_dump())
        raw_task, raw_connections = self.task_bytes(), (self.state / "connections.json").read_bytes()
        self.calls.clear()
        self.assertTrue(self.app.list_integrations()["connections"][0]["historical_only"])
        self.assertEqual(self.app.read_integration_login(attempt_id)["status"], "succeeded")
        self.assertEqual(self.view()["record"]["operations"], old["operations"])
        self.fault("integration_legacy_connection", self.app.github_read, "status", connection_id=retained.id)
        historical = self.app.github_comment(**args)
        self.assertEqual((historical["status"], historical["current_state_observed"], historical["task_operation_replayed"]),
                         ("retained_task_evidence", False, False))
        self.assertEqual(historical["result"], old["operations"][0]["result"])
        self.assertEqual(self.task_bytes(), raw_task)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.state / "connections.json").read_bytes(), raw_connections)
        self.actor = {"id": 2, "login": "other"}
        native = self.connect()
        self.calls.clear()
        self.fault("integration_legacy_connection", self.control, "claim", handoff_id="f" * 32)
        historical = self.app.github_comment(**args)
        self.assertEqual((historical["status"], historical["current_state_observed"], historical["task_operation_replayed"]),
                         ("retained_task_evidence", False, False))
        self.assertEqual(historical["result"], old["operations"][0]["result"])
        self.fault("integration_legacy_connection", self.app.github_comment,
                   **dict(args, task_context=self.context_for("b" * 32)))
        self.fault("task_scope_denied", self.app.github_comment, "create", "Example/repo", "Fixture.", 1,
                   connection_id=native["id"], task_context=self.context_for("c" * 32))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.task_bytes(), raw_task)

    def test_retired_unbound_pending_effect_stays_unknown_without_reconciliation(self):
        self.connect()
        self.request["contract"].update(repositories=["Example/repo"], tools=["github_comment"])
        record = self.create()["record"]
        self.control("claim", handoff_id=record["handoff"]["id"])
        args = dict(action="create", repository="Example/repo", number=1, body="Fixture.",
                    task_context=self.context_for("a" * 32))
        def lost(token, method, endpoint, payload=None):
            value = self.api(token, method, endpoint, payload)
            if method == "POST":
                raise Fault("github_response_unknown", "Fixture lost reply after publication.")
            return value
        with patch.object(auth, "request", side_effect=lost):
            self.assertEqual(self.app.github_comment(**args)["status"], "write_outcome_unknown")
        self.stop()
        old = self.view()["record"]
        old["format"] = 1
        old.pop("control_revision", None)
        old.pop("effect_scopes", None)
        for operation in old["operations"]:
            operation.pop("effect_scope", None)
            if operation["result"] is not None:
                operation["result"].pop("failure_code", None)
                operation["result"].pop("effect", None)
        old["contract"].pop("github_connections")
        old["operations"][0]["locator"].pop("connection")
        replace_task_record(self.state, old)
        raw = self.task_bytes()
        self.calls.clear()
        observed = self.app.task_admission.reconcile(self.view())
        self.assertEqual(observed["record"]["operations"][0]["result"]["status"], "unknown")
        self.fault("integration_legacy_connection", self.app.github_comment, **args)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.task_bytes(), raw)

    def test_real_state_readers_must_understand_native_authority(self):
        self.connect()
        config = self.root / "config.toml"
        config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        proof = checks.check_state(config, self.state)
        self.assertEqual(proof["state_contract"], 20)
        before = (self.state / "connections.json").read_bytes()
        for change in ({"state_contract": 10}, {"connections_present": False}, {"connections_sha256": "a" * 64}):
            with patch.object(checks, "_run", return_value=subprocess.CompletedProcess([], 0, json.dumps(proof | change), "")):
                self.fault("incompatible_state", checks._candidate_state, Path(sys.executable), config, self.state)
        self.assertEqual((self.state / "connections.json").read_bytes(), before)

    def test_missing_credential_allows_explicit_reauthorization(self):
        item = self.connect()
        value, _ = self.stored(item)
        (self.state / "credentials/github" / (value.credential_ref + ".json")).unlink()
        self.fault("integration_credential_missing", self.app.github_read, "repository", "Example/repo")
        retained = self.app.list_integrations()["connections"][0]
        self.assertEqual(retained["status"], "reauthorization_required")
        again = self.connect(connection_id=item["id"])
        self.assertEqual(again["id"], item["id"])

    def test_disconnect_wakes_pending_reauthorization(self):
        item = self.connect()
        disconnected = self.app.disconnect_integration(item["id"], item["authorization_revision"])["connection"]
        self.device["interval"] = 60
        identity = uuid.uuid4().hex
        self.app.connect_integration(identity, connection_id=item["id"])
        self.fault("activation_deferred", connections.check_state, self.state, require_idle=True)
        self.app.disconnect_integration(item["id"], disconnected["authorization_revision"])
        end = time.monotonic() + 1
        while identity in self.app.connections._jobs and time.monotonic() < end:
            time.sleep(0.01)
        self.assertNotIn(identity, self.app.connections._jobs)
        self.assertEqual(self.result(identity)["status"], "cancelled")

    def test_private_store_permissions_and_credential_repr(self):
        item = self.connect()
        value, pair = self.stored(item)
        self.assertNotIn(pair.access_token, repr(pair))
        self.assertNotIn(pair.refresh_token, repr(pair))
        path = self.state / "credentials/github" / (value.credential_ref + ".json")
        path.chmod(0o644)
        self.fault("integration_credential_store_unavailable", self.app.github_read, "repository", "Example/repo")
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        path.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
