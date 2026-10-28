"""Private web authorization and rotation through the existing connection owner."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from integrations import connection_state, credentials
from integrations.github import auth, web_authorization
from openmachine.bootstrap import create_application
from openmachine.config import GitHubConfig
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, save_json
from openmachine.service import checks, processes
from openmachine.service.state import STATE_CONTRACT
from test.support.connections_cases import ConnectionCaseFixture


class WebConnectionTests(ConnectionCaseFixture, unittest.TestCase):
    def begin(self, **values):
        self.run_owner()
        request = dict(v=1, intent_id=uuid.uuid4().hex,
                       core_binding=self.app.connections.web_identity()["core_binding"])
        request.update(values)
        self.app.connections.web_begin(request)
        return request

    @staticmethod
    def token_fields(pair):
        return dict(access_token=pair.access_token, refresh_token=pair.refresh_token,
                    expires_in=28800, refresh_token_expires_in=15897600,
                    token_type="bearer")

    def completion(self, original, **values):
        request = dict(v=1, intent_id=original["intent_id"], claim_id=uuid.uuid4().hex,
                       github_account_id=self.actor["id"], tokens=self.token_fields(self.pair()))
        request.update(values)
        return request

    def web_connect(self):
        original = self.begin()
        request = self.completion(original)
        result = self.app.connections.web_complete(request)
        item = self.app.list_integrations(connection_id=result["connection_id"])["connections"][0]
        return item, original, request

    def restart(self):
        self.stop()
        self.app = create_application(self.config, self.state)
        self.run_owner()

    def expire_web(self, item):
        value, pair = self.stored(item)
        credentials.save(self.state, value.credential_ref, value.credential_store,
                         pair.model_copy(update={"access_expires_at_ms": connection_state.now_ms() - 1}))
        return value

    def read_repository(self, item):
        return self.app.github_read("repository", "Example/repo", connection_id=item["id"])

    def rotated(self, state, **request):
        pair = auth.accept_web_tokens(self.token_fields(self.pair()), core_binding=request["core_binding"])
        return pair.model_copy(update={"refresh_id": request["refresh_id"]})

    def assert_secrets_absent(self, request, *values):
        rendered = json.dumps(values, default=str)
        for field in ("access_token", "refresh_token"):
            self.assertNotIn(request["tokens"][field], rendered)

    def test_complete_is_durable_and_public_results_contain_no_tokens(self):
        original = self.begin()
        request = self.completion(original)
        observed = []
        read_actor = auth.read_actor

        def actor(token):
            retained = connection_state._read(self.state).web_intents[original["intent_id"]]
            observed.append((retained.status, retained.claim_id))
            return read_actor(token)

        with patch.object(auth, "read_actor", side_effect=actor):
            result = self.app.connections.web_complete(request)
        self.assertEqual(observed, [("claimed", request["claim_id"])])
        self.assertEqual(result["status"], "connected")
        item = self.app.list_integrations()["connections"][0]
        _, pair = self.stored(item)
        self.assertEqual((pair.source, pair.phase, pair.account_id), ("web", "ready", self.actor["id"]))
        self.assertEqual(pair.core_binding, original["core_binding"])
        self.assertEqual(pair.access_token, request["tokens"]["access_token"])
        self.assert_secrets_absent(request, result, self.app.list_integrations(),
                                  (self.state / "connections.json").read_text())
        self.assertEqual(self.read_repository(item)["data"]["full_name"], "Example/repo")
        self.refresh.assert_not_called()

    def test_begin_retry_retains_exact_original_deadline(self):
        original = self.begin()
        first = self.app.connections.web_status(original["intent_id"])
        with patch.object(web_authorization, "now_ms", return_value=first["expires_at_ms"] - 1):
            self.assertEqual(self.app.connections.web_begin(original), first)
        changed = dict(original, expected_connection_id="a" * 32, expected_authorization_revision=1)
        self.fault("integration_request_conflict", self.app.connections.web_begin, changed)

    def test_begin_observed_expiry_or_configuration_change_cannot_revive(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                original = self.begin()
                retained = connection_state._read(self.state).web_intents[original["intent_id"]]
                approved = self.config.github
                observed_time = retained.expires_at_ms if expired else connection_state.now_ms()
                code = "github_web_intent_expired" if expired else "github_web_binding_changed"
                try:
                    if not expired:
                        self.config.github = GitHubConfig(repositories=["Example/other"])
                    with patch.object(web_authorization, "now_ms", return_value=observed_time):
                        conflict = dict(original, expected_connection_id="a" * 32, expected_authorization_revision=1)
                        self.fault("integration_request_conflict", self.app.connections.web_begin, conflict)
                        self.fault(code, self.app.connections.web_begin, original)
                finally:
                    self.config.github = approved
                retained = connection_state._read(self.state).web_intents[original["intent_id"]]
                self.assertEqual((retained.status, retained.error_code),
                                 ("expired" if expired else "rejected", code))
                self.fault("github_web_intent_consumed", self.app.connections.web_begin, original)

    def test_begin_rejects_unknown_fields_versions_and_unpaired_revision(self):
        original = self.begin()
        invalid = [dict(original, v=True), dict(original, v=1.0), dict(original, v=2),
                   dict(original, principal_id="other"),
                   dict(original, intent_id="A" * 32), dict(original, core_binding="/tmp/core.sock"),
                   dict(original, expected_connection_id="a" * 32),
                   dict(original, expected_authorization_revision=1)]
        for request in invalid:
            with self.subTest(request=request):
                self.fault("invalid_github_web_request", self.app.connections.web_begin, request)
        self.fault("github_web_binding_changed", self.app.connections.web_begin,
                   dict(original, intent_id=uuid.uuid4().hex, core_binding="a" * 64))
        self.assertEqual(len(connection_state._read(self.state).web_intents), 1)

    def test_complete_rejects_invalid_envelope_without_provider_io(self):
        original = self.begin()
        request = self.completion(original)
        invalid = [dict(request, v=True), dict(request, v=1.0), dict(request, v=2),
                   dict(request, github_account_id=True),
                   dict(request, github_login="operator"), dict(request, claim_id=original["intent_id"])]
        with patch.object(auth, "read_actor") as actor:
            for candidate in invalid:
                with self.subTest(fields=sorted(candidate)):
                    self.fault("invalid_github_web_request", self.app.connections.web_complete, candidate)
            actor.assert_not_called()
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "pending")

    def test_invalid_token_shape_consumes_original_claim_without_disclosure(self):
        original = self.begin()
        request = self.completion(original)
        request["tokens"]["unexpected"] = "not-an-import-endpoint"
        with patch.object(auth, "read_actor") as actor, self.assertRaises(Fault) as caught:
            self.app.connections.web_complete(request)
        actor.assert_not_called()
        status = self.app.connections.web_status(original["intent_id"])
        self.assertEqual(status["status"], "rejected")
        self.assert_secrets_absent(request, str(caught.exception), status)
        self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)

    def test_claimed_account_and_operator_pin_are_independently_verified(self):
        original = self.begin()
        self.fault("github_identity_changed", self.app.connections.web_complete,
                   self.completion(original, github_account_id=2))
        self.assertEqual(self.app.list_integrations()["connections"], [])
        self.config.github = GitHubConfig(repositories=["Example/repo"], account="different")
        original = self.begin()
        self.fault("github_identity_changed", self.app.connections.web_complete, self.completion(original))
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "rejected")
        self.assertEqual(self.app.list_integrations()["connections"], [])

    def test_configuration_change_invalidates_local_approval_before_io(self):
        original = self.begin()
        request = self.completion(original)
        self.config.github = GitHubConfig(repositories=["Example/other"])
        with patch.object(auth, "read_actor") as actor:
            self.fault("github_web_binding_changed", self.app.connections.web_complete, request)
            actor.assert_not_called()
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "rejected")

    def test_status_durably_rejects_changed_configuration_without_provider_io(self):
        original = self.begin()
        request = self.completion(original)
        approved = self.config.github
        self.config.github = GitHubConfig(repositories=["Example/other"])
        with patch.object(auth, "read_actor") as actor, patch.object(credentials, "save") as save:
            status = self.app.connections.web_status(original["intent_id"])
            self.assertEqual((status["status"], status["error_code"]),
                             ("rejected", "github_web_binding_changed"))
            self.assertEqual(connection_state._read(self.state).web_intents[original["intent_id"]].status, "rejected")
            self.config.github = approved
            self.assertEqual(self.app.connections.web_status(original["intent_id"]), status)
            self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
            actor.assert_not_called()
            save.assert_not_called()

    def test_restart_rejects_changed_configuration_before_original_status_read(self):
        original = self.begin()
        request = self.completion(original)
        approved = self.config.github
        self.stop()
        self.config.github = GitHubConfig(repositories=["Example/other"])
        with patch.object(auth, "read_actor") as actor:
            self.app = create_application(self.config, self.state)
            self.run_owner()
            retained = connection_state._read(self.state).web_intents[original["intent_id"]]
            self.assertEqual((retained.status, retained.error_code), ("rejected", "github_web_binding_changed"))
            self.config.github = approved
            self.restart()
            self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "rejected")
            self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
            actor.assert_not_called()

    def test_reauthorization_requires_original_account_and_current_revision(self):
        item, _, _ = self.web_connect()
        self.fault("integration_connection_changed", self.app.connections.web_begin,
                   dict(v=1, intent_id=uuid.uuid4().hex,
                        core_binding=self.app.connections.web_identity()["core_binding"],
                        expected_connection_id=item["id"], expected_authorization_revision=1))
        self.app.disconnect_integration(item["id"], 1)
        original = self.begin(expected_connection_id=item["id"], expected_authorization_revision=2)
        self.actor = {"id": 2, "login": "another-account"}
        self.fault("github_identity_changed", self.app.connections.web_complete, self.completion(original))
        self.assertEqual(self.app.list_integrations()["connections"][0]["account_id"], 1)
        stale = dict(original, intent_id=uuid.uuid4().hex, expected_authorization_revision=1)
        self.fault("integration_connection_changed", self.app.connections.web_begin, stale)

    def test_reauthorization_preserves_stable_connection_and_repository_pins(self):
        item = self.connect()
        self.app.disconnect_integration(item["id"], 1)
        self.config.github = GitHubConfig(repositories=["Example/repo", "Example/other"])
        original = self.begin(expected_connection_id=item["id"], expected_authorization_revision=2)
        result = self.app.connections.web_complete(self.completion(original))
        self.assertEqual((result["connection_id"], result["authorization_revision"]), (item["id"], 3))
        retained = connection_state._read(self.state).connections[item["id"]]
        self.assertEqual(retained.policy.repositories, ["Example/repo"])
        self.fault("github_not_allowed", self.app.github_read, "repository", "Example/other",
                   connection_id=item["id"])

    def test_cancel_is_idempotent_and_cannot_revive_authorization(self):
        original = self.begin()
        request = dict(v=1, intent_id=original["intent_id"])
        first = self.app.connections.web_cancel(request)
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(self.app.connections.web_cancel(request), first)
        self.fault("invalid_github_web_request", self.app.connections.web_cancel, dict(request, v=True))
        self.fault("invalid_github_web_request", self.app.connections.web_cancel, dict(request, extra=True))
        self.fault("github_web_intent_consumed", self.app.connections.web_complete, self.completion(original))
        self.assertEqual(self.app.list_integrations()["connections"], [])

    def test_expiry_uses_original_ten_minute_boundary(self):
        original = self.begin()
        status = self.app.connections.web_status(original["intent_id"])
        retained = connection_state._read(self.state).web_intents[original["intent_id"]]
        self.assertEqual(status["expires_at_ms"] - retained.created_at_ms, 600000)
        with patch.object(web_authorization, "now_ms", return_value=status["expires_at_ms"]):
            self.fault("github_web_intent_expired", self.app.connections.web_complete, self.completion(original))
            self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "expired")
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "expired")

    def test_observed_expiry_cannot_revive_after_clock_rollback(self):
        original = self.begin()
        request = self.completion(original)
        expires = self.app.connections.web_status(original["intent_id"])["expires_at_ms"]
        with patch.object(web_authorization, "now_ms", return_value=expires):
            expired = self.app.connections.web_status(original["intent_id"])
        self.assertEqual((expired["status"], expired["error_code"]), ("expired", "github_web_intent_expired"))
        self.assertEqual(connection_state._read(self.state).web_intents[original["intent_id"]].status, "expired")
        self.assertEqual(self.app.connections.web_status(original["intent_id"]), expired)
        with patch.object(auth, "read_actor") as actor:
            self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
            actor.assert_not_called()

    def test_successful_lost_response_and_duplicate_claim_never_reimport(self):
        item, original, request = self.web_connect()
        with patch.object(auth, "read_actor") as actor, patch.object(credentials, "save") as save:
            self.assertEqual(self.app.connections.web_status(original["intent_id"])["connection_id"], item["id"])
            self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
            second = self.begin()
            reused = self.completion(second, claim_id=request["claim_id"])
            self.fault("github_web_claim_consumed", self.app.connections.web_complete, reused)
            actor.assert_not_called()
            save.assert_not_called()
        self.assertEqual(self.app.connections.web_status(second["intent_id"])["status"], "pending")

    def test_original_ids_cannot_cross_device_intent_and_claim_families(self):
        device = self.connect()
        device_id = connection_state._read(self.state).connections[device["id"]].credential_ref
        original = self.begin()
        another = self.begin()
        with patch.object(auth, "read_actor") as actor:
            self.fault("integration_request_conflict", self.app.connections.web_begin,
                       dict(original, intent_id=device_id))
            for claim_id in (device_id, another["intent_id"]):
                self.fault("github_web_claim_consumed", self.app.connections.web_complete,
                           self.completion(original, claim_id=claim_id))
            actor.assert_not_called()
        self.app.disconnect_integration(device["id"], 1)
        approved = self.begin(expected_connection_id=device["id"], expected_authorization_revision=2)
        complete = self.completion(approved)
        self.app.connections.web_complete(complete)
        self.start.reset_mock()
        for identity in (approved["intent_id"], complete["claim_id"]):
            self.fault("integration_request_conflict", self.app.connect_integration, identity)
        self.fault("integration_request_conflict", self.app.connections.web_begin,
                   dict(original, intent_id=complete["claim_id"]))
        self.start.assert_not_called()

    def test_cancel_during_actor_read_prevents_late_credential_publication(self):
        original = self.begin()
        request = self.completion(original)
        entered, release = threading.Event(), threading.Event()
        read_actor = auth.read_actor

        def blocked(token):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Fixture actor was not released")
            return read_actor(token)

        with ThreadPoolExecutor(max_workers=1) as workers, patch.object(auth, "read_actor", side_effect=blocked):
            pending = workers.submit(self.app.connections.web_complete, request)
            try:
                self.assertTrue(entered.wait(3))
                self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
                self.app.connections.web_cancel(dict(v=1, intent_id=original["intent_id"]))
            finally:
                release.set()
            self.fault("github_web_intent_consumed", pending.result, 3)
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "cancelled")
        self.assertEqual(self.app.list_integrations()["connections"], [])

    def test_shutdown_drains_dispatched_completion_before_releasing_owner(self):
        original = self.begin()
        request = self.completion(original)
        entered, release = threading.Event(), threading.Event()
        read_actor = auth.read_actor
        results, failures = {}, []

        def blocked(token):
            entered.set()
            if not release.wait(6):
                raise AssertionError("Fixture actor was not released")
            return read_actor(token)

        def run(name, operation):
            try:
                results[name] = operation()
            except BaseException as error:
                failures.append(error)

        completion = threading.Thread(target=run, args=("completion",
            lambda: self.app.connections.web_complete(request)), daemon=True)
        shutdown = threading.Thread(target=run, args=("shutdown", self.stop), daemon=True)
        with patch.object(auth, "read_actor", side_effect=blocked):
            completion.start()
            try:
                self.assertTrue(entered.wait(3))
                shutdown.start()
                deadline = time.monotonic() + 3
                while self.app.list_integrations()["renewal"]["running"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(self.app.list_integrations()["renewal"]["running"])
                shutdown.join(0.05)
                self.assertTrue(shutdown.is_alive(), "Shutdown must wait for the dispatched completion")
                with self.assertRaises(Fault) as caught:
                    with lease(self.state / "locks/connections-owner.lock"):
                        pass
                self.assertEqual(caught.exception.code, "resource_busy")
            finally:
                release.set()
                completion.join(3)
                if shutdown.ident is not None:
                    shutdown.join(3)
        self.assertFalse(completion.is_alive())
        self.assertFalse(shutdown.is_alive())
        if failures:
            raise failures[0]
        self.assertEqual(results["completion"]["status"], "connected")
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "connected")
        with lease(self.state / "locks/connections-owner.lock"):
            pass

    def test_pending_approval_and_core_identity_survive_restart(self):
        original = self.begin()
        before = self.app.connections.web_status(original["intent_id"])
        self.restart()
        self.assertEqual(self.app.connections.web_identity()["core_binding"], original["core_binding"])
        self.assertEqual(self.app.connections.web_status(original["intent_id"]), before)
        self.assertEqual(self.app.connections.web_complete(self.completion(original))["status"], "connected")

    def test_restart_after_consumption_requires_new_authorization(self):
        class Interrupted(BaseException):
            pass

        original = self.begin()
        request = self.completion(original)
        with patch.object(auth, "read_actor", side_effect=Interrupted), self.assertRaises(Interrupted):
            self.app.connections.web_complete(request)
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "claimed")
        self.restart()
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "outcome_unknown")
        with patch.object(auth, "read_actor") as actor:
            self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
            actor.assert_not_called()
        self.assertEqual(self.app.list_integrations()["connections"], [])

    def test_provider_denial_offline_and_unknown_response_consume_claim(self):
        for code in ("github_access_denied", "github_network_unavailable", "github_response_unknown"):
            with self.subTest(code=code):
                original = self.begin()
                request = self.completion(original)
                with patch.object(auth, "read_actor", side_effect=Fault(code, "Fixture provider failure")) as actor:
                    self.fault(code, self.app.connections.web_complete, request)
                    self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
                    actor.assert_called_once()
                self.assert_secrets_absent(request, self.app.connections.web_status(original["intent_id"]))
        self.assertEqual(self.app.list_integrations()["connections"], [])

    def test_credential_write_failure_is_not_connected_or_replayed(self):
        original = self.begin()
        request = self.completion(original)
        failure = Fault("integration_credential_store_unavailable", "Fixture persistence failure")
        with patch.object(credentials, "save", side_effect=failure) as save:
            self.fault(failure.code, self.app.connections.web_complete, request)
            self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
            save.assert_called_once()
        self.assertEqual(self.app.connections.web_status(original["intent_id"])["status"], "outcome_unknown")
        self.assertEqual(self.app.list_integrations()["connections"], [])

    def test_reservation_persistence_failure_never_dispatches_or_replays(self):
        for published in (False, True):
            with self.subTest(published=published):
                original = self.begin()
                request = self.completion(original)
                save = self.app.connections._store.save
                failed = False

                def fail_once(value):
                    nonlocal failed
                    if not failed:
                        failed = True
                        if published:
                            save(value)
                        raise Fault("integration_store_unavailable", "Fixture reservation persistence failure")
                    return save(value)

                with patch.object(self.app.connections._store, "save", side_effect=fail_once), \
                        patch.object(auth, "read_actor") as actor, patch.object(credentials, "save") as credential_save:
                    self.fault("integration_store_unavailable", self.app.connections.web_complete, request)
                    expected = "outcome_unknown" if published else "rejected"
                    status = self.app.connections.web_status(original["intent_id"])
                    self.assertEqual((status["status"], status["error_code"]),
                                     (expected, "integration_store_unavailable"))
                    self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
                    self.restart()
                    self.assertEqual(self.app.connections.web_status(original["intent_id"]), status)
                    self.fault("github_web_intent_consumed", self.app.connections.web_complete, request)
                    actor.assert_not_called()
                    credential_save.assert_not_called()
                self.assertEqual(self.app.list_integrations()["connections"], [])

    def test_changed_revision_after_credential_write_does_not_overwrite_binding(self):
        item = self.connect()
        self.app.disconnect_integration(item["id"], 1)
        original = self.begin(expected_connection_id=item["id"], expected_authorization_revision=2)
        request = self.completion(original)
        save = credentials.save

        def changed(*args):
            save(*args)
            with self.app.connections._store.writing() as value:
                value.connections[item["id"]].authorization_revision += 1
                self.app.connections._store.save(value)

        with patch.object(credentials, "save", side_effect=changed):
            self.fault("integration_connection_changed", self.app.connections.web_complete, request)
        retained = connection_state._read(self.state).connections[item["id"]]
        self.assertEqual((retained.status, retained.authorization_revision), ("disconnected", 3))
        self.assertNotEqual(retained.credential_ref, original["intent_id"])
        self.fault("integration_credential_missing", credentials.load, self.state, original["intent_id"], "file")

    def test_same_github_account_on_two_cores_has_independent_credentials(self):
        item, original, request = self.web_connect()
        other_state = self.root / "other-core"
        other = create_application(self.config, other_state)
        with other.connections.running():
            binding = other.connections.web_identity()["core_binding"]
            self.assertNotEqual(binding, original["core_binding"])
            self.fault("github_web_intent_not_found", other.connections.web_complete, request)
            other_original = dict(v=1, intent_id=uuid.uuid4().hex, core_binding=binding)
            other.connections.web_begin(other_original)
            other_request = self.completion(other_original)
            result = other.connections.web_complete(other_request)
            self.assertEqual(result["connection_id"], item["id"])
            other_item = connection_state._read(other_state).connections[item["id"]]
            pair = credentials.load(other_state, other_item.credential_ref, other_item.credential_store)
            self.assertNotEqual(pair.access_token, request["tokens"]["access_token"])
            self.app.disconnect_integration(item["id"], 1)
            self.assertEqual(other.list_integrations()["connections"][0]["status"], "connected")

    def test_unexpired_web_token_remains_usable_without_cloud(self):
        item, _, _ = self.web_connect()
        self.expire_access(item)  # One second remains: Device Flow would proactively refresh.
        with patch("integrations.github.web_broker.refresh", side_effect=Fault("github_network_unavailable", "Offline")) as broker:
            self.assertEqual(self.read_repository(item)["data"]["full_name"], "Example/repo")
            broker.assert_not_called()
        self.refresh.assert_not_called()

    def test_web_rotation_is_reserved_and_persisted_before_business_use(self):
        item, _, _ = self.web_connect()
        old_item, old_pair = self.stored(item)
        self.expire_web(item)
        returned = []

        def broker(state, **request):
            pending = credentials.load(state, old_item.credential_ref, old_item.credential_store)
            self.assertEqual((pending.phase, pending.refresh_id), ("refreshing", request["refresh_id"]))
            self.assertEqual(request["expected_authorization_revision"], item["authorization_revision"])
            pair = self.rotated(state, **request)
            returned.append(pair)
            return pair

        with patch("integrations.github.web_broker.refresh", side_effect=broker) as refresh:
            self.read_repository(item)
            refresh.assert_called_once()
        retained, pair = self.stored(item)
        self.assertEqual(pair.phase, "ready")
        self.assertEqual(pair.refresh_id, returned[0].refresh_id)
        self.assertNotEqual(pair.refresh_token, old_pair.refresh_token)
        self.assertEqual(pair.access_token, returned[0].access_token)
        self.assertEqual(retained.authorization_revision, item["authorization_revision"])
        self.refresh.assert_not_called()

    def test_uncertain_business_write_is_never_replayed_or_refreshed(self):
        item, _, _ = self.web_connect()
        send = self.app.connections.request("Example/repo", connection_id=item["id"], write=True)
        failure = Fault("github_authentication_required", "Fixture response was lost", effect="effect_unknown")
        with patch.object(auth, "request", side_effect=failure) as request, \
                patch("integrations.github.web_broker.refresh") as broker:
            with self.assertRaises(Fault) as caught:
                send("POST", "repos/Example/repo/issues/1/comments", {"body": "Fixture write"})
            self.assertEqual(caught.exception.effect, "effect_unknown")
            request.assert_called_once()
            broker.assert_not_called()
        self.refresh.assert_not_called()

    def failed_rotation(self, failure):
        item, _, _ = self.web_connect()
        self.expire_web(item)
        with patch("integrations.github.web_broker.refresh", side_effect=failure) as broker:
            self.fault("github_reauthorization_required", self.read_repository, item)
            self.restart()
            self.fault("integration_disconnected", self.read_repository, item)
            broker.assert_called_once()
        retained, pair = self.stored(item)
        self.assertEqual((retained.status, pair.phase), ("reauthorization_required", "refreshing"))
        self.assertIsNotNone(pair.refresh_id)
        self.refresh.assert_not_called()

    def test_cloud_offline_during_refresh_requires_reauthorization(self):
        self.failed_rotation(Fault("github_network_unavailable", "Fixture Cloud is offline"))

    def test_lost_refresh_response_is_never_replayed_after_restart(self):
        self.failed_rotation(Fault("github_response_unknown", "Fixture exchange response was lost"))

    def test_revoked_refresh_grant_never_falls_back_to_device_flow(self):
        self.failed_rotation(Fault("github_reauthorization_required", "Fixture grant was revoked"))

    def test_mismatched_rotation_fields_never_publish_a_ready_credential(self):
        mutations = (lambda pair, old: pair.model_copy(update={"refresh_id": "f" * 32}),
                     lambda pair, old: pair.model_copy(update={"core_binding": "a" * 64}),
                     lambda pair, old: pair.model_copy(update={"refresh_token": old.refresh_token}))
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                saved = self.app.list_integrations()["connections"]
                if saved:
                    previous = saved[0]
                    original = self.begin(expected_connection_id=previous["id"],
                                          expected_authorization_revision=previous["authorization_revision"])
                    self.app.connections.web_complete(self.completion(original))
                    item = self.app.list_integrations()["connections"][0]
                else:
                    item, _, _ = self.web_connect()
                _, old = self.stored(item)
                self.expire_web(item)
                with patch("integrations.github.web_broker.refresh",
                           side_effect=lambda state, **request: mutate(self.rotated(state, **request), old)) as broker:
                    self.fault("github_reauthorization_required", self.read_repository, item)
                    self.fault("integration_disconnected", self.read_repository, item)
                    broker.assert_called_once()
                self.assertEqual(self.stored(item)[1].phase, "refreshing")

    def test_refreshed_actor_must_match_saved_github_account(self):
        item, _, _ = self.web_connect()
        self.expire_web(item)
        self.actor = {"id": 2, "login": "another-account"}
        with patch("integrations.github.web_broker.refresh", side_effect=self.rotated) as broker:
            self.fault("github_reauthorization_required", self.read_repository, item)
            self.fault("integration_disconnected", self.read_repository, item)
            broker.assert_called_once()
        retained, pair = self.stored(item)
        self.assertEqual(retained.account_id, 1)
        self.assertEqual((retained.status, pair.phase), ("reauthorization_required", "unverified"))

    def test_rotated_pair_save_failure_preserves_unusable_original_operation(self):
        item, _, _ = self.web_connect()
        self.expire_web(item)
        save = credentials.save

        def failed(state, identity, store, pair):
            if pair.phase == "unverified":
                raise Fault("integration_credential_store_unavailable", "Fixture rotated pair was not committed")
            return save(state, identity, store, pair)

        with patch("integrations.github.web_broker.refresh", side_effect=self.rotated) as broker, \
                patch.object(credentials, "save", side_effect=failed):
            self.fault("github_reauthorization_required", self.read_repository, item)
            self.fault("integration_disconnected", self.read_repository, item)
            broker.assert_called_once()
        self.assertEqual(self.stored(item)[1].phase, "refreshing")

    def test_locked_store_at_startup_preserves_binding_and_recovers_after_unlock(self):
        item, _, _ = self.web_connect()
        binding = connection_state._read(self.state).connections[item["id"]].binding()
        saved = (self.state / "connections.json").read_bytes()
        unavailable = Fault("integration_credential_store_unavailable", "Fixture store is temporarily locked")
        with patch("integrations.github.web_broker.refresh") as broker:
            for retry in ("request", "restart"):
                with self.subTest(retry=retry):
                    with patch.object(credentials, "load", side_effect=unavailable), \
                            patch.object(auth, "request") as provider:
                        self.restart()
                        listing = self.app.list_integrations()
                        self.assertEqual(listing["renewal"]["errors"].get(item["id"]), unavailable.code)
                        self.assertEqual((listing["connections"][0]["status"],
                                          listing["connections"][0]["authorization_revision"]),
                                         ("connected", binding["authorization_revision"]))
                        transport = self.app.connections.request("Example/repo", binding=binding)
                        with self.assertRaises(Fault) as caught:
                            transport("GET", "user")
                        self.assertEqual((caught.exception.code, caught.exception.effect),
                                         (unavailable.code, "known_no_effect"))
                        provider.assert_not_called()
                    if retry == "request":
                        self.assertEqual(transport("GET", "user"), self.actor)
                    else:
                        # Reenter the same owner so the old diagnostic must be cleared.
                        self.stop()
                        self.run_owner()
                    listing = self.app.list_integrations()
                    self.assertNotIn(item["id"], listing["renewal"]["errors"])
                    self.assertEqual(self.app.connections.select(binding=binding).binding(), binding)
                    self.assertEqual(listing["connections"][0]["status"], "connected")
                    self.assertTrue((self.state / "connections.json").read_bytes() == saved)
            broker.assert_not_called()

    def test_unlock_rejects_device_source_in_original_web_slot_before_io(self):
        item, _, _ = self.web_connect()
        for expired in (False, True):
            with self.subTest(expired=expired):
                retained, pair = self.stored(item)
                substitute = pair.model_copy(update={"source": "device_code", "core_binding": None,
                    "refresh_id": None, "access_expires_at_ms": connection_state.now_ms() - 1 if expired else pair.access_expires_at_ms})
                credentials.save(self.state, retained.credential_ref, retained.credential_store, substitute)
                unavailable = Fault("integration_credential_store_unavailable", "Fixture store is temporarily locked")
                with patch.object(auth, "request") as provider, patch.object(auth, "refresh") as direct, \
                        patch("integrations.github.web_broker.refresh") as broker:
                    with patch.object(credentials, "load", side_effect=unavailable):
                        self.restart()
                        listing = self.app.list_integrations()
                        self.assertEqual(listing["connections"][0]["status"], "connected")
                        self.assertEqual(listing["connections"][0]["authorization_revision"], item["authorization_revision"])
                    with self.assertRaises(Fault) as caught:
                        self.read_repository(item)
                    self.assertEqual((caught.exception.code, caught.exception.effect),
                                     ("integration_credential_invalid", "known_no_effect"))
                    self.fault("integration_disconnected", self.read_repository, item)
                    listing = self.app.list_integrations()
                    observed = listing["connections"][0]
                    self.assertEqual((observed["status"], observed["authorization_revision"]),
                                     ("reauthorization_required", item["authorization_revision"] + 1))
                    self.assertEqual(listing["renewal"]["errors"].get(item["id"]), "integration_credential_invalid")
                    provider.assert_not_called()
                    direct.assert_not_called()
                    broker.assert_not_called()
                if not expired:
                    approved = self.begin(expected_connection_id=item["id"],
                        expected_authorization_revision=observed["authorization_revision"])
                    self.app.connections.web_complete(self.completion(approved))
                    item = self.app.list_integrations()["connections"][0]

    def test_interrupted_refresh_record_is_not_dispatched_after_restart(self):
        item, _, _ = self.web_connect()
        retained, pair = self.stored(item)
        credentials.save(self.state, retained.credential_ref, retained.credential_store,
                         pair.model_copy(update={"phase": "refreshing", "refresh_id": uuid.uuid4().hex}))
        unavailable = Fault("integration_credential_store_unavailable", "Fixture store is temporarily locked")
        with patch.object(credentials, "load", side_effect=unavailable):
            self.restart()
            listing = self.app.list_integrations()
            self.assertEqual(listing["connections"][0]["status"], "connected")
            self.assertEqual(listing["connections"][0]["authorization_revision"], item["authorization_revision"])
            self.assertEqual(listing["renewal"]["errors"].get(item["id"]), unavailable.code)
        self.restart()
        listing = self.app.list_integrations()["connections"][0]
        self.assertEqual(listing["status"], "reauthorization_required")
        self.assertEqual(listing["authorization_revision"], item["authorization_revision"] + 1)
        with patch("integrations.github.web_broker.refresh") as broker:
            self.fault("integration_disconnected", self.read_repository, item)
            broker.assert_not_called()

    def test_refresh_id_collision_with_original_authorization_never_dispatches(self):
        item = self.connect()
        device_id = connection_state._read(self.state).connections[item["id"]].credential_ref
        self.app.disconnect_integration(item["id"], item["authorization_revision"])
        original = self.begin(expected_connection_id=item["id"], expected_authorization_revision=2)
        complete = self.completion(original)
        self.app.connections.web_complete(complete)
        for index, identity in enumerate((device_id, original["intent_id"], complete["claim_id"])):
            with self.subTest(family=index):
                item = self.app.list_integrations()["connections"][0]
                self.expire_web(item)
                with patch.object(web_authorization.secrets, "token_hex", return_value=identity), \
                        patch("integrations.github.web_broker.refresh") as broker:
                    self.fault("github_reauthorization_required", self.read_repository, item)
                    broker.assert_not_called()
                self.assertEqual(self.stored(item)[1].phase, "ready")
                if index < 2:
                    retained = self.app.list_integrations()["connections"][0]
                    renewed = self.begin(expected_connection_id=item["id"],
                        expected_authorization_revision=retained["authorization_revision"])
                    self.app.connections.web_complete(self.completion(renewed))

    def test_refresh_id_cannot_reuse_previous_successful_rotation(self):
        item, _, _ = self.web_connect()
        self.expire_web(item)
        with patch("integrations.github.web_broker.refresh", side_effect=self.rotated):
            self.read_repository(item)
        _, pair = self.stored(item)
        self.expire_web(item)
        with patch.object(web_authorization.secrets, "token_hex", return_value=pair.refresh_id), \
                patch("integrations.github.web_broker.refresh") as broker:
            self.fault("github_reauthorization_required", self.read_repository, item)
            broker.assert_not_called()
        self.assertEqual(self.stored(item)[1].refresh_id, pair.refresh_id)

    def test_legacy_device_credential_without_source_uses_direct_refresh(self):
        item = self.connect()
        retained, pair = self.stored(item)
        legacy = pair.model_dump()
        for key in ("source", "core_binding", "refresh_id"):
            legacy.pop(key, None)
        legacy["access_expires_at_ms"] = connection_state.now_ms() + 1000
        save_json(self.state / "credentials/github" / (retained.credential_ref + ".json"), legacy)
        with patch("integrations.github.web_broker.refresh") as broker:
            self.read_repository(item)
            broker.assert_not_called()
        self.refresh.assert_called_once()
        self.assertEqual(self.stored(item)[1].source, "device_code")

    def test_legacy_store_fingerprint_and_new_authority_are_both_preserved(self):
        for version in (1, 2):
            legacy = {"format": version, "connections": {}, "attempts": {}}
            with self.app.connections._store.writing():
                save_json(self.state / "connections.json", legacy)
            expected = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
            before = (self.state / "connections.json").read_bytes()
            self.assertEqual(connection_state.check_state(self.state)["connections_sha256"], expected)
            self.assertEqual((self.state / "connections.json").read_bytes(), before)
        original = self.begin()
        snapshot = connection_state.check_state(self.state)
        self.assertEqual(snapshot["connections_format"], 3)
        self.assertNotEqual(snapshot["connections_sha256"], expected)
        self.app.connections.web_cancel(dict(v=1, intent_id=original["intent_id"]))
        self.assertNotEqual(connection_state.check_state(self.state)["connections_sha256"], snapshot["connections_sha256"])

    def test_pending_approval_is_restartable_but_claimed_completion_defers_activation(self):
        class Interrupted(BaseException):
            pass

        original = self.begin()
        before = (self.state / "connections.json").read_bytes()
        self.assertEqual(connection_state.check_state(self.state, require_idle=True)["connections_format"], 3)
        self.assertEqual((self.state / "connections.json").read_bytes(), before)
        with patch.object(auth, "read_actor", side_effect=Interrupted), self.assertRaises(Interrupted):
            self.app.connections.web_complete(self.completion(original))
        claimed = (self.state / "connections.json").read_bytes()
        self.fault("activation_deferred", connection_state.check_state, self.state, require_idle=True)
        self.assertEqual((self.state / "connections.json").read_bytes(), claimed)

    def test_candidate_reader_must_preserve_web_authority_and_contract(self):
        self.begin()
        config = self.root / "config.toml"
        config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        proof = checks.check_state(config, self.state)
        self.assertEqual(proof["state_contract"], STATE_CONTRACT)
        self.assertGreaterEqual(proof["state_contract"], 23)
        self.assertEqual(proof["connections_format"], 3)
        before = (self.state / "connections.json").read_bytes()
        changes = ({"state_contract": 22}, {"connections_present": False},
                   {"connections_format": 2}, {"connections_sha256": "a" * 64})
        for change in changes:
            with self.subTest(change=change), patch.object(processes, "run", return_value=
                    subprocess.CompletedProcess([], 0, json.dumps(proof | change), "")):
                self.fault("incompatible_state", checks._candidate_state,
                           Path(sys.executable), config, self.state)
        with patch.object(processes, "run", return_value=
                subprocess.CompletedProcess([], 0, json.dumps(proof), "")):
            self.assertEqual(checks._candidate_state(Path(sys.executable), config, self.state), proof)
        self.assertEqual((self.state / "connections.json").read_bytes(), before)
