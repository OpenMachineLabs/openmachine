"""Private GitHub handoff over the actual owner-only Core Unix listener."""

import asyncio
import http.client
import json
import os
from pathlib import Path
import socket
import unittest
import uuid

from mcp import ClientSession
from test.support.mcp import MCPFixture


class GitHubWebSocketTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    def launch_web(self, extra=""):
        # Only the provider is synthetic; the private socket/owner/HTTP handoff,
        # connection owner, credential store and public readback are real.
        return self.launch(bootstrap=(
            "from integrations import credentials\n"
            "from integrations.github import auth\n"
            "credentials.select = lambda: 'file'\n"
            "auth.read_actor = lambda token: {'id': 71, 'login': 'github-b'}\n" + extra))

    def _request(self, method, action, body=None, *, headers=None, bound=True,
                 peer_directory=None, peer_name=None, peer_mode=0o600, raw=None,
                 after_connect=None):
        peer = Path(peer_directory or self.state) / (peer_name or f"web-{uuid.uuid4().hex}.sock")
        owner = None
        connection = http.client.HTTPConnection("localhost", timeout=5)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if bound:
                client.bind(str(peer))
                os.chmod(peer, peer_mode)
                owner = peer.stat()
            client.settimeout(5)
            client.connect(str(self.socket))
            connection.sock = client
            if after_connect:
                after_connect()
            values = {"Content-Type": "application/json"}
            values.update(headers or {})
            content = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            try:
                connection.request(method, "/internal/github/web/" + action, body=content, headers=values)
            except BrokenPipeError:
                # An early rejection may close the writer before all body bytes
                # are sent. Read that original response; never resend the request.
                pass
            response = connection.getresponse()
            data = response.read(65537)
            self.assertLessEqual(len(data), 65536)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return response.status, json.loads(data)
        finally:
            connection.close()
            client.close()
            if owner is not None and peer.exists():
                current = peer.lstat()
                if (current.st_dev, current.st_ino) == (owner.st_dev, owner.st_ino):
                    peer.unlink()

    async def request(self, method, action, body=None, **kwargs):
        return await asyncio.to_thread(self._request, method, action, body, **kwargs)

    async def identity(self):
        status, value = await self.request("GET", "identity?v=1")
        self.assertEqual(status, 200, value)
        self.assertEqual(set(value), {"v", "core_binding"})
        self.assertRegex(value["core_binding"], r"^[0-9a-f]{64}$")
        return value["core_binding"]

    async def begin(self, binding):
        identity = uuid.uuid4().hex
        request = {"v": 1, "intent_id": identity, "core_binding": binding}
        status, result = await self.request("POST", "begin", request)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "pending")
        return identity, request, result

    @staticmethod
    def complete(identity):
        return {"v": 1, "intent_id": identity, "claim_id": uuid.uuid4().hex,
            "github_account_id": 71, "tokens": {"access_token": "private-web-access-fixture",
                "refresh_token": "private-web-refresh-fixture", "expires_in": 28800,
                "refresh_token_expires_in": 15897600, "token_type": "bearer"}}

    async def test_private_handoff_has_durable_public_readback_and_no_mcp_tool(self):
        process = self.launch_web()
        binding = await self.identity()
        identity, request, pending = await self.begin(binding)
        status, duplicate = await self.request("POST", "begin", request)
        self.assertEqual((status, duplicate), (200, pending))
        complete = self.complete(identity)
        status, result = await self.request("POST", "complete", complete)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(set(result), {"v", "intent_id", "connection_id", "authorization_revision", "status"})
        status, replay = await self.request("POST", "complete", complete)
        self.assertEqual(status, 409)
        self.assertEqual(replay["error"]["code"], "github_web_intent_consumed")
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                catalog = await session.list_tools()
                self.assertFalse(any("web" in tool.name for tool in catalog.tools))
                listing = await session.call_tool("list_integrations", {"provider": "github"})
                self.assertFalse(listing.is_error)
                account = listing.structured_content["connections"][0]
                self.assertEqual(account["id"], result["connection_id"])
                self.assertEqual(account["account_id"], 71)
                for secret in complete["tokens"]["access_token"], complete["tokens"]["refresh_token"]:
                    self.assertNotIn(secret, listing.model_dump_json())
        self.stop(process)
        self.launch_web()
        self.assertEqual(await self.identity(), binding)
        status, retained = await self.request("GET", f"status/{identity}?v=1")
        self.assertEqual(status, 200, retained)
        self.assertEqual(retained["connection_id"], result["connection_id"])
        self.assertEqual(retained["status"], "connected")

    async def test_native_gate_rejects_unbound_proxy_wrong_path_and_unsafe_peer(self):
        self.launch_web()
        for case_id, options in enumerate(({"bound": False},
                        {"bound": False, "headers": {"om.github_web_owner": "true", "X-Local-Owner": "true"}},
                        {"peer_directory": self.root}, {"peer_name": "ordinary.sock"},
                        {"peer_mode": 0o644}), 1):
            with self.subTest(case=case_id, option_names=tuple(sorted(options))):
                status, value = await self.request("GET", "identity?v=1", **options)
                self.assertEqual(status, 403, value)
                self.assertEqual(value["error"]["code"], "github_web_owner_required")
        self.assertFalse((self.state / "connections.json").exists())
        await self.identity()

    async def test_native_uid_check_rejects_other_owner(self):
        self.launch_web("import openmachine.github_web_http as web_http\nimport os\n"
                        "web_http._peer_uid = lambda sock: os.getuid() + 1\n")
        status, value = await self.request("GET", "identity?v=1")
        self.assertEqual(status, 403, value)
        self.assertFalse((self.state / "connections.json").exists())

    async def test_private_routes_keep_current_lifecycle_admission(self):
        self.launch_web("from openmachine import service\n"
                        "from openmachine.foundation.sanitize import Fault\n"
                        "def denied(state):\n"
                        "    raise Fault('service_stopped', 'private-lifecycle-sentinel')\n"
                        "service.guard_writes = denied\n")
        status, value = await self.request("GET", "identity?v=1")
        self.assertEqual(status, 409, value)
        self.assertEqual(value["error"]["code"], "service_stopped")
        self.assertNotIn("private-lifecycle-sentinel", json.dumps(value))
        self.assertFalse((self.state / "connections.json").exists())

    async def test_native_gate_rechecks_original_listener_identity(self):
        self.launch_web()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)

        def replace_listener():
            self.socket.rename(self.state / "old-listener.sock")
            replacement.bind(str(self.socket))
            os.chmod(self.socket, 0o600)

        status, value = await self.request("GET", "identity?v=1", after_connect=replace_listener)
        self.assertEqual(status, 403, value)
        self.assertFalse((self.state / "connections.json").exists())

    async def test_http_schema_origin_limits_and_error_bodies_do_not_echo_tokens(self):
        self.launch_web()
        binding = await self.identity()
        identity = uuid.uuid4().hex
        begin = {"v": 1, "intent_id": identity, "core_binding": binding}
        oversized = b'{"access_token":"private-secret"}'
        oversized += b" " * (32769 - len(oversized))
        cases = [
            ("GET", "identity", None, {}, 400),
            ("GET", "identity?v=1&v=1", None, {}, 400),
            ("GET", "identity?v=1&extra=private-secret", None, {}, 400),
            ("GET", "identity?v=true", None, {}, 400),
            ("GET", "identity?v=1", None, {"headers": {"Origin": "http://localhost"}}, 403),
            ("GET", "identity?v=1", None, {"headers": {"X-Forwarded-For": "127.0.0.1"}}, 403),
            ("GET", "identity?v=1", None, {"headers": {"Host": "untrusted.invalid"}}, 421),
            ("POST", "begin?v=1", begin, {}, 400),
            ("GET", "begin?v=1", None, {}, 405),
            ("POST", "begin", begin, {"headers": {"Content-Type": "text/plain"}}, 415),
            ("POST", "begin", begin | {"v": True}, {}, 409),
            ("POST", "begin", begin | {"v": 1.0}, {}, 409),
            ("POST", "begin", begin | {"private-secret": "private-secret"}, {}, 409),
            ("POST", "begin", None, {"raw": b'{"v":1,"v":1,"access_token":"private-secret"}'}, 400),
            ("POST", "begin", None, {"raw": b'{"v":NaN,"access_token":"private-secret"}'}, 400),
            ("POST", "begin", None, {"raw": oversized}, 413),
            ("POST", "refresh", None, {"raw": b"private-secret"}, 404),
        ]
        for case_id, (method, action, body, options, expected) in enumerate(cases, 1):
            with self.subTest(case=case_id, method=method, expected=expected,
                              option_names=tuple(sorted(options))):
                status, result = await self.request(method, action, body, **options)
                self.assertEqual(status, expected, result)
                self.assertNotIn("private-secret", json.dumps(result))
        self.assertEqual(json.loads((self.state / "connections.json").read_text())["web_intents"], {})

    async def test_cancel_and_pending_restart_observe_same_original_intent(self):
        process = self.launch_web()
        binding = await self.identity()
        identity, _, pending = await self.begin(binding)
        self.stop(process)
        self.launch_web()
        status, retained = await self.request("GET", f"status/{identity}?v=1")
        self.assertEqual((status, retained), (200, pending))
        status, cancelled = await self.request("POST", "cancel", {"v": 1, "intent_id": identity})
        self.assertEqual((status, cancelled["status"]), (200, "cancelled"))
        status, result = await self.request("POST", "complete", self.complete(identity))
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error"]["code"], "github_web_intent_consumed")

    async def test_provider_failures_and_private_validation_never_reach_logs(self):
        process = self.launch_web(
            "def private_failure(token):\n"
            "    raise RuntimeError('private-web-access-fixture private-web-refresh-fixture')\n"
            "auth.read_actor = private_failure\n")
        binding = await self.identity()
        identity, _, _ = await self.begin(binding)
        status, result = await self.request("POST", "complete", self.complete(identity))
        self.assertEqual(status, 409, result)
        self.assertEqual(result["error"]["code"], "github_web_completion_interrupted")
        process.terminate()
        process.wait(timeout=5)
        logs = process.stderr.read().decode()
        for secret in ("private-web-access-fixture", "private-web-refresh-fixture"):
            self.assertNotIn(secret, logs)
            self.assertNotIn(secret, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
