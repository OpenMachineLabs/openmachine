"""Installed MCP notices remain passive while the release checker is busy or unavailable."""

import asyncio
import time
import unittest

import openmachine
from test.support.mcp import MCPFixture


class ReleaseNoticeTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    def enable(self):
        self.config.write_text(self.config.read_text().replace(
            "check_for_updates = false", "check_for_updates = true"))

    async def notice(self, client):
        response = await self.wire_call(client, "tools/call", {"name": "get_capabilities"})
        result = response.json()["result"]
        self.assert_wire_tool_result(result, client.headers["MCP-Protocol-Version"])
        self.assertFalse(result["isError"])
        return result["structuredContent"]

    async def until_notice(self, client, predicate):
        deadline = time.monotonic() + 3
        while True:
            result = await self.notice(client)
            if predicate(result["updates"]):
                return result
            self.assertLess(time.monotonic(), deadline, result)
            await asyncio.sleep(0.01)

    async def test_disabled_notice_creates_no_cache_and_preserves_wire_envelopes(self):
        self.launch(bootstrap="from openmachine.service import updates\n"
                    "async def never():\n    raise AssertionError('disabled checker contacted network')\n"
                    "updates._latest_tag = never\n")
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                capabilities = await self.notice(client)
                self.assertEqual(capabilities["updates"]["status"], "disabled")
                self.assertEqual(capabilities["version"], openmachine.__version__)
        self.assertFalse((self.state / "release-check.json").exists())

    async def test_available_notice_and_file_reads_work_on_both_protocols(self):
        self.enable()
        self.launch(bootstrap="from openmachine.service import updates\n"
                    "async def latest():\n    return 'v999.0.0'\nupdates._latest_tag = latest\n")
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                capabilities = await self.until_notice(client, lambda value: value["status"] == "available")
                notice = capabilities["updates"]
                self.assertEqual(notice["latest_version"], "999.0.0")
                self.assertEqual(notice["current_version"], capabilities["version"])
                self.assertEqual(notice["release_url"],
                                 "https://github.com/OpenMachineLabs/openmachine/releases/tag/v999.0.0")
                self.assertFalse(notice["stale"])
                read = await self.wire_call(client, "tools/call", {"name": "read_workspace_file",
                    "arguments": {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}})
                self.assertFalse(read.json()["result"]["isError"])
                self.assertIn("Only modify this disposable project", read.json()["result"]["structuredContent"]["text"])
                self.assertEqual((await self.notice(client))["updates"], notice)
        self.assertFalse((self.state / "executions").exists())
        self.assertFalse((self.state / "tasks.db").exists())

    async def test_pending_network_does_not_block_overlapping_clients(self):
        self.enable()
        entered, release = self.root / "release-started", self.root / "release-finish"
        self.launch(bootstrap=("import asyncio\nfrom pathlib import Path\nfrom openmachine.service import updates\n"
            "async def slow():\n"
            f"    Path({str(entered)!r}).touch()\n"
            f"    while not Path({str(release)!r}).exists():\n        await asyncio.sleep(0.01)\n"
            "    return 'v999.0.0'\nupdates._latest_tag = slow\n"))
        async with self.wire_client("2025-11-25") as first, self.wire_client("2026-07-28") as second:
            async def await_entry():
                while not entered.exists():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(await_entry(), 3)
            replies = await asyncio.wait_for(asyncio.gather(self.notice(first), self.notice(second)), 2)
            self.assertFalse(release.exists())
            self.assertEqual(replies[0]["mcp_catalog"], replies[1]["mcp_catalog"])
            for reply in replies:
                self.assertEqual(reply["updates"]["reason"], "check_incomplete")
            release.touch()
            await self.until_notice(first, lambda value: value["status"] == "available")

    async def test_network_failure_does_not_fail_capabilities_or_expose_details(self):
        self.enable()
        self.launch(bootstrap="from openmachine.service import updates\n"
                    "async def offline():\n    raise OSError('private-fixture-network-detail')\n"
                    "updates._latest_tag = offline\n")
        async with self.wire_client("2026-07-28") as client:
            result = await self.until_notice(client, lambda value: value["reason"] == "network_unavailable")
            self.assertEqual(result["updates"]["status"], "unavailable")
            self.assertNotIn("private-fixture-network-detail", str(result))
            self.assertIsNone(result["updates"]["latest_version"])
            self.assertIn("read_sections", result["tasks"])
