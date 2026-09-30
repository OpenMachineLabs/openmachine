"""Explicit local tools and optional GitHub operations on a private MCP service."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
import json
import os
from pathlib import Path
import socket
import stat
import signal
import sys
import threading
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolRequest, CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from openmachine import __version__, service
from openmachine.app import DEFAULT_TIMEOUT, MAX_TIMEOUT, MAX_WAIT, MAX_WAITERS, MAX_RESULT_BYTES, bound_output, MAX_CONTROL_SEQ, MAX_INPUT
from openmachine.app import MAX_WORKSPACE_RESULT, bound_workspace_result
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)
GITHUB_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
GITHUB_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


class ExactEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    old_text: Annotated[str, Field(min_length=1, max_length=131072)]
    new_text: Annotated[str, Field(max_length=131072)]


class TerminalSize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: Annotated[int, Field(strict=True, ge=1, le=1000)]
    cols: Annotated[int, Field(strict=True, ge=1, le=1000)]


def _result(payload: dict, failed: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                          structuredContent=payload, isError=failed)


def _reply(operation, *, execution_output: bool = False, workspace_output: bool = False, workspace_mutation: bool = False,
           change_id: str = "") -> CallToolResult:
    try:
        payload = operation()
    except Fault as error:
        payload = {**(error.evidence or {}), "error": {"code": error.code, "message": str(error)}}
    except OSError as error:
        payload = {"error": {"code": "source_unavailable", "message": type(error).__name__}}
    if change_id:
        payload.setdefault("change_id", change_id)
    failed = isinstance(payload.get("error"), dict) or workspace_mutation and payload.get("status") in {
        "rejected", "partial", "outcome_unknown", "write_outcome_unknown"} or (workspace_mutation and payload.get("reconciliation") == "outcome_unknown")
    if execution_output or workspace_output:
        bound, limit = (bound_workspace_result, MAX_WORKSPACE_RESULT) if workspace_output else (bound_output, MAX_RESULT_BYTES)
        try:
            payload = bound(payload, lambda value: len(json.dumps(_result(value, bool(failed)).model_dump(
                mode="json", by_alias=True, exclude_none=True)).encode()), limit - 1024)
        except Fault as error:
            payload = {**(error.evidence or {}), "error": {"code": error.code, "message": str(error)}}
            if change_id:
                payload["change_id"] = change_id
            failed = True
    return _result(payload, bool(failed))


def _submit_owned(operation) -> Future:
    """Keep one request independent of observation saturation and SDK lifespans."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="om-operation")
    try:
        return pool.submit(operation)
    finally:
        # Existing work finishes; no persistent pool or cross-request cleanup.
        pool.shutdown(wait=False)


