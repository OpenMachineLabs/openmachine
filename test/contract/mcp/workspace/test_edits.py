"""contract mcp workspace test_edits behavior."""

import json
import sys
import unittest
import uuid
from mcp import ClientSession
from openmachine.mcp import protocol
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_exact_edit_grant_schema_and_worst_case_json(self):
        self.config.write_text(self.config.read_text().replace("execution_enabled = true", "execution_enabled = false\nworkspace_write_enabled = true"))
        self.launch()
        file = self.work / "escaped.txt"
        file.write_text("\x01" * 32760 + " 41")
        scope = {"machine": "desktop", "workspace_name": "example", "path": "escaped.txt"}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                read = (await session.call_tool("read_workspace_file", scope)).structured_content
                args = {**scope, "edits": [{"old_text": "41", "new_text": "42"}],
                        "expected_workspace_identity": read["context"]["workspace_identity"],
                        "expected_head_sha": "", "expected_sha256": read["sha256"]}
                for delta in ({"dry_run": "false"}, {"edits": [{"old_text": "41", "new_text": "42", "replace_all": True}]},
                              {"edits": [{"old_text": "", "new_text": "42"}]}, {"edits": []}):
                    bad = await session.call_tool("edit_workspace_file", args | {"change_id": uuid.uuid4().hex} | delta)
                    self.assertTrue(bad.is_error)
                    self.assertTrue(file.read_text().endswith("41"))
                identity = uuid.uuid4().hex
                result = await session.call_tool("edit_workspace_file", args | {"change_id": identity})
                self.assertFalse(result.is_error, result)
                encoded = json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()
                self.assertLessEqual(len(encoded), protocol.MAX_WORKSPACE_RESULT - 1024)
                self.assertEqual(result.structured_content["change_id"], identity)
                self.assertTrue(result.structured_content["truncated"])
                self.assertEqual(result.structured_content["effects"][0]["stage"], "applied")
                self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                denied = await session.call_tool("start_execution", {"machine": "desktop", "execution_id": uuid.uuid4().hex,
                    "cwd": str(self.work), "argv": [sys.executable, "-c", "pass"]})
                self.assertEqual(denied.structured_content["error"]["code"], "execution_disabled")

    async def test_large_line_window_small_edit_and_receipt_through_sdk(self):
        self.launch()
        file = self.work / "large.py"
        original = b"padding\n" * 20000 + b"answer = 41\n"
        file.write_bytes(original)
        scope = {"machine": "desktop", "workspace_name": "example", "path": "large.py"}
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                read = await session.call_tool("read_workspace_file", scope | {"start_line": 20001, "line_count": 1})
                self.assertFalse(read.is_error, read)
                page = read.structured_content
                self.assertEqual(page["text"], "answer = 41\n")
                self.assertGreater(page["offset"], 131072)
                byte_page = await session.call_tool("read_workspace_file", scope | {"offset": page["offset"], "expected_sha256": page["sha256"]})
                self.assertFalse(byte_page.is_error, byte_page)
                identity = uuid.uuid4().hex
                args = scope | {"edits": [{"old_text": "41", "new_text": "42"}],
                    "expected_workspace_identity": page["context"]["workspace_identity"],
                    "expected_head_sha": "", "expected_sha256": page["sha256"]}
                preview = await session.call_tool("edit_workspace_file", args | {"dry_run": True})
                self.assertFalse(preview.is_error, preview)
                self.assertEqual(file.read_bytes(), original)
                changed = await session.call_tool("edit_workspace_file", args | {"change_id": identity,
                    "expected_plan_sha256": preview.structured_content["plan_sha256"]})
                self.assertFalse(changed.is_error, changed)
                self.assertEqual(file.read_bytes(), original.replace(b"41", b"42"))
                receipt = await session.call_tool("read_workspace_change", {"machine": "desktop", "change_id": identity, "observe_current": True})
                self.assertFalse(receipt.is_error, receipt)
                self.assertEqual(receipt.structured_content["current"]["sha256"], changed.structured_content["plan"]["after_sha256"])
                for invalid in ({"start_line": True}, {"line_count": "2"}, {"line_count": 501}, {"start_line": 1, "offset": 1}):
                    result = await session.call_tool("read_workspace_file", scope | invalid)
                    self.assertTrue(result.is_error, invalid)
