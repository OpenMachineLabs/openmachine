"""Two generic MCP callers retain Task ownership on both protocol variants."""

import asyncio
import sys
import time
import unittest
import uuid

from mcp import Client
from test.support.mcp import MCPFixture


class MCPWriterReservationTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def call(self, client, name, arguments):
        result = await client.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.structured_content)
        return result.structured_content

    async def control(self, client, identity, action, **values):
        record = (await self.call(client,"read_task",{"task_id":identity}))["record"]
        return await self.call(client,"control_task",dict(task_id=identity,expected_revision=record["revision"],
            expected_plan_revision=record["plan"]["revision"],expected_epoch=record["control"]["epoch"],
            update_id=uuid.uuid4().hex,transition=dict(action=action,**values)))

    async def settled_task(self, client, identity):
        # Execution receipts and Task publication have independent lifetimes.
        # Observe the original result before issuing an exact-CAS control change.
        deadline = time.monotonic() + 6
        while True:
            view = await self.observer_task_view(client, identity)
            if view["counts"]["unresolved_count"] == 0:
                return view
            self.assertLess(time.monotonic(), deadline, view)
            await asyncio.sleep(0.03)

    async def start(self, client, identity, marker):
        record = (await self.call(client,"read_task",{"task_id":identity}))["record"]
        operation=uuid.uuid4().hex
        return await self.call(client,"start_execution",dict(machine="desktop",execution_id=operation,cwd=str(self.work),
            argv=[sys.executable,"-c",f"from pathlib import Path; Path({marker!r}).write_text('once')"],
            task_context=dict(task_id=identity,task_revision=record["revision"],plan_revision=record["plan"]["revision"],
                              continuation_epoch=record["control"]["epoch"],operation_id=operation)))

    async def check_callers(self, mode):
        self.launch()
        async with Client(self.client(),mode=mode,cache=None) as first, Client(self.client(),mode=mode,cache=None) as second:
            observed = await self.call(first,"read_workspace_file",dict(machine="desktop",workspace_name="example",path="AGENTS.md"))
            request=self.task_request(observed["context"],"shared-task")
            request["contract"]["tools"]=["read_task","start_execution","control_task"]
            created=await self.call(first,"create_task",request)
            await self.control(first,"shared-task","claim",handoff_id=created["record"]["handoff"]["id"])
            original=await self.start(first,"shared-task","first")
            settled=await self.execution_until(first,dict(machine="desktop",execution_id=original["execution_id"]),
                                               lambda value:value["status"]=="succeeded")
            self.assertEqual(settled["cleanup"]["status"],"verified")
            await self.settled_task(first,"shared-task")
            released=await self.control(first,"shared-task","release",reason="Eligible generic caller handoff")
            await self.control(second,"shared-task","claim",handoff_id=released["record"]["handoff"]["id"])
            successor=await self.start(second,"shared-task","successor")
            await self.execution_until(second,dict(machine="desktop",execution_id=successor["execution_id"]),
                                       lambda value:value["status"]=="succeeded")
            self.assertEqual((self.work / "first").read_text(),"once")
            self.assertEqual((self.work / "successor").read_text(),"once")
            bare_id=uuid.uuid4().hex
            denied=await first.call_tool("start_execution",dict(machine="desktop",execution_id=bare_id,cwd=str(self.work),
                argv=[sys.executable,"-c","from pathlib import Path; Path('bypass').touch()"],workspace_access="shared"))
            self.assertTrue(denied.is_error)
            self.assertEqual(denied.structured_content["error"]["code"],"task_resource_conflict")
            self.assertFalse((self.state / "executions" / bare_id).exists())
            self.assertFalse((self.work / "bypass").exists())
            original_read=await self.call(second,"read_execution",dict(machine="desktop",execution_id=original["execution_id"]))
            self.assertEqual(original_read["status"],"succeeded")

    async def test_legacy_two_callers(self):
        await self.check_callers("legacy")

    async def test_modern_two_callers(self):
        await self.check_callers("2026-07-28")
