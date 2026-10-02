"""Connection authority checks using existing disposable task fixtures."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
import uuid

from integrations import connections
from integrations.github import connection
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.config import GitHubConfig
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import save_json
from openmachine.service import checks
from openmachine.tasks import records
from support_tasks import TaskFixture


class ConnectionTests(TaskFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config = self.config.model_copy(update={"github": GitHubConfig(repositories=["Example/repo"])})
        self.app = create_application(self.config, self.state)
        self.actor = {"id": 1, "login": "operator"}
        self.calls, self.comment = [], None
        self.mock = patch.object(connection, "request", side_effect=self.api)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def api(self, method, endpoint, payload=None):
        self.calls.append((method, endpoint))
        if method == "GET" and endpoint == "user":
            return {**self.actor, "unrelated_private_field": "must-not-persist"}
        if method == "GET" and endpoint == "repos/Example/repo":
            return {"full_name": "Example/repo", "private": True, "permissions": {"push": True}}
        if method == "GET" and endpoint == "repos/Example/repo/issues/1":
            return {"number": 1}
        if method == "POST" and endpoint == "repos/Example/repo/issues/1/comments":
            self.comment = {"id": 50, "body": payload["body"], "user": dict(self.actor),
                            "issue_url": "https://api.github.com/repos/Example/repo/issues/1"}
            return dict(self.comment)
        if method == "GET" and endpoint == "repos/Example/repo/issues/comments/50":
            return dict(self.comment)
        if method == "GET" and endpoint.startswith("repos/Example/repo/issues/1/comments?"):
            return [dict(self.comment)] if self.comment else []
        raise AssertionError((method, endpoint))

    def connect(self, **kwargs):
        result = self.app.connect_integration(uuid.uuid4().hex, **kwargs)
        return self.app.list_integrations(connection_id=result["connection_id"])["connections"][0]

    def test_catalog_is_passive_and_operator_disable_is_preserved(self):
        self.assertEqual(self.app.list_integrations()["connections"], [])
        self.assertFalse(self.app.get_capabilities()["connections"]["native_login"])
        self.assertFalse(self.state.exists())
        disabled = create_application(self.config.model_copy(update={"github": None}), self.state)
        self.fault("github_not_allowed", disabled.connect_integration, "1" * 32)
        self.assertFalse(self.calls)

    def test_attach_persists_only_metadata_and_restart_needs_no_login(self):
        item = self.connect()
        path = self.state / "connections.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("must-not-persist", path.read_text())
        self.calls.clear()
        restarted = create_application(self.config, self.state)
        self.assertEqual(restarted.list_integrations()["connections"], [item])
        self.assertFalse(self.calls)
        self.assertEqual(restarted.github_read("repository", "Example/repo")["actor"], self.actor)

    def test_attach_request_replay_and_conflicting_intent(self):
        identity = uuid.uuid4().hex
        first = self.app.connect_integration(identity)
        before = (self.state / "connections.json").read_bytes()
        self.calls.clear()
        self.assertEqual(self.app.connect_integration(identity), first)
        self.fault("integration_request_conflict", self.app.connect_integration, identity, method="device_code")
        self.assertFalse(self.calls)
        self.assertEqual((self.state / "connections.json").read_bytes(), before)

    def test_no_native_login_placeholder_or_invalid_id_effect(self):
        self.fault("integration_unsupported", self.app.connect_integration, "1" * 32, method="device_code")
        self.fault("invalid_integration_request", self.app.connect_integration, "invalid")
        self.assertFalse((self.state / "connections.json").exists())
        self.assertFalse(self.calls)

    def test_disconnect_is_local_persistent_and_revision_guarded(self):
        item = self.connect()
        self.fault("integration_connection_changed", self.app.disconnect_integration, item["id"], 99)
        self.calls.clear()
        result = self.app.disconnect_integration(item["id"], item["authorization_revision"])
        self.assertEqual(result["credential_cleanup"], "externally_managed")
        self.assertEqual(self.app.disconnect_integration(item["id"], item["authorization_revision"]), result)
        restarted = create_application(self.config, self.state)
        self.fault("integration_disconnected", restarted.github_read, "repository", "Example/repo")
        self.assertFalse(self.calls)

    def test_reconnect_rejects_another_account_without_overwriting(self):
        item = self.connect()
        self.actor = {"id": 2, "login": "other"}
        self.fault("github_identity_changed", self.app.connect_integration, "2" * 32, connection_id=item["id"])
        self.assertEqual(self.app.list_integrations()["connections"], [item])
        self.assertEqual(self.app.read_integration_login("2" * 32)["status"], "failed")

    def test_multiple_accounts_require_selection_and_never_fall_back(self):
        one = self.connect()
        self.actor = {"id": 2, "login": "other"}
        two = self.connect()
        self.fault("integration_selection_required", self.app.github_read, "repository", "Example/repo")
        self.fault("github_identity_changed", self.app.github_read, "repository", "Example/repo", connection_id=one["id"])
        self.assertEqual(self.app.github_read("repository", "Example/repo", connection_id=two["id"])["actor"], self.actor)

    def test_request_guard_observes_disconnect_before_dispatch(self):
        item = self.connect()
        send = self.app.connections.request("Example/repo")
        self.app.disconnect_integration(item["id"], item["authorization_revision"])
        self.calls.clear()
        self.fault("integration_disconnected", send, "GET", "repos/Example/repo")
        self.assertFalse(self.calls)

    def test_saved_policy_is_not_widened_by_current_configuration(self):
        self.connect()
        wider = self.config.model_copy(update={"github": GitHubConfig(repositories=["Example/repo", "Other/repo"])})
        app = create_application(wider, self.state)
        self.calls.clear()
        self.fault("github_not_allowed", app.github_read, "repository", "Other/repo")
        self.assertFalse(self.calls)

    def test_missing_or_corrupt_state_is_not_reset(self):
        self.connect()
        path = self.state / "connections.json"
        original = path.read_bytes()
        for raw in (b"{}", b'{"format":1}', b'{"format":1,"format":1}', b"[]"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self.fault("integration_store_invalid", self.app.list_integrations)
                self.assertEqual(path.read_bytes(), raw)
        path.write_bytes(original)
        path.chmod(0o644)
        self.fault("integration_store_invalid", self.app.list_integrations)
        path.chmod(0o600)
        path.unlink()
        self.fault("integration_store_invalid", self.app.list_integrations)
        self.assertFalse(path.exists())

    def test_incomplete_attach_is_visible_without_restarting_probe(self):
        self.connect()
        path = self.state / "connections.json"
        value = json.loads(path.read_text())
        attempt = next(iter(value["attempts"].values()))
        attempt.update(status="pending", connection_id=None, error_code=None)
        save_json(path, value)
        raw = path.read_bytes()
        self.calls.clear()
        result = self.app.read_integration_login(next(iter(value["attempts"])))
        self.assertEqual(result["error_code"], "runtime_interrupted")
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(self.calls)

    def test_attach_capacity_preserves_original_receipts(self):
        identity = "1" * 32
        original = self.app.connect_integration(identity)
        with patch.object(connections, "MAX_ATTEMPTS", 1):
            self.fault("integration_capacity", self.app.connect_integration, "2" * 32)
        self.assertEqual(self.app.read_integration_login(identity), original)

    def test_late_reconnect_cannot_undo_disconnect(self):
        item = self.connect()
        self.app.disconnect_integration(item["id"], item["authorization_revision"])
        item = self.app.list_integrations()["connections"][0]
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return dict(self.actor)
        with patch.object(connection, "read_actor", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.app.connect_integration, "3" * 32, connection_id=item["id"])
            self.assertTrue(entered.wait(5))
            try:
                self.app.disconnect_integration(item["id"], item["authorization_revision"])
            finally:
                release.set()
            with self.assertRaises(Fault) as caught:
                future.result(5)
            self.assertEqual(caught.exception.code, "integration_connection_changed")
        self.assertEqual(self.app.list_integrations()["connections"][0]["status"], "disconnected")

    def github_task(self):
        self.request["contract"].update(repositories=["Example/repo"], tools=["github_comment"])
        return self.create()

    def test_new_task_records_resolved_authority_and_original_retry(self):
        result = self.github_task()
        record = result["record"]
        self.assertEqual(record["format"], 2)
        self.assertEqual(record["contract"]["github_connections"]["Example/repo"]["account_id"], 1)
        raw = self.path.read_bytes()
        self.calls.clear()
        self.assertEqual(self.create()["status"], "unchanged")
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(self.calls)

    def test_task_writes_keep_binding_and_original_publication(self):
        record = self.github_task()["record"]
        self.control("claim", handoff_id=record["handoff"]["id"])
        identity = "a" * 32
        args = dict(action="create", repository="Example/repo", number=1, body="Fixture.", task_context=self.context_for(identity))
        self.assertEqual(self.app.github_comment(**args)["status"], "verified")
        stored = self.view()["record"]["operations"][0]
        self.assertEqual(stored["locator"]["connection"], record["contract"]["github_connections"]["Example/repo"])
        count = sum(method == "POST" for method, _ in self.calls)
        self.app.github_comment(**args)
        self.assertEqual(sum(method == "POST" for method, _ in self.calls), count)
        self.actor = {"id": 2, "login": "other"}
        self.fault("github_identity_changed", self.app.github_comment, **dict(args, task_context=self.context_for("b" * 32)))
        self.assertEqual(sum(method == "POST" for method, _ in self.calls), count)

    def test_legacy_task_bytes_and_identity_policy_are_unchanged(self):
        self.request["contract"].update(repositories=["Example/repo"], tools=["github_comment"])
        legacy = tasks.create(self.state, self.request["task_id"], **{k: v for k, v in self.request.items() if k != "task_id"})
        self.assertEqual(legacy["record"]["format"], 1)
        raw = self.path.read_bytes()
        self.assertEqual(records.canonical(records.Record.model_validate(legacy["record"]).model_dump()), raw)
        self.view()
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertNotIn("github_connections", self.view()["record"]["contract"])
        self.control("claim", handoff_id=legacy["record"]["handoff"]["id"])
        item = self.connect()
        self.fault("task_scope_denied", self.app.github_comment, "create", "Example/repo", "Fixture.", 1,
                   connection_id=item["id"], task_context=self.context_for("c" * 32))

    def test_candidate_must_read_retained_connections(self):
        self.connect()
        config = self.root / "config.toml"
        config.write_text('schema_version = 1\n[machines.local]\ntransport = "local"\n')
        proof = checks.check_state(config, self.state)
        self.assertTrue(proof["connections_present"])
        self.assertEqual(proof["state_contract"], 10)
        before = (self.state / "connections.json").read_bytes()
        for change in ({"state_contract": 9}, {"connections_present": False}, {"connections_sha256": "a" * 64}):
            with self.subTest(change=change), patch.object(checks, "_run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(proof | change), "")):
                self.fault("incompatible_state", checks._candidate_state, Path(sys.executable), config, self.state)
        self.assertEqual((self.state / "connections.json").read_bytes(), before)
