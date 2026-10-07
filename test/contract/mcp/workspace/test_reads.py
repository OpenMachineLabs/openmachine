"""contract mcp workspace test_reads behavior."""

import json
import unittest
from mcp import ClientSession
from openmachine import server
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_large_file_offsets_remain_usable_through_mcp(self):
        self.launch()
        (self.work / "large.txt").write_text("x" * 180000)
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("inspect_workspace", {"machine": "desktop", "workspace_name": "example",
                                                                         "action": "read", "path": "large.txt", "offset": 131072})
                self.assertFalse(result.is_error, result)
                page = result.structured_content
                self.assertEqual(page["next_offset"], 163840)
                self.assertTrue(page["has_more"])
                self.assertEqual(len(page["text"].encode()), 32768)
                tail = await session.call_tool("inspect_workspace", {
                    "machine": "desktop", "workspace_name": "example", "action": "read",
                    "path": "large.txt", "offset": page["next_offset"]})
                self.assertFalse(tail.is_error, tail)
                self.assertEqual(tail.structured_content["next_offset"], 180000)
                self.assertFalse(tail.structured_content["has_more"])
                self.assertEqual(page["text"] + tail.structured_content["text"], "x" * (180000 - 131072))

    async def test_bounded_search_pages_do_not_skip_captured_matches(self):
        self.launch()
        file = self.work / "escaped.txt"
        file.write_text(("needle " + "\x01" * 315 + "\n") * 300)
        scope = {"machine": "desktop", "workspace_name": "example", "action": "search",
                 "query": "needle", "path_glob": "*.txt", "limit": 200}
        offset, lines, snapshot = 0, [], ""
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                while True:
                    result = await session.call_tool("inspect_workspace", scope | {"offset": offset, "expected_snapshot": snapshot})
                    self.assertFalse(result.is_error, result)
                    encoded = json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()
                    self.assertLessEqual(len(encoded), server.MAX_WORKSPACE_RESULT - 1024)
                    page = result.structured_content
                    self.assertEqual(json.loads(result.content[0].text), page)
                    lines.extend(item["line"] for item in page["matches"])
                    snapshot = page["snapshot"]
                    if not page["has_more"]:
                        break
                    self.assertEqual(page["next_offset"], offset + len(page["matches"]))
                    self.assertGreater(page["next_offset"], offset)
                    offset = page["next_offset"]
        self.assertEqual(lines, list(range(1, 301)))

    async def test_escaped_raw_read_pages_remain_bounded_and_lossless(self):
        self.launch()
        file = self.work / "escaped.txt"
        original = "\x01" * 100000
        file.write_text(original)
        scope = {"machine": "desktop", "workspace_name": "example", "path": "escaped.txt"}
        offset, parts = 0, []
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                while True:
                    result = await session.call_tool("read_workspace_file", scope | {"offset": offset})
                    self.assertFalse(result.is_error, result)
                    self.assertLessEqual(len(json.dumps(result.model_dump(mode="json", by_alias=True, exclude_none=True)).encode()),
                                         server.MAX_WORKSPACE_RESULT - 1024)
                    page = result.structured_content
                    parts.append(page["text"])
                    if not page["has_more"]:
                        break
                    self.assertEqual(page["next_offset"], offset + len(page["text"].encode()))
                    offset = page["next_offset"]
        self.assertEqual("".join(parts), original)
