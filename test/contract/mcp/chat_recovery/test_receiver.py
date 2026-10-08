"""contract mcp chat_recovery test_receiver behavior."""

import unittest
import uuid
from openmachine import tasks
from openmachine.tasks import records
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_correlated_receiver_read_is_durable_on_both_wire_protocols(self):
        from integrations import chatgpt
        from openmachine.bootstrap import create_application
        from openmachine.config import load
        self.config.write_text(self.config.read_text() + '\n[chat_recovery]\nworkspace = "example"\n'
            'connection = "Fixture OM"\nbrowser_account = "Fixture Account"\n'
            'browser_workspace = "Personal"\nmodel = "Fixture Model"\n')
        app = create_application(load(self.config), self.state)
        context = app.read_workspace_file("desktop", "example", "AGENTS.md")["context"]
        identity = "receiver-wire"
        request = self.task_request(context, identity)
        request["contract"]["recovery_policy"] = "task_chat"
        view = app.create_task(**request)
        self.assertEqual(view["chat_recovery"]["state"], "preparing")
        preparation_id = view["record"]["chat_registration"]
        chatgpt.reserve_preparation(self.state, identity, preparation_id,
                                   view["chat_recovery"]["binding_revision"], uuid.uuid4().hex)
        # Only the browser scheduler is held; this test uses real external MCP
        # dispatch, result serialization and durable receiver observation.
        bootstrap = "from openmachine.app.chat_recovery import ChatRecovery\nChatRecovery.tick = lambda self: None"
        self.launch(bootstrap=bootstrap)
        before = records.canonical(tasks.read(self.state, identity)["record"])
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                for invalid in ("bad", True, "a" * 31):
                    response = await self.wire_call(client, "tools/call", {"name": "read_task", "arguments": {
                        "task_id": identity, "preparation_id": invalid}})
                    result = response.json()["result"]
                    self.assert_wire_tool_result(result, version)
                    self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_tool_call")
                response = await self.wire_call(client, "tools/call", {"name": "read_task", "arguments": {
                    "task_id": identity, "preparation_id": preparation_id}})
                result = response.json()["result"]
                self.assert_wire_tool_result(result, version)
                self.assertFalse(result["isError"], result)
                proof = result["structuredContent"]["preparation_read"]
                observed = chatgpt.get(self.state, identity).preparation
                self.assertEqual(observed.receiver_read.task_revision, proof["task_revision"])
                self.assertEqual(observed.receiver_read.record_sha256, proof["record_sha256"])
                self.assertIsNone(observed.ready_at_ms)
                self.assertEqual(records.canonical(tasks.read(self.state, identity)["record"]), before)
        self.stop(self.processes[-1])
        self.launch(bootstrap=bootstrap + "\nfrom integrations import chatgpt\n"
            "def unavailable(*args, **kwargs): raise OSError('Injected receiver store failure')\n"
            "chatgpt.observe_preparation_read = unavailable")
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                response = await self.wire_call(client, "tools/call", {"name": "read_task", "arguments": {
                    "task_id": identity, "preparation_id": preparation_id}})
                result = response.json()["result"]
                self.assert_wire_tool_result(result, version)
                self.assertTrue(result["isError"], result)
                self.assertEqual(result["structuredContent"]["error"]["code"], "chat_recovery_store_unavailable")
                self.assertNotIn("preparation_read", result["structuredContent"])