def create_server(config: Settings, state: Path) -> FastMCP:
    application = create_application(config, state)
    waiting = threading.BoundedSemaphore(MAX_WAITERS)

    mcp = FastMCP(
        "Open Machine", instructions="Read inspect_skills(action='read') before operating this runtime.",
        stateless_http=True, json_response=True, max_request_body_size=1048576,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["localhost", "localhost:*"], allowed_origins=["http://localhost", "http://localhost:*"],
        ),
    )
    # The pinned FastMCP constructor does not expose the low-level version field.
    mcp._mcp_server.version = __version__

    private_directory(state)
    private_directory(state / "locks")

    has_ssh = any(item.transport == "ssh" for item in config.machines.values())
    machine_read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=has_ssh)
    file_write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=has_ssh)

    @mcp.tool(annotations=READ, structured_output=False)
    def get_capabilities() -> CallToolResult:
        """Report implemented limits and configured targets, not live health or future features."""
        return _reply(lambda: application.get_capabilities())

    @mcp.tool(annotations=machine_read, structured_output=False)
    def inspect_workspace(machine: str, workspace_name: str, action: Literal["files", "read", "search", "status", "diff"],
                          path: str = ".", offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=200)] = 100, query: str = "",
                          staged: bool = False, expected_snapshot: str = "") -> CallToolResult:
        """Read files, literal-search, or inspect Git status/diff. Echo snapshot between pages; heed skipped/truncated results."""
        return _reply(lambda: application.inspect_workspace(machine, workspace_name, action, path, offset, limit, query, staged, expected_snapshot))

    @mcp.tool(annotations=READ, structured_output=False)
    def inspect_skills(action: Literal["list", "read", "files"] = "list", skill: str = "", path: str = "",
                       query: Annotated[str, Field(max_length=200)] = "", offset: Annotated[int, Field(ge=0)] = 0,
                       limit: Annotated[int, Field(ge=1, le=100)] = 100, expected_snapshot: str = "") -> CallToolResult:
        """Read the bundled Skill or explicit user sources. Select exact discovered identities, not names. Sources are a Skill directory or a collection of immediate Skill directories, including whole-directory links. Resource reads never execute scripts or escape the selected Skill. Follow pages/snapshots and warnings; empty skill with action=read selects the bundled operating Skill."""
        return _reply(lambda: application.inspect_skills(action, skill, path, query, offset, limit, expected_snapshot))

    @mcp.tool(annotations=machine_read, structured_output=False)
    def read_workspace_file(machine: str, workspace_name: str, path: str,
                            offset: Annotated[int, Field(ge=0, le=131072)] = 0,
                            expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "") -> CallToolResult:
        """Read one configured-workspace text file, including untracked source, with whole-file SHA256, Git context and explicit absence. Follow next_offset and echo expected_sha256 until has_more=false; never overwrite a file from partial/redacted text. Bounded UTF-8 pages; no writes or submitted-content execution. Configured SSH workspaces are read on their pinned remote target."""
        return _reply(lambda: application.read_workspace_file(machine, workspace_name, path, offset, expected_sha256), workspace_output=True)

    @mcp.tool(annotations=file_write, structured_output=False)
    async def write_workspace_file(machine: str, workspace_name: str, path: str,
                             content: Annotated[str, Field(max_length=131072)],
                             expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                             expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")],
                             change_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                             expected_workspace_identity: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "") -> CallToolResult:
        """Publish one bounded whole file. A change_id requires observed workspace identity and retains the original result; no-ID calls retain legacy behavior without durable recovery. Empty source hash means absence. Uses the independent effective edit grant and the original workspace lease; no commands, parents or implicit replay. Unknown effects require original-ID readback. Native checks are not a kernel CAS or sandbox."""
        operation = partial(_reply, lambda: application.write_workspace_file(machine, workspace_name, path, content,
            expected_head_sha, expected_sha256, change_id, expected_workspace_identity), workspace_output=True,
            workspace_mutation=True, change_id=change_id)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @mcp.tool(annotations=file_write, structured_output=False)
    async def edit_workspace_file(machine: str, workspace_name: str, path: str,
                            edits: Annotated[list[ExactEdit], Field(min_length=1, max_length=64)],
                            expected_workspace_identity: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
                            expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                            expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
                            change_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                            dry_run: Annotated[bool, Field(strict=True)] = False,
                            expected_plan_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "") -> CallToolResult:
        """Replace exact non-overlapping fragments located in the same original UTF-8 file. Preview requires empty ID and has no writes; apply requires a caller-generated ID. Echo observed whole-file hash, HEAD and workspace identity; optionally bind preview plan SHA. Same ID/request observes, never replays; ambiguous matches fail. W1 sources/payloads remain 128 KiB. File changes are not test results."""
        operation = partial(_reply, lambda: application.edit_workspace_file(machine, workspace_name, path,
            [edit.model_dump() for edit in edits], expected_workspace_identity, expected_head_sha, expected_sha256,
            change_id, dry_run, expected_plan_sha256), workspace_output=True, workspace_mutation=True, change_id=change_id)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @mcp.tool(annotations=machine_read, structured_output=False)
    def read_workspace_change(machine: str, change_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                              observe_current: Annotated[bool, Field(strict=True)] = False) -> CallToolResult:
        """Read original target-owned change evidence without applying, repairing or initializing state. Optional current hashes are separate observations, not historical proof. Missing history is not non-admission; do not retry blindly. Original root must remain authorized. No write grant is required."""
        return _reply(lambda: application.read_workspace_change(machine, change_id, observe_current),
                      workspace_output=True, change_id=change_id)

    @mcp.tool(annotations=READ, structured_output=False)
    def find_tasks(query: Annotated[str, Field(max_length=200)] = "", anchors: Annotated[list[str], Field(max_length=16)] = [],
                   task_id: str = "", offset: Annotated[int, Field(ge=0)] = 0,
                   limit: Annotated[int, Field(ge=1, le=50)] = 20,
                   expected_generation: Annotated[int, Field(ge=-1)] = -1) -> CallToolResult:
        """Find private checkpoints by exact task/anchors or title/ID keywords. Anchor filters intersect; descriptive results are candidates, never continuation authority. Follow pagination/generation. Reads do not initialize state or start work; missing/corrupt history is explicit."""
        return _reply(lambda: application.find_tasks(query, anchors, task_id, offset, limit, expected_generation))

    @mcp.tool(annotations=READ, structured_output=False)
    def read_task(task_id: str) -> CallToolResult:
        """Read the original bounded checkpoint and revision, verify its index hash, then recheck current authority, Git and execution receipts before continuation. This does not resume an agent or execute work."""
        return _reply(lambda: application.read_task(task_id))

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=False)
    def save_task(task_id: str, expected_revision: Annotated[int, Field(ge=0)],
                  title: Annotated[str, Field(min_length=1, max_length=200)],
                  body: Annotated[str, Field(min_length=1, max_length=32768)],
                  anchors: Annotated[list[str], Field(max_length=16)] = []) -> CallToolResult:
        """Save one private material checkpoint and lookup metadata. Expected revision zero creates; updates compare the observed revision under a store lock. Identical last-write retries are no-ops; stale/corrupt state is not overwritten. Lookup anchors are additive; omitting them preserves prior references. Keep goals, authority references, original executions, evidence and next action, not credentials, transcripts or hidden reasoning. No code execution, external publication or automatic resume. Torn two-file saves are reported, not silently repaired."""
        return _reply(lambda: application.save_task(task_id, expected_revision, title, body, anchors))

    @mcp.tool(annotations=WRITE, structured_output=False)
    def start_execution(machine: str, execution_id: str, cwd: str,
                        argv: Annotated[list[str], Field(min_length=1, max_length=100)],
                        timeout_seconds: Annotated[int, Field(ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
                        expected_head_sha: str = "", workspace_access: Literal["exclusive", "shared"] = "exclusive",
                        resource_keys: Annotated[list[str], Field(max_length=16)] = [], task_id: str = "",
                        termination_grace_seconds: Annotated[float | None, Field(strict=True, ge=0, le=30, allow_inf_nan=False)] = None,
                        tty: Annotated[bool, Field(strict=True)] = False,
                        terminal_size: TerminalSize | None = None) -> CallToolResult:
        """Start one explicitly authorized native command. Save its ID before launch and query that ID after uncertainty. Optional task_id references an existing checkpoint; it never writes task progress or supplies authorization."""
        return _reply(lambda: application.start_execution(machine, execution_id, cwd, argv, timeout_seconds, expected_head_sha, workspace_access, resource_keys, task_id, termination_grace_seconds, tty, terminal_size.model_dump() if terminal_size is not None else None), execution_output=True)

    @mcp.tool(annotations=machine_read, structured_output=False)
    async def read_execution(machine: str, execution_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                             max_bytes: Annotated[int, Field(ge=1, le=65536)] = 65536,
                             wait_seconds: Annotated[float, Field(strict=True, ge=0, le=MAX_WAIT, allow_inf_nan=False)] = 0) -> CallToolResult:
        """Read the original receipt and raw-byte-cursor output. Optional 0..5-second waiting wakes on output/state/exit, never changes command timeout. At most 16 waiting readers per server; reader_busy rejects overflow. Cancellation/disconnect affects observation only. Observe output loss/presentation flags; unknown target/worker evidence never permits replay."""
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

    @mcp.tool(annotations=machine_read, structured_output=False)
    def list_executions(machine: str, cursor: str = "",
                        limit: Annotated[int, Field(ge=1, le=100)] = 20) -> CallToolResult:
        """Find original execution IDs in stable ID order. Follow next_cursor and unreadable records; this is not task recovery."""
        return _reply(lambda: application.list_executions(machine, cursor, limit))

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def cancel_execution(machine: str, execution_id: str) -> CallToolResult:
        """Request cancellation of this owned execution only; read its terminal receipt. Does not undo previous side effects."""
        operation = partial(_reply, lambda: application.cancel_execution(machine, execution_id), execution_output=True)
        return await asyncio.wrap_future(_submit_owned(operation))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=has_ssh), structured_output=False)
    async def control_execution(machine: str, execution_id: str,
                                expected_control_seq: Annotated[int, Field(strict=True, ge=0, le=MAX_CONTROL_SEQ - 1)],
                                action: Literal["write_stdin", "interrupt", "resize"],
                                text: Annotated[str | None, Field(max_length=MAX_INPUT)] = None,
                                terminal_size: TerminalSize | None = None) -> CallToolResult:
        """Control the original worker using its observed sequence. PTY input is exact UTF-8, with no added newline or automatic confirmation. Interrupt is not cancellation. Identical retained retries return receipts; stale/conflicting or uncertain effects never replay. Read output separately. Launch permission is not blanket authority for later input; never send credentials."""
        operation = partial(_reply, lambda: application.control_execution(
            machine, execution_id, expected_control_seq, action, text,
            terminal_size.model_dump() if terminal_size is not None else None))
        return await asyncio.wrap_future(_submit_owned(operation))

    if config.github is not None:
        github = application.github
        @mcp.tool(annotations=GITHUB_READ, structured_output=False)
        def github_read(action: github.ReadAction, repository: str = "", number: Annotated[int, Field(ge=0)] = 0,
                        ref: str = "", path: str = "", page: Annotated[int, Field(ge=1, le=1000)] = 1,
                        limit: Annotated[int, Field(ge=1, le=100)] = 30, cursor: Annotated[str, Field(max_length=1024)] = "",
                        expected_head_sha: str = "", expected_base_sha: str = "",
                        offset: Annotated[int, Field(ge=0, le=1048576)] = 0) -> CallToolResult:
            """Read scoped GitHub identity, repositories, Issues and version-bound PR evidence. Read pull_request before candidate pages; echo both HEAD/base on continuation. Follow next_page/next_cursor and evidence gaps. checks uses an exact commit ref. file uses a fixed commit and 16 KiB UTF-8 byte pages; no symlink/credential reads. API patches are not proof of complete diffs. status probes the existing gh identity without credentials or login changes. merge_context observes gates and never merges. No arbitrary endpoint or mutation dispatch."""
            return _reply(lambda: application.github_read(action, repository, number, ref, path, page, limit, cursor, expected_head_sha, expected_base_sha, offset))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_issue(action: Literal["create", "update"], repository: str,
                         title: Annotated[str | None, Field(max_length=256)] = None,
                         body: Annotated[str | None, Field(max_length=60000)] = None,
                         number: Annotated[int, Field(ge=0)] = 0,
                         state: Literal["open", "closed"] | None = None, expected_updated_at: str = "") -> CallToolResult:
            """Create or update an authorized Issue with field/identity readback and preserved provenance. state is update-only. Supply observed updated_at to detect stale edits; this is not atomic CAS. Creation is not idempotent: reconcile an uncertain destination before retrying."""
            return _reply(lambda: application.github_issue(action, repository, title, body, number, state, expected_updated_at))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_pull_request(action: Literal["create", "update"], repository: str,
                                title: Annotated[str | None, Field(max_length=256)] = None,
                                body: Annotated[str | None, Field(max_length=60000)] = None,
                                number: Annotated[int, Field(ge=0)] = 0,
                                head: str = "", base: str = "", draft: bool = False,
                                state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                                expected_head_sha: str = "") -> CallToolResult:
            """Create/update a scoped PR using the same gh connection. Prefer expected_head_sha and observed updated_at. Pinned creation uses an existing same-repository branch. Readback verifies supplied fields and identity; uncertain creation must not be repeated. Local commits and push stay with native execution."""
            return _reply(lambda: application.github_pull_request(action, repository, title, body, number, head, base, draft, state, expected_updated_at, expected_head_sha))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_comment(action: Literal["create", "update"], repository: str,
                           body: Annotated[str, Field(max_length=60000)], number: Annotated[int, Field(ge=0)] = 0,
                           comment_id: Annotated[int, Field(ge=0)] = 0, expected_updated_at: str = "") -> CallToolResult:
            """Create/update a top-level Issue/PR comment and verify its fields. Inline comments are outside this writer. Preserve the original comment identity after uncertainty; do not post it again. updated_at detects an observed edit conflict without promising atomic CAS."""
            return _reply(lambda: application.github_comment(action, repository, body, number, comment_id, expected_updated_at))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_review(action: Literal["create", "update"], repository: str,
                          body: Annotated[str, Field(max_length=60000)],
                          number: Annotated[int, Field(ge=0)] = 0, review_id: Annotated[int, Field(ge=0)] = 0,
                          expected_head_sha: str = "",
                          event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT") -> CallToolResult:
            """Submit an exact-HEAD review summary after reading the complete relevant diff and discussions, or update its body. Missing gate evidence rejects creation; known failures can still be reviewed. Self-review must use COMMENT and is not independent approval. Uncertain writes require original-review readback, never blind retry."""
            return _reply(lambda: application.github_review(action, repository, body, number, review_id, expected_head_sha, event))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_merge(repository: str, number: Annotated[int, Field(ge=1)],
                         expected_head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")],
                         expected_base_sha: str = "", method: Literal["squash", "merge", "rebase"] = "squash",
                         commit_title: Annotated[str, Field(max_length=256)] = "",
                         commit_message: Annotated[str, Field(max_length=60000)] = "") -> CallToolResult:
            """Merge only an explicitly authorized, current-HEAD reviewed PR with complete checks/discussions and clean native policy. No bypass, auto-merge or queue support. Bind both observed SHAs when available. Only merged_verified confirms the actual commit on the destination branch; pending/unknown results require readback, not resubmission."""
            return _reply(lambda: application.github_merge(repository, number, expected_head_sha, expected_base_sha, method, commit_title, commit_message))

    sdk_handler = mcp._mcp_server.request_handlers[CallToolRequest]

    async def safe_tool_call(request):
        # Keep SDK schema validation, but never echo rejected argument values.
        result = await sdk_handler(request)
        response = result.root
        if isinstance(response, CallToolResult) and response.isError and response.structuredContent is None:
            payload = {"error": {"code": "invalid_tool_call",
                                 "message": "Check the tool name and arguments against tools/list; no success was verified."}}
            response.content = [TextContent(type="text", text=json.dumps(payload))]
            response.structuredContent = payload
        return result

    mcp._mcp_server.request_handlers[CallToolRequest] = safe_tool_call
    return mcp


def serve(config: Settings, state: Path, *, managed: bool = False) -> None:
    state = private_directory(state)
    path = state / "mcp.sock"
    locks = private_directory(state / "locks")
    with lease(locks / "listener.lock"), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        if managed and os.path.lexists(path):
            identity = path.lstat()
            if (not stat.S_ISSOCK(identity.st_mode) or identity.st_uid != os.getuid()
                    or identity.st_mode & 0o077 or service._live_socket(path)):
                raise Fault("socket_in_use", "The existing socket is live or has unknown ownership; it was not replaced.")
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise Fault("socket_changed", "The listener changed during recovery; it was not removed.")
            path.unlink()
        # Manual serve still refuses stale paths. The lease coordinates current servers.
        listener.bind(str(path))
        identity = path.stat()
        os.chmod(path, 0o600)
        listener.listen(128)
        # Uvicorn re-raises SIGTERM after shutdown. Unwind Python cleanup first.
        previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            app = create_server(config, state).streamable_http_app()
            uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning")).run(sockets=[listener])
        finally:
            signal.signal(signal.SIGTERM, previous)
            if path.exists() and path.stat().st_ino == identity.st_ino:
                path.unlink()
