"""contract mcp tasks test_schema behavior."""

import sys
import unittest
from test.support.mcp import MCPFixture


class MCPContractTests(MCPFixture, unittest.IsolatedAsyncioTestCase):
    async def test_all_mutation_contexts_reject_unknown_and_forged_fields_before_dispatch(self):
        self.launch()
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                response = await self.wire_call(client, "tools/call", {"name": "read_workspace_file", "arguments": {
                    "machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}})
                source = response.json()["result"]["structuredContent"]
                native = dict(machine="desktop", execution_id="a" * 32)
                file = dict(machine="desktop", workspace_name="example", expected_head_sha="", change_id="b" * 32,
                            expected_workspace_identity=source["context"]["workspace_identity"])
                cases = {
                    "start_execution": dict(native, cwd=str(self.work), argv=[sys.executable, "-c", "raise SystemExit(0)"]),
                    "cancel_execution": native,
                    "control_execution": dict(native, expected_control_seq=0, action="write_stdin", text="not sent\n"),
                    "write_workspace_file": dict(file, path="new.txt", expected_sha256="", content="not written"),
                    "edit_workspace_file": dict(file, path="AGENTS.md", expected_sha256=source["sha256"],
                        edits=[{"old_text": "Only", "new_text": "Never"}]),
                    "apply_workspace_patch": dict(file, patch="*** Begin Patch\n*** Add File: new.txt\n+not written\n*** End Patch\n",
                        preconditions=[{"path": "new.txt", "expected_sha256": ""}]),
                    "github_issue": dict(action="create", repository="Example/repo", title="Not published", body="fixture"),
                    "github_pull_request": dict(action="create", repository="Example/repo", title="Not published", body="fixture", head="topic", base="main"),
                    "github_comment": dict(action="create", repository="Example/repo", body="fixture", number=1),
                    "github_review": dict(action="create", repository="Example/repo", body="fixture", number=1, expected_head_sha="c" * 40),
                    "github_merge": dict(repository="Example/repo", number=1, expected_head_sha="c" * 40)}
                forged = dict(task_id="not-created", task_revision=1, plan_revision=1, continuation_epoch=0, operation_id="d" * 32)
                for name, arguments in cases.items():
                    for invalid in (dict(arguments, task_revison=1),
                                    dict(arguments, task_context=dict(forged, principal_ref="forged")),
                                    dict(arguments, task_context=dict(forged, task_revision=True))):
                        response = await self.wire_call(client, "tools/call", {"name": name, "arguments": invalid})
                        result = response.json()["result"]
                        self.assertTrue(result["isError"], (name, result))
                        self.assert_wire_tool_result(result, version)
                        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_tool_call")
                control = dict(task_id="not-created", expected_revision=1, expected_plan_revision=1,
                    expected_epoch=0, update_id="e" * 32, transition={"action": "pause", "reason": "fixture"})
                for invalid in (dict(control, owner="forged"), dict(control, expected_revision=True),
                                dict(control, transition={"action": "pause", "reason": "fixture", "state": "completed"})):
                    response = await self.wire_call(client, "tools/call", {"name": "control_task", "arguments": invalid})
                    result = response.json()["result"]
                    self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_tool_call")
                    self.assert_wire_tool_result(result, version)
                self.assertEqual((self.work / "AGENTS.md").read_text(), "Only modify this disposable project.\n")
                self.assertFalse((self.work / "new.txt").exists())
                self.assertFalse((self.state / "tasks").exists())
                self.assertFalse((self.state / "executions").exists())

    async def test_canonical_contract_rejects_runtime_fields_and_invalid_bindings_on_both_protocols(self):
        self.launch()
        import copy
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                response = await self.wire_call(client, "tools/call", {"name": "read_workspace_file", "arguments": {
                    "machine": "desktop", "workspace_name": "example", "path": "AGENTS.md"}})
                context = response.json()["result"]["structuredContent"]["context"]
                request = self.task_request(context, "never-admitted")
                changes = [lambda r: r.update(runtime_state="succeeded"),
                    lambda r: r["stages"][0].update(state="accepted"),
                    lambda r: r["contract"].update(expires_at_ms=True),
                    lambda r: r["contract"].update(scope="\u00e9" * 513),
                    lambda r: r["stages"][0].update(source_sha256="f" * 64),
                    lambda r: r["contract"]["targets"][0].update(workspace_identity="f" * 64)]
                for change in changes:
                    invalid = copy.deepcopy(request)
                    change(invalid)
                    response = await self.wire_call(client, "tools/call", {"name": "create_task", "arguments": invalid})
                    result = response.json()["result"]
                    self.assertTrue(result["isError"], result)
                    self.assert_wire_tool_result(result, version)
                    self.assertIn(result["structuredContent"]["error"]["code"],
                                  {"invalid_tool_call", "invalid_task_contract", "context_mismatch"})
                    self.assertFalse((self.state / "tasks").exists())
                    self.assertFalse((self.state / "executions").exists())

    async def test_scope_binding_is_exposed_and_dispatched_on_both_protocols(self):
        self.launch()
        for version in ("2025-11-25", "2026-07-28"):
            async with self.wire_client(version) as client:
                arguments = dict(task_id="missing-original", expected_revision=1,
                    expected_plan_revision=1, expected_epoch=0, update_id="e" * 32,
                    transition={"action": "bind_effect_scope", "operation_id": "a" * 32,
                        "method": "github_pull_request", "arguments": {
                            "action": "update", "repository": "Example/repo", "number": 8,
                            "body": "Original retained body."},
                        "reason": "Prove tool routing without a provider effect."})
                response = await self.wire_call(client, "tools/call",
                    {"name": "control_task", "arguments": arguments})
                result = response.json()["result"]
                self.assert_wire_tool_result(result, version)
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["error"]["code"], "task_not_found")
                arguments["transition"]["scope"] = {"fields": ["title"]}
                response = await self.wire_call(client, "tools/call",
                    {"name": "control_task", "arguments": arguments})
                self.assertEqual(response.json()["result"]["structuredContent"]["error"]["code"], "invalid_tool_call")
        self.assertFalse((self.state / "tasks").exists())
        self.assertFalse((self.state / "executions").exists())
