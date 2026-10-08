"""contract mcp workspace test_reads behavior."""

from copy import deepcopy
import json
import subprocess
import unittest
import uuid
from mcp import ClientSession
from openmachine import server
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_optional_worktree_selection_creation_and_task_target_through_mcp(self):
        subprocess.run(["git", "-C", str(self.work), "init", "-q", "-b", "main"],
                       check=True, capture_output=True)
        source = self.work / "source.txt"
        source.write_text("Committed source.\n")
        subprocess.run(["git", "-C", str(self.work), "add", "AGENTS.md", "source.txt"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.work), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "SDK worktree fixture"],
                       check=True, capture_output=True)
        source.write_text("Uncommitted source stays here.\n")
        trees = self.root / "trees"
        trees.mkdir()
        trees = trees.resolve()
        self.config.write_text(self.config.read_text() + '\n[workspaces.trees]\nmachine = "desktop"\nroot = '
                               + json.dumps(str(trees)) + '\n')
        self.launch()
        scope = {"machine": "desktop", "workspace_name": "example"}
        destination = trees / "selected"
        identity = uuid.uuid4().hex
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    self.assertFalse(result.is_error, result.structured_content)
                    self.assertEqual(json.loads(result.content[0].text), result.structured_content)
                    return result.structured_content

                original = await call("read_workspace_file", scope | {"path": "source.txt"})
                invalid_read = await session.call_tool("read_workspace_file", scope | {
                    "path": "source.txt", "worktree": True})
                self.assertTrue(invalid_read.is_error)
                self.assertEqual(invalid_read.structured_content["error"]["code"], "invalid_tool_call")
                invalid_path = await session.call_tool("read_workspace_file", scope | {
                    "path": "source.txt", "worktree": "relative-tree"})
                self.assertEqual(invalid_path.structured_content["error"]["code"], "path_denied")
                request = scope | {"destination": str(destination), "base_ref": original["context"]["head_sha"],
                    "execution_id": identity, "expected_workspace_identity": original["context"]["workspace_identity"]}
                for delta in ({"execution_id": "invalid"}, {"worktree": 7}, {"argv": ["unrelated-command"]}):
                    malformed = await session.call_tool("create_worktree", request | delta)
                    self.assertTrue(malformed.is_error, malformed)
                    self.assertEqual(malformed.structured_content["error"]["code"], "invalid_tool_call")
                    self.assertFalse(destination.exists())
                    self.assertFalse((self.state / "executions" / identity).exists())
                started = await call("create_worktree", request)
                self.assertEqual(started["execution_id"], identity)
                result = await self.execution_until(session, {"machine": "desktop", "execution_id": identity},
                    lambda value: value["status"] not in {"accepted", "preparing", "running", "finishing"})
                self.assertEqual((result["status"], result["exit_code"]), ("succeeded", 0))
                self.assertEqual(result["context"]["created_worktree"]["cwd"], str(destination))
                self.assertIsNone(result["context"]["created_worktree"]["branch"])
                selected = await call("read_workspace_file", scope | {
                    "path": "source.txt", "worktree": str(destination)})
                self.assertEqual(selected["text"], "Committed source.\n")
                self.assertEqual(selected["context"]["cwd"], str(destination))
                unchanged = await call("read_workspace_file", scope | {"path": "source.txt", "worktree": ""})
                self.assertEqual(unchanged["text"], original["text"])
                self.assertEqual(unchanged["context"]["workspace_identity"], original["context"]["workspace_identity"])
                listed = await call("inspect_workspace", scope | {"action": "worktrees"})
                self.assertEqual({item["worktree"] for item in listed["entries"]},
                                 {original["context"]["worktree"], str(destination)})
                repeated = await call("create_worktree", request | {"worktree": ""})
                self.assertEqual((repeated["execution_id"], repeated["created_at"]), (identity, result["created_at"]))

                task_request = self.task_request(selected["context"], "selected-worktree-sdk")
                task_request["contract"]["targets"][0]["worktree"] = str(destination)
                malformed_task = deepcopy(task_request)
                malformed_task["contract"]["targets"][0]["worktree"] = 7
                rejected = await session.call_tool("create_task", malformed_task)
                self.assertTrue(rejected.is_error)
                self.assertEqual(rejected.structured_content["error"]["code"], "invalid_tool_call")
                created = await call("create_task", task_request)
                retained = await self.observer_task_view(session, task_request["task_id"])
                self.assertEqual(retained["record"]["contract"]["targets"], created["record"]["contract"]["targets"])
                self.assertEqual(retained["record"]["contract"]["targets"][0]["worktree"], str(destination))

        self.stop(self.processes.pop())
        self.config.write_text(self.config.read_text().replace("execution_enabled = true", "execution_enabled = false"))
        self.launch()
        async with self.client() as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                repeated = await session.call_tool("create_worktree", request)
                self.assertFalse(repeated.is_error, repeated.structured_content)
                self.assertEqual(repeated.structured_content["created_at"], result["created_at"])
                denied = await session.call_tool("create_worktree", request | {"execution_id": uuid.uuid4().hex})
                self.assertEqual(denied.structured_content["error"]["code"], "execution_disabled")

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
