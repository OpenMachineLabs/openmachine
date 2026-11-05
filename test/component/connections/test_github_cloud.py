"""Cloud connection metadata never becomes a local GitHub credential."""
import json
from copy import deepcopy
import uuid
import unittest
from unittest.mock import patch

from integrations import connection_state
from integrations.github import cloud
from test.support.connections_cases import ConnectionCaseFixture


class CloudConnectionTests(ConnectionCaseFixture, unittest.TestCase):
    def test_cloud_default_is_pinned_before_task_admission_and_not_reselected_on_retry(self):
        a = self.app.connections.cloud_bind(self.binding())["connection_id"]
        b = self.app.connections.cloud_bind(self.binding(account_id="b" * 32, github_id=2, login="other"))["connection_id"]
        selected = a
        def send(state, connection, repository, write, method, endpoint, payload, scope):
            if endpoint == "user":
                return {"id": connection.account_id, "login": connection.account_login}
            return {"full_name": repository, "permissions": {"push": True}}
        request = deepcopy(self.request)
        request["contract"].update(github_source="cloud", repositories=["Example/repo"], tools=["github_read"], targets=[])
        with patch.object(cloud, "resolve", side_effect=lambda owner: selected) as resolve, patch.object(cloud, "request", side_effect=send):
            original = self.create(request)
            binding = original["record"]["contract"]["github_connections"]["Example/repo"]
            self.assertEqual(binding["connection_id"], a)
            selected = b
            repeated = self.create(request)
            self.assertEqual(repeated["record"]["contract"]["github_connections"]["Example/repo"], binding)
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual(self.app._github_call("read", {"action": "repository", "repository": "Example/repo"}, binding=binding)["actor"]["id"], 1)
            next_request = deepcopy(request); next_request.update(task_id="cloud-default-b", update_id=uuid.uuid4().hex)
            next_task = self.create(next_request)
            self.assertEqual(next_task["record"]["contract"]["github_connections"]["Example/repo"]["connection_id"], b)
            self.assertEqual(resolve.call_count, 2)

    def binding(self, **updates):
        self.run_owner()
        request = dict(v=1, origin="https://mcp.example.com", principal_id="owner", account_id="a" * 32,
            core_binding=self.app.connections.web_identity()["core_binding"], github_id=self.actor["id"],
            login=self.actor["login"], revision=1)
        request.update(updates)
        return request

    def test_binding_is_durable_idempotent_and_has_no_local_credential(self):
        request = self.binding()
        result = self.app.connections.cloud_bind(request)
        self.assertEqual(self.app.connections.cloud_bind(request), result)
        snapshot = self.app.list_integrations(connection_id=result["connection_id"])["connections"][0]
        self.assertEqual(snapshot["backend"], "cloud")
        stored = connection_state._read(self.state)
        item = stored.connections[result["connection_id"]]
        self.assertEqual(stored.format, 4)
        self.assertIsNone(item.credential_store)
        self.assertIsNone(item.credential_ref)
        self.assertNotIn("access_token", json.dumps(snapshot))
        self.assertNotIn("refresh_token", (self.state / "connections.json").read_text())

    def test_cloud_transport_is_explicit_and_never_requests_a_local_token(self):
        result = self.app.connections.cloud_bind(self.binding())
        with patch.object(self.app.connections, "_token", side_effect=AssertionError("local credential read")), \
                patch.object(cloud, "request", return_value={"ok": True}) as send:
            transport = self.app.connections.request("Example/repo", connection_id=result["connection_id"])
            self.assertEqual(transport("GET", "repos/Example/repo"), {"ok": True})
            self.assertEqual(send.call_args.args[2:6], ("Example/repo", False, "GET", "repos/Example/repo"))
        self.fault("integration_authentication_required", self.app.connections.select)

    def test_reauthorization_does_not_replace_an_original_task_binding(self):
        request = self.binding()
        result = self.app.connections.cloud_bind(request)
        original = self.app.connections.select(connection_id=result["connection_id"]).binding()
        self.app.connections.cloud_bind({**request, "revision": 2})
        self.fault("integration_connection_changed", self.app.connections.select, binding=original)
        self.fault("integration_connection_changed", self.app.connections.cloud_bind, request)

    def test_reauthorization_preserves_the_retained_native_repository_policy(self):
        request = self.binding()
        result = self.app.connections.cloud_bind(request)
        self.app.config.github.repositories.append("Example/another")
        self.app.connections.cloud_bind({**request, "revision": 2})
        current = self.app.connections.select(connection_id=result["connection_id"])
        self.assertEqual(current.policy.repositories, ["Example/repo"])
        self.fault("github_not_allowed", self.app.connections.select, "Example/another", connection_id=result["connection_id"])

    def test_core_and_actor_substitution_fail_and_native_disconnect_is_not_undone(self):
        request = self.binding()
        self.fault("github_cloud_binding_changed", self.app.connections.cloud_bind, {**request, "core_binding": "b" * 64})
        result = self.app.connections.cloud_bind(request)
        self.fault("integration_connection_changed", self.app.connections.cloud_bind, {**request, "github_id": request["github_id"] + 1})
        self.app.disconnect_integration(result["connection_id"], 1)
        self.fault("integration_connection_changed", self.app.connections.cloud_bind, request)
        reauthorized = self.app.connections.cloud_bind({**request, "revision": 2})
        self.assertEqual(reauthorized["authorization_revision"], 2)
        self.assertEqual(self.app.connections.select(connection_id=result["connection_id"]).status, "connected")
