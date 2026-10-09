"""contract mcp workspace test_patch behavior."""

import json
import sys
import unittest
import uuid
from mcp import ClientSession
from openmachine.mcp import protocol
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_patch_strict_schema_independent_grant_and_escaped_response(self):
        self.config.write_text(self.config.read_text().replace("execution_enabled = true","execution_enabled = false\nworkspace_write_enabled = true"))
        self.launch()
        scope = {"machine":"desktop","workspace_name":"example"}
        text = "*** Begin Patch\n*** Add File: escaped\n+" + "\x01" * 32760 + "\n*** End Patch\n"
        async with self.client() as (reader, writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                read = (await session.call_tool("read_workspace_file",scope | {"path":"AGENTS.md"})).structured_content
                request = scope | {"patch":text,"expected_workspace_identity":read["context"]["workspace_identity"],
                                  "expected_head_sha":"","preconditions":[{"path":"escaped","expected_sha256":""}]}
                for delta in ({"create_parents":1},{"dry_run":"false"},{"preconditions":[]},
                              {"preconditions":[{"path":"escaped","expected_sha256":"","extra":True}]}):
                    result = await session.call_tool("apply_workspace_patch",request | {"change_id":uuid.uuid4().hex} | delta)
                    self.assertTrue(result.is_error)
                    self.assertFalse((self.work / "escaped").exists())
                identity = uuid.uuid4().hex
                result = await session.call_tool("apply_workspace_patch",request | {"change_id":identity})
                self.assertFalse(result.is_error,result)
                self.assertEqual(result.structured_content["status"],"applied")
                self.assertEqual(result.structured_content["change_id"],identity)
                self.assertEqual(len(result.structured_content["effects"]),1)
                self.assertEqual(json.loads(result.content[0].text),result.structured_content)
                self.assertLessEqual(len(json.dumps(result.model_dump(mode="json",by_alias=True,exclude_none=True)).encode()),protocol.MAX_WORKSPACE_RESULT-1024)
                denied = await session.call_tool("start_execution",{"machine":"desktop","execution_id":uuid.uuid4().hex,
                    "cwd":str(self.work),"argv":[sys.executable,"-c","pass"]})
                self.assertEqual(denied.structured_content["error"]["code"],"execution_disabled")
