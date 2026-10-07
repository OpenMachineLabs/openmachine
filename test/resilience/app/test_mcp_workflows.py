"""resilience app test_mcp_workflows behavior."""

import asyncio
import subprocess
import sys
import unittest
import uuid
from mcp import ClientSession
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_complete_coding_workflow_uses_real_mcp_tools(self):
        self.launch()
        def git(*args):
            return subprocess.run(["git", "-C", str(self.work), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        (self.work / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
        (self.work / "test_calculator.py").write_text(
            "import unittest\nfrom calculator import add\nclass Addition(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        git("add", "AGENTS.md", "calculator.py", "test_calculator.py")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
        head = git("rev-parse", "HEAD")
        (self.work / "unrelated.txt").write_text("preserve this existing work\n")
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                scope = {"machine": "desktop", "workspace_name": "example"}
                rules = await session.call_tool("inspect_workspace", {**scope, "action": "read", "path": "AGENTS.md"})
                self.assertIn("disposable", rules.structured_content["text"])
                before = await session.call_tool("inspect_workspace", {**scope, "action": "status"})
                self.assertEqual(before.structured_content["context"]["head_sha"], head)
                found = await session.call_tool("inspect_workspace", {**scope, "action": "search", "query": "return a - b"})
                self.assertEqual(found.structured_content["matches"][0]["path"], "calculator.py")
                commands = [
                    [sys.executable, "-c", 'from pathlib import Path; p=Path("calculator.py"); s=p.read_text(); assert s.count("return a - b")==1; p.write_text(s.replace("return a - b", "return a + b"))'],
                    [sys.executable, "-m", "unittest", "discover", "-p", "test_calculator.py"],
                ]
                for argv in commands:
                    execution_id = uuid.uuid4().hex
                    request = {"machine": "desktop", "execution_id": execution_id, "cwd": str(self.work),
                               "argv": argv, "expected_head_sha": head}
                    started = await session.call_tool("start_execution", request)
                    self.assertFalse(started.is_error, started)
                    for _ in range(100):
                        result = await session.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})
                        self.assertFalse(result.is_error, result)
                        if result.structured_content["status"] in ("succeeded", "failed", "outcome_unknown"):
                            break
                        await asyncio.sleep(0.03)
                    self.assertEqual((result.structured_content["status"], result.structured_content["exit_code"]), ("succeeded", 0))
                self.assertIn("OK", result.structured_content["output"])
                diff = await session.call_tool("inspect_workspace", {**scope, "action": "diff", "path": "calculator.py"})
                self.assertFalse(diff.is_error, diff)
                self.assertIn("+    return a + b", diff.structured_content["text"])
                self.assertFalse(diff.structured_content["truncated"])
                readback = await session.call_tool("inspect_workspace", {**scope, "action": "read", "path": "calculator.py"})
                self.assertEqual(readback.structured_content["text"], "def add(a, b):\n    return a + b\n")
                self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve this existing work\n")
                self.assertEqual(git("rev-parse", "HEAD"), head)
