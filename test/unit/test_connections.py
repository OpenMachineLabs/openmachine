"""Native connection behavior using existing task fixtures and a small provider fake."""

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

from integrations import connections, credentials
from integrations.github import auth, connection
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.config import GitHubConfig
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import checks
from openmachine.tasks import records
from support_tasks import TaskFixture


class ConnectionTests(TaskFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = self.config.model_copy(update={"github": GitHubConfig(repositories=["Example/repo"])})
        self.enterContext(patch.object(auth, "CLIENT_ID", "Iv1.fixture"))
        self.enterContext(patch.object(credentials, "select", return_value="file"))
        self.app = create_application(self.config, self.state)
        self.actor = {"id": 1, "login": "operator"}
        self.calls, self.tokens, self.comment = [], {}, None
        self.owner = None
        self.device = dict(device_code="private-fixture-device-code", user_code="ABCD-EFGH",
                           verification_uri=auth.VERIFY_URL, expires_in=900, interval=0.01)
        self.start = self.enterContext(patch.object(auth, "start_device", return_value=self.device))
        self.poll = self.enterContext(patch.object(auth, "poll_device", side_effect=lambda *a: (self.pair(), a[-1])))
        self.refresh = self.enterContext(patch.object(auth, "refresh", side_effect=lambda *a: self.pair()))
        self.enterContext(patch.object(auth, "request", side_effect=self.api))
        self.enterContext(patch.object(connection, "request", side_effect=lambda m, e, p=None: self.api("legacy", m, e, p)))
        self.addCleanup(self.stop)

    def pair(self):
        value = credentials.Credential(account_id=0, phase="unverified",
            access_token="access-fixture-" + uuid.uuid4().hex, refresh_token="refresh-fixture-" + uuid.uuid4().hex,
            access_expires_at_ms=connections.now_ms() + 28800000,
            refresh_expires_at_ms=connections.now_ms() + 15897600000)
        self.tokens[value.access_token] = dict(self.actor)
        return value

    def api(self, token, method, endpoint, payload=None):
        self.calls.append((method, endpoint))
        actor = {"id": 1, "login": "operator"} if token == "legacy" else self.tokens[token]
        if endpoint == "user":
            return dict(actor)
        if endpoint == "repos/Example/repo":
            return {"full_name": "Example/repo", "private": True, "permissions": {"push": True}}
        if endpoint == "repos/Example/repo/issues/1":
            return {"number": 1}
        if method == "POST" and endpoint == "repos/Example/repo/issues/1/comments":
            self.comment = dict(id=50, body=payload["body"], user=dict(actor),
                                issue_url="https://api.github.com/repos/Example/repo/issues/1")
            return dict(self.comment)
        if endpoint == "repos/Example/repo/issues/comments/50":
            return dict(self.comment)
        if endpoint.startswith("repos/Example/repo/issues/1/comments?"):
            return [dict(self.comment)] if self.comment else []
        raise AssertionError((method, endpoint))

    def run_owner(self):
        if self.owner is None:
            self.owner = self.app.connections.running()
            self.owner.__enter__()

    def stop(self):
        if self.owner:
            self.owner.__exit__(None, None, None)
            self.owner = None

    def result(self, identity):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.app.read_integration_login(identity)
            if result["status"] != "pending":
                return result
            time.sleep(0.01)
        self.fail("The fixture login did not complete")

    def connect(self, **kwargs):
        self.run_owner()
        identity = uuid.uuid4().hex
        self.app.connect_integration(identity, **kwargs)
        result = self.result(identity)
        self.assertEqual(result["status"], "succeeded", result)
        return self.app.list_integrations(connection_id=result["connection_id"])["connections"][0]

    def stored(self, item):
        value = connections._read(self.state).connections[item["id"]]
        return value, credentials.load(self.state, value.credential_ref, value.credential_store)

    def expire_access(self, item):
        value, pair = self.stored(item)
        credentials.save(self.state, value.credential_ref, value.credential_store,
                         pair.model_copy(update={"access_expires_at_ms": connections.now_ms() + 1000}))
        return value

    def test_catalog_is_passive_and_disabled_provider_stays_disabled(self):
        self.assertEqual(self.app.list_integrations()["connections"], [])
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
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.app.connect_integration(identity), range(2)))
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
        self.fault("integration_disconnected", send, "GET", "repos/Example/repo")
        self.assertFalse(self.calls)

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
        raw = self.path.read_bytes()
        self.assertEqual(old["format"], 1)
        self.assertEqual(records.canonical(records.Record.model_validate(old).model_dump()), raw)
        self.control("claim", handoff_id=old["handoff"]["id"])
        self.actor = {"id": 2, "login": "other"}
        native = self.connect()
        self.fault("task_scope_denied", self.app.github_comment, "create", "Example/repo", "Fixture.", 1,
                   connection_id=native["id"], task_context=self.context_for("c" * 32))
        self.assertNotIn("github_connections", self.view()["record"]["contract"])

    def test_real_state_readers_must_understand_native_authority(self):
        self.connect()
        config = self.root / "config.toml"
        config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        proof = checks.check_state(config, self.state)
        self.assertEqual(proof["state_contract"], 14)
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


class GitHubDeviceWireTests(unittest.TestCase):
    def test_device_intervals_and_definitive_denials(self):
        with patch.object(auth, "CLIENT_ID", "Iv1.fixture"), patch.object(
                auth, "_post", return_value={"error": "slow_down", "interval": 75}):
            self.assertEqual(auth.poll_device("private-device", 65), (None, 75))
        for provider, expected in (("access_denied", "github_access_denied"), ("expired_token", "integration_login_expired")):
            with patch.object(auth, "CLIENT_ID", "Iv1.fixture"), patch.object(
                    auth, "_post", return_value={"error": provider}), self.assertRaises(Fault) as caught:
                auth.poll_device("private-device", 5)
            self.assertEqual(caught.exception.code, expected)

    def test_device_refresh_requires_no_client_secret_and_keeps_both_deadlines(self):
        result = dict(token_type="bearer", scope="", access_token="access-fixture-000000", refresh_token="refresh-fixture-000000",
                      expires_in=28800, refresh_token_expires_in=15897600)
        with patch.object(auth, "CLIENT_ID", "Iv1.fixture"), patch.object(auth, "_post", return_value=result) as post:
            pair = auth.refresh("original-refresh")
        self.assertEqual(post.call_args.args[1], dict(client_id="Iv1.fixture", grant_type="refresh_token", refresh_token="original-refresh"))
        self.assertGreater(pair.refresh_expires_at_ms, pair.access_expires_at_ms)
        self.assertEqual(pair.phase, "unverified")
