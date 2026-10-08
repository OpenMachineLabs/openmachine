"""resilience app test_mcp_observer behavior."""

import asyncio
import sys
import time
import unittest
import uuid
from mcp import Client
from openmachine import tasks
from openmachine.foundation.state import lease
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_observer_publishes_original_results_with_all_clients_absent(self):
        self.launch()
        # Protocol conformance keeps both wire modes; the lifecycle owner
        # covers every distinct boundary once on the current protocol.
        scenarios = (("legacy", ("live",)),
                     ("2026-07-28", ("live", "restart-running", "restart-completed",
                                      "crash-running", "crash-completed", "activation")))
        for mode, boundaries in scenarios:
            for boundary in boundaries:
                identity = "observe-" + mode + "-" + boundary
                execution_id = uuid.uuid4().hex
                counter, release, done = (self.work / (prefix + identity) for prefix in ("count-", "release-", "done-"))
                def retained_record():
                    return tasks.read(self.state, identity, operation_ids=(execution_id,))["record"]
                try:
                    async with Client(self.client(), mode=mode, cache=None) as client:
                        async def call(name, arguments):
                            result = await client.call_tool(name, arguments)
                            self.assertFalse(result.is_error, result.structured_content)
                            return result.structured_content
                        context = (await call("read_workspace_file", {"machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}))["context"]
                        request = self.task_request(context, identity)
                        request["contract"]["tools"] = ["control_task", "start_execution", "read_task", "read_execution"]
                        record = (await call("create_task", request))["record"]
                        record = (await call("control_task", dict(task_id=identity, expected_revision=record["revision"],
                            expected_plan_revision=1, expected_epoch=0, update_id=uuid.uuid4().hex,
                            transition={"action": "claim", "handoff_id": record["handoff"]["id"]})))["record"]
                        code = ('from pathlib import Path\nimport time\n'
                            + 'p=Path(' + repr(str(counter)) + '); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
                            + 'while not Path(' + repr(str(release)) + ').exists(): time.sleep(0.02)\n'
                            + 'Path(' + repr(str(done)) + ').touch()\n')
                        started = await call("start_execution", dict(machine="desktop", execution_id=execution_id,
                            cwd=str(self.work), argv=[sys.executable, "-c", code], timeout_seconds=20,
                            task_context=dict(task_id=identity, task_revision=record["revision"], plan_revision=1,
                                continuation_epoch=record["control"]["epoch"], operation_id=execution_id)))
                        async with Client(self.client(), mode=mode, cache=None) as second:
                            self.assertEqual((await self.observer_task_view(second, identity))["task_id"], identity)
                    # No SDK sessions or tool calls remain. Only fixture control
                    # and passive Task-owner SQLite reads may await publication.
                    if boundary != "live":
                        crashed = boundary.startswith("crash-")
                        if crashed:
                            self.processes[-1].kill()
                            self.processes[-1].wait(timeout=5)
                            self.assertTrue(self.socket.exists())
                        else:
                            self.stop(self.processes[-1])
                        before = retained_record()
                        if boundary.endswith("-completed"):
                            release.touch()
                            deadline = time.monotonic() + 5
                            while not done.exists() and time.monotonic() < deadline:
                                await asyncio.sleep(0.03)
                            self.assertTrue(done.exists())
                            self.assertEqual(retained_record(), before)
                        if boundary == "activation":
                            # The real lifecycle owner holds this lease through
                            # candidate startup/readiness. It must not strand the
                            # new observer or permit publication before release.
                            with lease(self.state / "locks/lifecycle.lock"):
                                self.launch(managed=True)
                                release.touch()
                                deadline = time.monotonic() + 5
                                while not done.exists() and time.monotonic() < deadline:
                                    await asyncio.sleep(0.03)
                                self.assertTrue(done.exists())
                                async with Client(self.client(), mode=mode, cache=None) as probe:
                                    self.assertEqual((await self.observer_task_view(probe, identity))["task_id"], identity)
                                self.assertEqual(retained_record(), before)
                        else:
                            # CI explicitly supplies managed startup; native
                            # launchd auto-restart is separate acceptance.
                            self.launch(managed=crashed)
                    release.touch()
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        record = retained_record()
                        if (record["operations"][0]["result"] or {}).get("status") == "succeeded":
                            break
                        await asyncio.sleep(0.03)
                    self.assertEqual(record["operations"][0]["result"]["status"], "succeeded", record)
                    self.assertEqual(record["handoff"]["reason"], "result_ready")
                    self.assertEqual(record["control"]["state"], "waiting_for_ai")
                    self.assertEqual(record["plan"]["stages"][0]["state"], "running")
                    self.assertIsNone(record["terminal"])
                    self.assertEqual([item["id"] for item in record["operations"]], [execution_id])
                    self.assertEqual(record["_facts"]["operation_count"], 1)
                    self.assertEqual(counter.read_text(), "1")
                    before = retained_record()
                    async with Client(self.client(), mode=mode, cache=None) as client:
                        view = await self.observer_task_view(client, identity)
                        self.assertTrue(view["support"]["local_result_observation"])
                        self.assertFalse(view["support"]["events_delivery"])
                        self.assertEqual(view["observations"][0]["state"], "unknown")
                        self.assertNotIn("operations", view["record"])
                        page = await client.call_tool("read_task", {"task_id": identity,
                            "section": "operations", "operation_id": execution_id})
                        self.assertFalse(page.is_error, page.structured_content)
                        self.assertTrue(page.structured_content["complete"])
                        self.assertEqual([item["id"] for item in page.structured_content["items"]], [execution_id])
                        original = (await client.call_tool("read_execution", {"machine": "desktop", "execution_id": execution_id})).structured_content
                        self.assertEqual(original["created_at"], started["created_at"])
                        self.assertEqual(original["status"], "succeeded")
                        self.assertEqual(retained_record(), before)
                finally:
                    release.touch()
                    deadline = time.monotonic() + 5
                    while not done.exists() and time.monotonic() < deadline:
                        await asyncio.sleep(0.03)
