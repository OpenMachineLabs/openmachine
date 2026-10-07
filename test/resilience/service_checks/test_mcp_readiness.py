"""resilience service_checks test_mcp_readiness behavior."""

import asyncio
import hashlib
import json
import unittest
from mcp import ClientSession
import openmachine
from openmachine.foundation.state import lease
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_readiness_and_mutation_gate(self):
        self.launch()
        from openmachine import execution, service
        from openmachine.foundation.sanitize import Fault
        probe = {"workspace": "example", "path": "AGENTS.md"}
        ready = await asyncio.to_thread(service.probe_service, self.state, self.config, probe,
                                        openmachine.__version__, self.processes[-1].pid)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["tunnel"], "not_probed")
        self.assertEqual(ready["chatgpt"], "not_probed")
        self.assertNotIn("text", ready)
        self.assertEqual(ready["file_sha256"], hashlib.sha256((self.work / "AGENTS.md").read_bytes()).hexdigest())
        # Content changes and safe redaction do not make a readable probe fail.
        (self.work / "AGENTS.md").write_text("token = synthetic-test-value\n")
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                redacted = await session.call_tool("read_workspace_file", {"machine": "desktop",
                    "workspace_name": "example", "path": "AGENTS.md"})
                self.assertFalse(redacted.is_error)
                self.assertTrue(redacted.structured_content["content_redacted"])
        ready = await asyncio.to_thread(service.probe_service, self.state, self.config, probe,
                                        openmachine.__version__, self.processes[-1].pid)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["file_sha256"], hashlib.sha256((self.work / "AGENTS.md").read_bytes()).hexdigest())
        self.assertNotIn("synthetic-test-value", json.dumps(ready))
        with self.assertRaises(Fault) as failed:
            await asyncio.to_thread(service.probe_service, self.state, self.config, probe,
                                    openmachine.__version__, self.processes[-1].pid + 1)
        self.assertEqual(failed.exception.code, "service_identity_mismatch")
        with self.assertRaises(Fault) as failed:
            await asyncio.to_thread(service.probe_service, self.state, self.config,
                                    {**probe, "path": "absent.txt"}, openmachine.__version__)
        self.assertEqual(failed.exception.code, "service_read_unavailable")
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                request = {"task_id": "transition-guard", "title": "Transition guard",
                           "expected_revision": 0, "body": "One authorized fixture checkpoint.", "anchors": []}
                with lease(self.state / "locks/lifecycle.lock"):
                    denied = await session.call_tool("save_task", request)
                    self.assertTrue(denied.is_error)
                    self.assertEqual(denied.structured_content["error"]["code"], "resource_busy")
                    self.assertFalse((await session.call_tool("get_capabilities", {})).is_error)
                    self.assertFalse((self.state / "tasks").exists())
                saved = await session.call_tool("save_task", request)
                self.assertFalse(saved.is_error, saved)
                self.assertEqual(saved.structured_content["revision"], 1)
