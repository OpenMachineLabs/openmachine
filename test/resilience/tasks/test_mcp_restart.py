"""resilience tasks test_mcp_restart behavior."""

from contextlib import closing
import sqlite3
import sys
import unittest
import uuid
from mcp import Client
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    def task_snapshot(self):
        with closing(sqlite3.connect(self.state / "tasks.db")) as connection:
            return (connection.execute("SELECT generation FROM store").fetchone(),
                    connection.execute("SELECT task_id,revision,change_seq,current,sha256,operation_count,history_count "
                                       "FROM tasks ORDER BY task_id").fetchall())

    async def test_canonical_tasks_survive_real_server_restart_without_dispatch(self):
        self.launch()
        retained = {}
        for mode in ("legacy", "2026-07-28"):
            async with Client(self.client(), mode=mode, cache=None) as client:
                scope = await client.call_tool("read_workspace_file", {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"})
                identity = "canonical-" + mode
                request = self.task_request(scope.structured_content["context"], identity)
                created = await client.call_tool("create_task", request)
                self.assertFalse(created.is_error, created)
                record = created.structured_content["record"]
                self.assertEqual(created.structured_content["read_schema"], 2)
                self.assertNotIn("operations", record)
                self.assertNotIn("history", record)
                retained[identity] = record
                repeated = await client.call_tool("create_task", request)
                self.assertEqual(repeated.structured_content["record"], record)
                self.assertEqual(repeated.structured_content["status"], "unchanged")
                changed = await client.call_tool("create_task", request | {"title": "Conflicting declaration"})
                self.assertEqual(changed.structured_content["error"]["code"], "task_conflict")
                execution_id = uuid.uuid4().hex
                denied = await client.call_tool("start_execution", {"machine": "desktop", "execution_id": execution_id,
                    "cwd": str(self.work), "argv": [sys.executable, "-c", "raise SystemExit(0)"], "task_id": identity})
                self.assertEqual(denied.structured_content["error"]["code"], "supervised_admission_unavailable")
                self.assertFalse((self.state / "executions" / execution_id).exists())
                original = self.task_snapshot()
                with closing(sqlite3.connect(self.state / "tasks.db")) as connection:
                    payload = connection.execute("SELECT current FROM tasks WHERE task_id=?", (identity,)).fetchone()[0]
                    connection.execute("UPDATE tasks SET current=? WHERE task_id=?",
                                       ("Interrupted authoritative payload fixture", identity))
                    connection.commit()
                corrupted = self.task_snapshot()
                read = await client.call_tool("read_task", {"task_id": identity})
                self.assertTrue(read.is_error, read)
                self.assertEqual(read.structured_content["error"]["code"], "corrupt_tasks")
                self.assertEqual(self.task_snapshot(), corrupted, "A passive read must not repair corrupt authority")
                with closing(sqlite3.connect(self.state / "tasks.db")) as connection:
                    connection.execute("UPDATE tasks SET current=? WHERE task_id=?", (payload, identity))
                    connection.commit()
                read = await client.call_tool("read_task", {"task_id": identity})
                self.assertFalse(read.is_error, read)
                self.assertEqual(read.structured_content["record"], record)
                self.assertEqual(self.task_snapshot(), original)
                reference = record["plan"]["stages"][0]["evidence_ref"]
                decision = await client.call_tool("read_task", reference)
                self.assertFalse(decision.is_error, decision)
                self.assertEqual((decision.structured_content["items"][0]["state"],
                                  decision.structured_content["items"][0]["evidence"]), ("pending", []))
                caps = (await client.call_tool("get_capabilities")).structured_content
                self.assertEqual(caps["lifecycle"]["state_contract"], 18)
                self.assertTrue(caps["tasks"]["structured_records"])
                self.assertTrue(caps["tasks"]["supervised_admission"])
                self.assertFalse(caps["tasks"]["events_delivery"])
        snapshot = self.task_snapshot()
        self.stop(self.processes[-1])
        self.launch()
        async with Client(self.client(), mode="2026-07-28", cache=None) as client:
            found = (await client.call_tool("find_tasks", {"anchors": ["fixture=s11"]})).structured_content
            self.assertEqual(found["coverage"], "complete")
            self.assertEqual({item["task_id"] for item in found["tasks"]}, set(retained))
            for identity, record in retained.items():
                read = (await client.call_tool("read_task", {"task_id": identity})).structured_content
                self.assertEqual(read["record"], record)
                self.assertEqual(read["observations"][0]["state"], "unknown")
            self.assertEqual(self.task_snapshot(), snapshot)
