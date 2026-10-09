"""MCP execution and Codex registration; original effects stay application-owned."""

import asyncio
from functools import partial
import threading
from typing import Annotated, Literal
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field
from openmachine import tasks
from openmachine.mcp_protocol import (READ, WRITE, DEFAULT_TIMEOUT, MAX_TIMEOUT, MAX_WAIT,
                                      MAX_CONTROL_SEQ, MAX_INPUT, TerminalSize,
                                      _reply, _result, _submit_owned, _task_context)

def register_execution_tools(application, guided_tool, waiting, machine_read, has_ssh):
    @guided_tool(annotations=WRITE)
    async def start_codex_task(machine: str, execution_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                               cwd: str, goal: Annotated[str, Field(min_length=1, max_length=6000)],
                               start_url: Annotated[str, Field(min_length=1, max_length=2048)],
                               allowed_origins: Annotated[list[Annotated[str, Field(max_length=2048)]], Field(min_length=1, max_length=8)],
                               mode: Literal["browser"] = "browser", browser: Literal["auto", "iab", "chrome", "edge"] = "auto",
                               timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=1800)] = 600,
                               expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")] = "",
                               task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Delegate an authorized browser/UI goal to native Codex; Chat remains controller. Use exact origins and a task-owned tab, with no secrets or unrelated context. Execution grant, workspace and task_context admission apply. Retain execution_id and read it after uncertainty; cancel via cancel_execution. CLI presence is not browser availability; native scope is not a sandbox."""
        operation = partial(_reply, lambda: application.start_codex_task(machine, execution_id, cwd, goal,
            start_url, allowed_origins, mode, browser, timeout_seconds, expected_head_sha, _task_context(task_context)), execution_output=True)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=READ)
    def read_codex_task(machine: str, execution_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> CallToolResult:
        """Read the original Codex browser report and native final-page evidence without starting work. Process success is not semantic completion; Chat must inspect this report before stage acceptance. Missing evidence stays unknown, and cancellation/timeout is never promoted to completion."""
        return _reply(lambda: application.read_codex_task(machine, execution_id))

    @guided_tool(annotations=WRITE)
    async def create_worktree(machine: str, workspace_name: str,
                              destination: Annotated[str, Field(min_length=1, max_length=4096)],
                              base_ref: Annotated[str, Field(min_length=1, max_length=256)],
                              execution_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                              expected_workspace_identity: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
                              new_branch: Annotated[str, Field(max_length=256)] = "",
                              worktree: Annotated[str, Field(max_length=4096)] = "",
                              task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Create isolation only after inspecting existing trees and task context. Destination must be absent and within machine grants; base_ref must exist locally. new_branch must be absent, or empty means detached. No fetch, dirty-file copy or automatic removal; conditional Git includes need explicit resolution. Save execution_id before calling and read/cancel that original ID after uncertainty. Success does not change default directories or grants: select context.created_worktree explicitly and revise the task target before edits."""
        operation = partial(_reply, lambda: application.create_worktree(machine, workspace_name, destination,
            base_ref, execution_id, expected_workspace_identity, new_branch, worktree, _task_context(task_context)), execution_output=True)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=WRITE)
    def start_execution(machine: str, execution_id: str, cwd: str,
                        argv: Annotated[list[str], Field(min_length=1, max_length=100)],
                        timeout_seconds: Annotated[int, Field(ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
                        expected_head_sha: str = "", workspace_access: Literal["exclusive", "shared"] = "exclusive",
                        resource_keys: Annotated[list[str], Field(max_length=16)] = [], task_id: str = "",
                        termination_grace_seconds: Annotated[float | None, Field(strict=True, ge=0, le=30, allow_inf_nan=False)] = None,
                        tty: Annotated[bool, Field(strict=True)] = False,
                        terminal_size: TerminalSize | None = None,
                        task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Start one explicitly authorized native command. Save its ID before launch and query that ID after uncertainty. Optional task_id is a legacy checkpoint association, not permission. Structured work requires current task_context; the original execution_id must equal operation_id. Check task_feedback for verified association or unavailable task status. Admission precedes dispatch; unknown outcomes never permit a new ID."""
        return _reply(lambda: application.start_execution(machine, execution_id, cwd, argv, timeout_seconds, expected_head_sha, workspace_access, resource_keys, task_id, termination_grace_seconds, tty, terminal_size.model_dump() if terminal_size is not None else None, _task_context(task_context)), execution_output=True)

    @guided_tool(annotations=machine_read)
    async def read_execution(machine: str, execution_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                             max_bytes: Annotated[int, Field(ge=1, le=65536)] = 65536,
                             wait_seconds: Annotated[float, Field(strict=True, ge=0, le=MAX_WAIT, allow_inf_nan=False)] = 0) -> CallToolResult:
        """Read the original receipt and raw-byte-cursor output. task_feedback reports association and verified task foreground state without changing the execution outcome; unavailable task metadata does not hide the receipt. Optional 0..5-second waiting never changes command timeout. At most 16 waiting readers; cancellation/disconnect affects observation only. Observe output loss/presentation flags; unknown evidence never permits replay."""
        if wait_seconds and not waiting.acquire(blocking=False):
            return _result({"error": {"code": "reader_busy", "message": "At most 16 waiting readers are admitted; immediate reads and cancellation remain available."}}, True)
        cancelled = threading.Event()
        operation = partial(_reply, lambda: application.read_execution(
            machine, execution_id, offset, max_bytes, wait_seconds, cancelled=cancelled), execution_output=True)
        try:
            if wait_seconds:
                try:
                    future = _submit_owned(operation)
                except BaseException:
                    waiting.release()
                    raise
                # A cancelled asyncio wrapper is not evidence that its thread stopped.
                # This callback runs only on real completion or pre-start cancellation.
                future.add_done_callback(lambda _: waiting.release())
                return await asyncio.wrap_future(future)
            return await asyncio.to_thread(operation)
        finally:
            cancelled.set()

    @guided_tool(annotations=machine_read)
    def list_executions(machine: str, cursor: str = "",
                        limit: Annotated[int, Field(ge=1, le=100)] = 20) -> CallToolResult:
        """Find original execution IDs in stable ID order. Follow next_cursor and unreadable records; this is not task recovery."""
        return _reply(lambda: application.list_executions(machine, cursor, limit))

    @guided_tool(annotations=WRITE)
    async def cancel_execution(machine: str, execution_id: str, task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Request cancellation of this owned execution only; read its terminal receipt. Does not undo previous side effects."""
        operation = partial(_reply, lambda: application.cancel_execution(machine, execution_id, _task_context(task_context)), execution_output=True)
        return await asyncio.wrap_future(_submit_owned(operation))

    @guided_tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    async def reconcile_execution(machine: str, execution_id: str) -> CallToolResult:
        """Verify cleanup for an original local execution with a known exit and retained native identity. Append bound evidence without rewriting its receipt, replaying commands, signaling processes or accepting Task stages. Missing identity or incomplete evidence remains unknown; read the original ID after a lost reply."""
        operation = partial(_reply, lambda: application.reconcile_execution(machine, execution_id), execution_output=True)
        return await asyncio.wrap_future(_submit_owned(operation))

    @guided_tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=has_ssh))
    async def control_execution(machine: str, execution_id: str,
                                expected_control_seq: Annotated[int, Field(strict=True, ge=0, le=MAX_CONTROL_SEQ - 1)],
                                action: Literal["write_stdin", "interrupt", "resize"],
                                text: Annotated[str | None, Field(max_length=MAX_INPUT)] = None,
                                terminal_size: TerminalSize | None = None,
                                task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Control the original worker using its observed sequence. PTY input is exact UTF-8, with no added newline or automatic confirmation. Interrupt is not cancellation. Identical retained retries return receipts; stale/conflicting or uncertain effects never replay. Read output separately. Launch permission is not blanket authority for later input; never send credentials."""
        operation = partial(_reply, lambda: application.control_execution(
            machine, execution_id, expected_control_seq, action, text,
            terminal_size.model_dump() if terminal_size is not None else None, _task_context(task_context)))
        return await asyncio.wrap_future(_submit_owned(operation))
