"""resilience workspace test_mcp_edit_restart behavior."""

import hashlib
import unittest
import uuid
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_exact_edit_preview_and_original_receipt_survive_server_restart(self):
        self.launch()
        file = self.work / "exact.py"
        file.write_bytes(b"answer = 41\r\nuntouched = 7")
        scope = {"machine": "desktop", "workspace_name": "example", "path": "exact.py"}
        change_id = uuid.uuid4().hex
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                read = (await session.call_tool("read_workspace_file", scope)).structured_content
                args = {**scope, "edits": [{"old_text": "41", "new_text": "42"}],
                        "expected_workspace_identity": read["context"]["workspace_identity"],
                        "expected_head_sha": "", "expected_sha256": read["sha256"]}
                preview = await session.call_tool("edit_workspace_file", args | {"dry_run": True})
                self.assertFalse(preview.is_error, preview)
                self.assertEqual(preview.structured_content["status"], "preview")
                self.assertFalse((self.state / "workspace-changes").exists())
                self.assertIn(b"41", file.read_bytes())
                args.update(change_id=change_id, expected_plan_sha256=preview.structured_content["plan_sha256"])
                applied = await session.call_tool("edit_workspace_file", args)
                self.assertFalse(applied.is_error, applied)
                first = applied.structured_content
                self.assertEqual(first["status"], "applied")
                self.assertEqual(file.read_bytes(), b"answer = 42\r\nuntouched = 7")
                repeated = await session.call_tool("edit_workspace_file", args)
                self.assertFalse(repeated.is_error, repeated)
                self.assertEqual((repeated.structured_content["created_at"], repeated.structured_content["revision"]),
                                 (first["created_at"], first["revision"]))
                rejected = await session.call_tool("edit_workspace_file", args | {"expected_sha256": "0" * 64})
                self.assertTrue(rejected.is_error)
                self.assertEqual(rejected.structured_content["error"]["code"], "change_id_conflict")
                self.assertEqual(rejected.structured_content["change_id"], change_id)
        self.stop(self.processes[-1])
        file.write_bytes(b"later third-party edit")
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("read_workspace_change", {
                    "machine": "desktop", "change_id": change_id, "observe_current": True})
                self.assertFalse(result.is_error, result)
                readback = result.structured_content
                self.assertEqual((readback["status"], readback["created_at"], readback["revision"]),
                                 ("applied", first["created_at"], first["revision"]))
                self.assertEqual(readback["current"]["sha256"], hashlib.sha256(file.read_bytes()).hexdigest())
                self.assertFalse(readback["historical_diff_available"])
                repeated = await session.call_tool("edit_workspace_file", args)
                self.assertEqual(repeated.structured_content["revision"], first["revision"])
                self.assertEqual(file.read_bytes(), b"later third-party edit")
        self.assertEqual(len(list((self.state / "workspace-changes").iterdir())), 1)
        self.assertFalse((self.state / "executions").exists())
