"""Behavior checks for MCPContractTests."""

import json
import unittest
from unittest.mock import patch
import httpx2 as httpx
from openmachine import server
from support_mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_task_events_readonly_preflight_preserves_original_store(self):
        import probe_task_handoff as probe
        self.assertEqual(probe.private_socket(str(self.socket)), self.socket)
        async with self.wire_client(probe.PROTOCOL) as client:
            report = await probe.observe(client)
        self.assertEqual(report["discovery"], "ready")
        self.assertTrue(report["protocol_supported"])
        self.assertTrue(report["events_advertised"])
        self.assertIsNone(report["method_errors"]["events/list"])
        self.assertEqual(report["ai_continuation"], "not_run")
        self.assertFalse((self.state / "tasks").exists())
        self.assertFalse((self.state / "executions").exists())
        alias = self.root / "socket-alias"
        alias.symlink_to(self.socket)
        for path in (str(alias), str(self.config), "relative.sock"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                probe.private_socket(path)

    async def test_delivered_events_extension_discovers_verifies_and_revokes_on_public_sdk(self):
        import base64
        from integrations import events
        from openmachine.bootstrap import create_application
        from openmachine.config import load
        from mcp.server.transport_security import TransportSecuritySettings

        self.stop_all()
        application = create_application(load(self.config), self.state)
        context = application.read_workspace_file("desktop", "example", "AGENTS.md")["context"]
        application.create_task(**self.task_request(context, "events-sdk"))
        with patch.object(server, "create_application", return_value=application):
            runtime = server.create_server(load(self.config), self.state)
        app = runtime.streamable_http_app(json_response=True, stateless_http=True,
            transport_security=TransportSecuritySettings(allowed_hosts=["localhost"]))
        params = {"name": "task.handoff_ready", "arguments": {"task_id": "events-sdk"}, "cursor": None,
            "delivery": {"mode": "webhook", "url": "https://callback.example/event",
                         "secret": "whsec_" + base64.b64encode(b"s" * 32).decode()}}
        def callback(url, body, *args):
            value = json.loads(body)
            return (200, json.dumps({"challenge": value["challenge"]}).encode(), None) if value.get("type") == "verification" else (204, b"", None)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False, timeout=6,
                    headers={"MCP-Protocol-Version": "2026-07-28", "accept": "application/json, text/event-stream"}) as client:
                discovery = (await self.wire_call(client, "server/discover", {})).json()["result"]
                self.assertEqual(discovery["capabilities"]["events"], {})
                catalog = (await self.wire_call(client, "events/list", {})).json()["result"]
                self.assertEqual([event["name"] for event in catalog["events"]], ["task.handoff_ready"])
                for error_params in (params | {"cursor": "replay"}, params | {"principal": "forged"}, params | {"principal": None}):
                    invalid = (await self.wire_call(client, "events/subscribe", error_params)).json()
                    self.assertEqual(invalid["error"]["code"], -32602)
                with patch.object(events, "post", return_value=(200, b'{"challenge":"wrong"}', None)):
                    rejected = (await self.wire_call(client, "events/subscribe", params)).json()
                    self.assertEqual(rejected["error"]["code"], -32015)
                with patch.object(events, "post", side_effect=callback):
                    subscribed = (await self.wire_call(client, "events/subscribe", params)).json()["result"]
                    repeat_reply = (await self.wire_call(client, "events/subscribe", params)).json()
                    self.assertIn("result", repeat_reply, repeat_reply)
                    repeated = repeat_reply["result"]
                    self.assertEqual(subscribed["id"], repeated["id"])
                    self.assertIsNone(subscribed["cursor"])
                    self.assertFalse(subscribed["truncated"])
                    # No private signing material or callback URL in the task view.
                    read = (await self.wire_call(client, "tools/call", {"name": "read_task", "arguments": {"task_id": "events-sdk"}})).json()
                    self.assertNotIn(params["delivery"]["secret"], json.dumps(read))
                    self.assertNotIn(params["delivery"]["url"], json.dumps(read))
                    unsubscribe = {"name": params["name"], "arguments": params["arguments"],
                                   "delivery": {"mode": "webhook", "url": params["delivery"]["url"]}}
                    for _ in range(2):
                        reply = (await self.wire_call(client, "events/unsubscribe", unsubscribe)).json()["result"]
                        self.assertEqual({k: v for k, v in reply.items() if k not in {"_meta", "resultType"}}, {})
                self.assertEqual(application.events.task_ids(), set())
                client.headers["MCP-Protocol-Version"] = "2025-11-25"
                self.assertEqual((await self.wire_call(client, "events/list", {})).json()["error"]["code"], -32601)

    async def test_task_events_probe_rejects_inconclusive_or_unsafe_evidence(self):
        import probe_task_handoff as probe
        requests = []
        def transport(request):
            value = json.loads(request.content)
            requests.append(value["method"])
            if value["method"] == "server/discover":
                result = {"supportedVersions": [probe.PROTOCOL], "capabilities": {"events": {}}}
            else:
                result = {"events": [], "nextCursor": "more"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": value["id"], "result": result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False) as client:
            report = await probe.observe(client)
            self.assertEqual(report["discovery"], "unknown")
            self.assertFalse(report["catalog_complete"])
            self.assertEqual(requests, list(probe.READ_METHODS))
            with self.assertRaises(ValueError):
                await probe.rpc(client, "events/subscribe")
            self.assertEqual(requests, list(probe.READ_METHODS))
        cases = ((200, b"x" * (probe.MAX_RESPONSE + 1)), (200, b"{}"),
                 (200, b'{"jsonrpc":"2.0","id":true,"result":{}}'),
                 (200, b'{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":-32601}}'),
                 (302, b"not-followed"))
        for status, raw in cases:
            with self.subTest(status=status, size=len(raw)):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(status, content=raw)), trust_env=False) as client:
                    with self.assertRaises(ValueError):
                        await probe.rpc(client, "server/discover")
