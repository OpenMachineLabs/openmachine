"""Explicit local tools and optional GitHub operations on a private MCP service."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
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

from jsonschema import Draft202012Validator
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import uvicorn

from openmachine import __version__, service, skills, tasks
from openmachine.app import DEFAULT_TIMEOUT, MAX_TIMEOUT, MAX_WAIT, MAX_WAITERS, MAX_RESULT_BYTES, bound_output, MAX_CONTROL_SEQ, MAX_INPUT, MAX_TASK_WAIT
from openmachine.app import MAX_SOURCE, MAX_WORKSPACE_RESULT, MAX_TASK_RESULT_BYTES, bound_workspace_result
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
LOCAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
GITHUB_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
GITHUB_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)


class ExactEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    old_text: Annotated[str, Field(min_length=1, max_length=131072)]
    new_text: Annotated[str, Field(max_length=131072)]


class PatchPrecondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")]


class TerminalSize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: Annotated[int, Field(strict=True, ge=1, le=1000)]
    cols: Annotated[int, Field(strict=True, ge=1, le=1000)]


GitHubNames = Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=100)]


def _task_context(value: tasks.TaskContext | None) -> dict | None:
    return value.model_dump() if value is not None else None


def _result(payload: dict, failed: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                          structured_content=payload, is_error=failed)


def _reply(operation, *, execution_output: bool = False, workspace_output: bool = False, workspace_mutation: bool = False,
           change_id: str = "", task_output: bool = False) -> CallToolResult:
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
    result = _result(payload, bool(failed))
    if task_output and len(json.dumps(result.model_dump(
            mode="json", by_alias=True, exclude_none=True)).encode()) > MAX_TASK_RESULT_BYTES - 1024:
        return _result({"task_id": payload.get("task_id"), "error": {
            "code": "task_response_limit", "message": "The task response exceeded its wire budget. "
            "Preserve original task and operation IDs; request a smaller history page."}}, True)
    return result


def _submit_owned(operation) -> Future:
    """Keep one request independent of observation saturation and SDK lifespans."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="om-operation")
    try:
        return pool.submit(operation)
    finally:
        # Existing work finishes; no persistent pool or cross-request cleanup.
        pool.shutdown(wait=False)


def create_server(config: Settings, state: Path) -> MCPServer:
    application = create_application(config, state)
    waiting = threading.BoundedSemaphore(MAX_WAITERS)

    @asynccontextmanager
    async def lifespan(_server):
        # SDK 2.2 enters this once per HTTP manager, not per request/session.
        with application.connections.running(), application.task_observer.running():
            yield {}

    validators: dict[str, Draft202012Validator] = {}

    async def safe_tool_call(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        invalid = _result({"error": {"code": "invalid_tool_call",
            "message": "Check the tool name and arguments against tools/list; no success was verified."}}, True).model_dump(
                mode="json", by_alias=True, exclude_none=True, exclude={"result_type"})
        # Middleware short-circuits bypass the SDK's per-version envelope serializer.
        if ctx.protocol_version == "2026-07-28":
            invalid.update(resultType="complete", _meta={
                "io.modelcontextprotocol/serverInfo": {"name": mcp.name, "version": mcp.version}})
        # v1 validated JSON schema before Pydantic coercion. Preserve this boundary
        # using the public catalog, without modifying the SDK handler registry.
        if not validators:
            validators.update({tool.name: Draft202012Validator(tool.input_schema)
                               for tool in await mcp.list_tools()})
        params = ctx.params
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return invalid
        validator = validators.get(params["name"])
        arguments = params.get("arguments")
        if validator is None or not validator.is_valid({} if arguments is None else arguments):
            return invalid
        # The new contract forbids extra fields before SDK argument filtering.
        guarded = {"read_task", "wait_task", "start_codex_task", "read_codex_task", "create_task", "control_task", "start_execution", "create_worktree", "cancel_execution", "control_execution",
                   "write_workspace_file", "edit_workspace_file", "apply_workspace_patch", "github_issue",
                   "github_pull_request", "github_comment", "github_review", "github_review_thread", "github_merge",
                   "list_integrations", "connect_integration", "read_integration_login",
                   "cancel_integration_login", "disconnect_integration"}
        if (params["name"] in guarded or "task_context" in (arguments or {})) and set(arguments or {}) - validator.schema["properties"].keys():
            return invalid
        try:
            response = await call_next(ctx)
        except ValidationError:
            return invalid
        if (isinstance(response, dict) and response.get("isError") is True
                and response.get("structuredContent") is None):
            return invalid
        return response

    mcp = MCPServer(
        "Open Machine", instructions="Read inspect_skills(action='read') for the operating overview. "
        "Each tool's Guide gives an exact bounded read for its instructions; guides never grant permission.",
        version=__version__, middleware=[safe_tool_call], lifespan=lifespan,
    )

    def guided_tool(*, annotations):
        def register(operation):
            operation.__doc__ = skills.describe_tool(operation.__name__, operation.__doc__ or "")
            return mcp.tool(annotations=annotations, structured_output=False)(operation)
        return register

    private_directory(state)
    private_directory(state / "locks")

    has_ssh = any(item.transport == "ssh" for item in config.machines.values())
    machine_read = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=has_ssh)
    file_write = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=has_ssh)

    @guided_tool(annotations=READ)
    def get_capabilities() -> CallToolResult:
        """Report implemented limits and configured targets, not live health or future features."""
        return _reply(lambda: application.get_capabilities())

    @guided_tool(annotations=READ)
    def list_integrations(provider: Annotated[str, Field(max_length=64)] = "",
                          connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
        """List installed connection methods and retained accounts without network access or state writes. Saved connection status is not live provider health."""
        return _reply(lambda: application.list_integrations(provider, connection_id))

    @guided_tool(annotations=WRITE)
    async def connect_integration(request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                                  provider: Annotated[str, Field(max_length=64)] = "github",
                                  method: Annotated[str, Field(max_length=64)] = "device_code",
                                  connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
        """Start native GitHub Device Flow owned by this runtime. Return only the public verification URL/code; credentials never enter tool results. Preserve request_id after uncertainty."""
        operation = lambda: _reply(lambda: application.connect_integration(request_id, provider, method, connection_id))
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=READ)
    def read_integration_login(login_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> CallToolResult:
        """Read the original login result without restarting it. A runtime restart interrupts unfinished Device Flow but preserves established connections."""
        return _reply(lambda: application.read_integration_login(login_id))

    @guided_tool(annotations=LOCAL_WRITE)
    def cancel_integration_login(login_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> CallToolResult:
        """Cancel one unfinished native login. This does not disconnect an established account or undo provider-side consent."""
        return _reply(lambda: application.cancel_integration_login(login_id))

    @guided_tool(annotations=LOCAL_WRITE)
    async def disconnect_integration(connection_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                                     expected_revision: Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]) -> CallToolResult:
        """Disconnect only this saved OM connection using its observed authorization revision. Native credentials are removed from their pinned local store; remote GitHub grants are not claimed revoked."""
        operation = lambda: _reply(lambda: application.disconnect_integration(connection_id, expected_revision))
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=machine_read)
    def inspect_workspace(machine: str, workspace_name: str, action: Literal["files", "read", "search", "status", "diff", "worktrees"],
                          path: str = ".", offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=200)] = 100, query: str = "",
                          staged: bool = False, expected_snapshot: str = "",
                          path_glob: Annotated[str, Field(max_length=1024)] = "",
                          worktree: Annotated[str, Field(max_length=4096)] = "") -> CallToolResult:
        """Read files, search, Git status/diff or registered worktrees. Optional worktree selects an authorized registered root for this call only; omitted uses the configured root. Listing is not a claim or proof a tree is idle. path_glob filters workspace-relative files/search (* never crosses /; no **). Echo snapshot between pages; heed skipped/truncated results."""
        return _reply(lambda: application.inspect_workspace(machine, workspace_name, action, path, offset, limit, query, staged, expected_snapshot, path_glob, worktree), workspace_output=True)

    @guided_tool(annotations=READ)
    def inspect_skills(action: Literal["list", "read", "files"] = "list", skill: str = "", path: str = "",
                       query: Annotated[str, Field(max_length=200)] = "", offset: Annotated[int, Field(ge=0)] = 0,
                       limit: Annotated[int, Field(ge=1, le=100)] = 100, expected_snapshot: str = "",
                       tool: Annotated[str, Field(max_length=128)] = "") -> CallToolResult:
        """List, read or enumerate explicit Skills. tool selects an exact built-in guide for list/read; conflicting selectors fail. Otherwise use discovered identities, not display names. Follow pages, snapshots and warnings. Omitted skill reads the operating overview. Resources never execute scripts."""
        return _reply(lambda: application.inspect_skills(action, skill, path, query, offset, limit, expected_snapshot, tool))

    @guided_tool(annotations=machine_read)
    def read_workspace_file(machine: str, workspace_name: str, path: str,
                            offset: Annotated[int, Field(ge=0, le=MAX_SOURCE)] = 0,
                            expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                            start_line: Annotated[int | None, Field(strict=True, ge=1)] = None,
                            line_count: Annotated[int, Field(strict=True, ge=1, le=500)] = 200,
                            worktree: Annotated[str, Field(max_length=4096)] = "") -> CallToolResult:
        """Read bounded UTF-8 source with whole-file SHA256/context and 32 KiB pages. start_line is 1-based and requires offset=0. Continue long lines using next_offset and the same SHA; next_line exists only at a known boundary. Heed range_complete/has_more and redaction metadata; never reconstruct full files from partial/redacted pages. SSH reads stay on the pinned target."""
        return _reply(lambda: application.read_workspace_file(machine, workspace_name, path, offset, expected_sha256, start_line, line_count, worktree), workspace_output=True)

    @guided_tool(annotations=file_write)
    async def write_workspace_file(machine: str, workspace_name: str, path: str,
                             content: Annotated[str, Field(max_length=131072)],
                             expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                             expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")],
                             change_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                             expected_workspace_identity: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                             task_context: tasks.TaskContext | None = None,
                             worktree: Annotated[str, Field(max_length=4096)] = "") -> CallToolResult:
        """Publish one bounded whole file under the effective edit grant and original workspace lease. change_id requires observed workspace identity; no-ID calls have no durable recovery. Empty source hash means absence. No parent creation or implicit replay. After uncertainty read the original ID. Native checks are not a kernel CAS or sandbox."""
        operation = partial(_reply, lambda: application.write_workspace_file(machine, workspace_name, path, content,
            expected_head_sha, expected_sha256, change_id, expected_workspace_identity, _task_context(task_context), worktree), workspace_output=True,
            workspace_mutation=True, change_id=change_id)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=file_write)
    async def edit_workspace_file(machine: str, workspace_name: str, path: str,
                            edits: Annotated[list[ExactEdit], Field(min_length=1, max_length=64)],
                            expected_workspace_identity: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
                            expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                            expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
                            change_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                            dry_run: Annotated[bool, Field(strict=True)] = False,
                            expected_plan_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                            task_context: tasks.TaskContext | None = None,
                            worktree: Annotated[str, Field(max_length=4096)] = "") -> CallToolResult:
        """Replace exact non-overlapping fragments in the same original UTF-8 file; ambiguous matches fail. Preview requires empty ID; apply requires a caller-generated ID. Echo whole-file hash, HEAD and workspace identity; optionally bind preview plan SHA. Same ID/request observes, never replays. Combined replacement strings are bounded to 128 KiB. File changes are not test results."""
        operation = partial(_reply, lambda: application.edit_workspace_file(machine, workspace_name, path,
            [edit.model_dump() for edit in edits], expected_workspace_identity, expected_head_sha, expected_sha256,
            change_id, dry_run, expected_plan_sha256, _task_context(task_context), worktree), workspace_output=True, workspace_mutation=True, change_id=change_id)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=file_write)
    async def apply_workspace_patch(machine: str, workspace_name: str,
                            patch: Annotated[str, Field(max_length=131072)],
                            expected_workspace_identity: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
                            expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                            preconditions: Annotated[list[PatchPrecondition], Field(min_length=1, max_length=32)],
                            create_parents: Annotated[bool, Field(strict=True)] = False,
                            change_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                            dry_run: Annotated[bool, Field(strict=True)] = False,
                            expected_plan_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                            task_context: tasks.TaskContext | None = None,
                            worktree: Annotated[str, Field(max_length=4096)] = "") -> CallToolResult:
        """Apply a strict om_patch_v1 plan with exact preconditions on every endpoint; empty hash means absence. Preview writes nothing and requires empty ID. Apply requires a caller-generated ID; identical retained requests never replay. Publication is ordered, not atomic. Partial/unknown outcomes require original-evidence readback, never automatic retry or rollback. File changes are not test results."""
        operation = partial(_reply, lambda: application.apply_workspace_patch(machine, workspace_name, patch,
            expected_workspace_identity, expected_head_sha, [item.model_dump() for item in preconditions],
            create_parents, change_id, dry_run, expected_plan_sha256, _task_context(task_context), worktree), workspace_output=True,
            workspace_mutation=True, change_id=change_id)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=machine_read)
    def read_workspace_change(machine: str, change_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                              observe_current: Annotated[bool, Field(strict=True)] = False) -> CallToolResult:
        """Read original target-owned change evidence without applying, repairing or initializing state. Optional current hashes are separate observations, not historical proof. Missing history is not non-admission; do not retry blindly. Original root must remain authorized. No write grant is required."""
        return _reply(lambda: application.read_workspace_change(machine, change_id, observe_current),
                      workspace_output=True, change_id=change_id)

    @guided_tool(annotations=WRITE)
    def create_task(task_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                    update_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")],
                    title: Annotated[str, Field(min_length=1, max_length=200)],
                    anchors: Annotated[list[str], Field(max_length=16)], origin: tasks.Origin, contract: tasks.Contract,
                    stages: Annotated[list[tasks.StageDefinition], Field(min_length=1, max_length=16)]) -> CallToolResult:
        """Create an authorized structured task; preserve readable instructions and inputs with correctly hashed stage sources. Preserve task/update IDs after uncertainty; committed creation is not business permission. Explicit claim/current task_context remain required. Never convert legacy history or supply runtime facts."""
        return _reply(lambda: application.create_task(task_id, update_id, title, anchors, origin.model_dump(),
                      contract.model_dump(), [stage.model_dump() for stage in stages]), task_output=True)

    @guided_tool(annotations=WRITE)
    async def control_task(task_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                           expected_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                           expected_plan_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                           expected_epoch: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)],
                           update_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")],
                           transition: tasks.Transition) -> CallToolResult:
        """Control the original task using exact task/plan/epoch revisions; retain update_id after uncertainty. Renew the original claim_id/principal/epoch. Expired authority requires a revised contract with fresh evidence. bind_effect_scope needs original normalized arguments and never resolves an unknown effect. Claims grant no permission. Pause blocks new admission; cancellation preserves admitted/unknown effects. Finish requires action, outcome, acceptance references to accepted original evidence and limitations. Inspect returned state and observation blockers."""
        operation = partial(_reply, lambda: application.control_task(task_id, expected_revision,
            expected_plan_revision, expected_epoch, update_id, transition.model_dump()), task_output=True)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=READ)
    def find_tasks(query: Annotated[str, Field(max_length=200)] = "", anchors: Annotated[list[str], Field(max_length=16)] = [],
                   task_id: str = "", offset: Annotated[int, Field(ge=0)] = 0,
                   limit: Annotated[int, Field(ge=1, le=50)] = 20,
                   expected_generation: Annotated[int, Field(ge=-1)] = -1) -> CallToolResult:
        """Find private checkpoints by exact task/anchors or title/ID keywords. Anchor filters intersect; descriptive results are candidates, never continuation authority. Follow pagination/generation. Reads do not initialize state or start work; missing/corrupt history is explicit. resume_authorized=false means this lookup grants no authority, not that current user authorization is denied."""
        return _reply(lambda: application.find_tasks(query, anchors, task_id, offset, limit, expected_generation), task_output=True)

    @guided_tool(annotations=READ)
    def read_task(task_id: str,
                  section: Literal["summary", "operations", "history", "evidence"] = "summary",
                  cursor: Annotated[str, Field(max_length=1024)] = "",
                  limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20,
                  operation_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                  stage_id: Annotated[str, Field(max_length=64, pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63})?$")] = "",
                  decision_version: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)] = 0) -> CallToolResult:
        """Read schema v2 current state or a bounded operations/history/evidence page. Summary omits cumulative history; follow complete/has_more, cursor and revision watermarks. Use the exact original operation_id, or an evidence_ref's stage_id and decision_version together on evidence without cursor/operation_id. Reads never initialize, repair, claim or execute work."""
        return _reply(lambda: application.read_task(task_id, section=section,
                      cursor=cursor, limit=limit, operation_id=operation_id,
                      stage_id=stage_id, decision_version=decision_version), task_output=True)

    @guided_tool(annotations=READ)
    async def wait_task(task_id: Annotated[str, Field(min_length=1, max_length=64,
                        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                        expected_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                        wait_seconds: Annotated[float, Field(strict=True, ge=0, le=MAX_TASK_WAIT,
                        allow_inf_nan=False)] = MAX_TASK_WAIT) -> CallToolResult:
        """Wait once for bounded structured-task progress. Renewal required returns immediately for guarded same-claim renewal without ending the turn. Attention or wait expiry sets foreground.stop_foreground and requires ending the turn. This read never claims, renews, retries, cancels, or extends original work."""
        if wait_seconds and not waiting.acquire(blocking=False):
            return _result({"error": {"code": "reader_busy",
                "message": "At most 16 bounded waits are admitted; immediate task reads and cancellation remain available."}}, True)
        cancelled = threading.Event()
        operation = partial(_reply, lambda: application.wait_task(
            task_id, expected_revision, wait_seconds, cancelled=cancelled), task_output=True)
        try:
            if wait_seconds:
                try:
                    future = _submit_owned(operation)
                except BaseException:
                    waiting.release()
                    raise
                future.add_done_callback(lambda _: waiting.release())
                return await asyncio.wrap_future(future)
            return await asyncio.to_thread(operation)
        finally:
            cancelled.set()

    @guided_tool(annotations=LOCAL_WRITE)
    def save_task(task_id: str, expected_revision: Annotated[int, Field(ge=0)],
                  title: Annotated[str, Field(min_length=1, max_length=200)],
                  body: Annotated[str, Field(min_length=1, max_length=32768)],
                  anchors: Annotated[list[str], Field(max_length=16)] = []) -> CallToolResult:
        """Save an existing legacy workflow, not a parallel structured-task checkpoint or conversion. Revision zero creates; updates require observed revision. Identical last-write retries are no-ops; stale/corrupt state is not overwritten. Anchors are additive. Retain authority/evidence/next action, never credentials or hidden reasoning. Torn saves stay explicit; saving does not execute, publish or resume work."""
        return _reply(lambda: application.save_task(task_id, expected_revision, title, body, anchors), task_output=True)

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

    if config.github is not None:
        github = application.github
        @guided_tool(annotations=GITHUB_READ)
        def github_read(action: github.ReadAction, repository: str = "", number: Annotated[int, Field(ge=0)] = 0,
                        ref: str = "", path: str = "", page: Annotated[int, Field(ge=1, le=1000)] = 1,
                        limit: Annotated[int, Field(ge=1, le=100)] = 30, cursor: Annotated[str, Field(max_length=1024)] = "",
                        expected_head_sha: str = "", expected_base_sha: str = "",
                        offset: Annotated[int, Field(ge=0, le=1048576)] = 0, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Read scoped GitHub evidence. Read pull_request before candidate pages; echo HEAD/base on continuation and follow pagination/evidence gaps. checks requires exact commit ref; file uses a fixed commit and 16 KiB UTF-8 pages, with no symlink/credential reads. API patches may be incomplete. status/merge_context only observe; no arbitrary endpoint or mutation dispatch."""
            return _reply(lambda: application.github_read(action, repository, number, ref, path, page, limit, cursor, expected_head_sha, expected_base_sha, offset, connection_id=connection_id))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_issue(action: Literal["create", "update"], repository: str,
                         title: Annotated[str | None, Field(max_length=256)] = None,
                         body: Annotated[str | None, Field(max_length=60000)] = None,
                         number: Annotated[int, Field(ge=0)] = 0,
                         state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                         labels: GitHubNames | None = None, assignees: GitHubNames | None = None,
                         task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                         expected_fields: dict | None = None) -> CallToolResult:
            """Create/update an authorized Issue with readback. labels/assignees replace complete sets; omit to preserve, [] to clear. state is update-only. Update expected_fields must equal the observed values of every replaced field and no others; expected_updated_at adds a whole-object guard. Checks are sequential, not atomic CAS. Creation is non-idempotent: reconcile the original destination after uncertainty."""
            return _reply(lambda: application.github_issue(action, repository, title, body, number, state, expected_updated_at, labels, assignees, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_pull_request(action: Literal["create", "update", "ready_for_review"], repository: str,
                                title: Annotated[str | None, Field(max_length=256)] = None,
                                body: Annotated[str | None, Field(max_length=60000)] = None,
                                number: Annotated[int, Field(ge=0)] = 0,
                                head: str = "", base: str = "", draft: bool = False,
                                state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                                expected_head_sha: str = "", labels: GitHubNames | None = None,
                                assignees: GitHubNames | None = None, task_context: tasks.TaskContext | None = None,
                                connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                                expected_fields: dict | None = None) -> CallToolResult:
            """Create/update a scoped PR or mark an observed Draft ready. ready_for_review requires exact HEAD and accepts only number, optional draft/updated_at guards and connection identity; concurrent pushes can precede it and changed readback stays unverified. Updates replace complete labels/assignees sets (omit preserves, [] clears); keep metadata separate from title/body/state. expected_fields must cover exactly replaced observed fields; ready uses only draft. expected_updated_at is an extra guard; none is atomic CAS. Creation accepts neither metadata nor expected_fields and uses an existing same-repository branch. Reconcile the original destination after uncertainty; never blindly retry."""
            return _reply(lambda: application.github_pull_request(action, repository, title, body, number, head, base, draft, state, expected_updated_at, expected_head_sha, labels, assignees, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_comment(action: Literal["create", "update"], repository: str,
                           body: Annotated[str, Field(max_length=60000)], number: Annotated[int, Field(ge=0)] = 0,
                           comment_id: Annotated[int, Field(ge=0)] = 0, expected_updated_at: str = "",
                           task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                           expected_fields: dict | None = None) -> CallToolResult:
            """Create/update a top-level Issue/PR comment and verify its fields. Inline comments are outside this writer. Preserve the original comment identity after uncertainty; do not post it again. On update, expected_fields contains only the observed body value. Supplied expected_updated_at remains an additional whole-object guard. These detect observed edit conflicts without promising atomic CAS."""
            return _reply(lambda: application.github_comment(action, repository, body, number, comment_id, expected_updated_at, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_review(action: Literal["create", "update"], repository: str,
                          body: Annotated[str, Field(max_length=60000)],
                          number: Annotated[int, Field(ge=0)] = 0, review_id: Annotated[int, Field(ge=0)] = 0,
                          expected_head_sha: str = "",
                          event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT",
                          task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                          expected_fields: dict | None = None) -> CallToolResult:
            """Submit an exact-HEAD review summary after reading the complete relevant diff and discussions, or update its body. Missing gate evidence rejects creation; known failures can still be reviewed. Self-review must use COMMENT and is not independent approval. On update, expected_fields contains only the observed body value; this is a sequential precondition, not atomic CAS. Uncertain writes require original-review readback, never blind retry."""
            return _reply(lambda: application.github_review(action, repository, body, number, review_id, expected_head_sha, event, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_review_thread(action: Literal["resolve", "unresolve"], repository: str,
                                 number: Annotated[int, Field(ge=1)],
                                 thread_id: Annotated[str, Field(pattern=r"^PRRT_[A-Za-z0-9_-]{1,128}$")],
                                 expected_head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")],
                                 expected_base_sha: str = "",
                                 task_context: tasks.TaskContext | None = None,
                                 connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Resolve or unresolve one verified review thread on the specified PR and exact HEAD. Verify its membership, state and account before and after mutation; never replay uncertain results."""
            return _reply(lambda: application.github_review_thread(action, repository, number, thread_id, expected_head_sha, expected_base_sha, _task_context(task_context), connection_id=connection_id))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_merge(repository: str, number: Annotated[int, Field(ge=1)],
                         expected_head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")],
                         expected_base_sha: str = "", method: Literal["squash", "merge", "rebase"] = "squash",
                         commit_title: Annotated[str, Field(max_length=256)] = "",
                         commit_message: Annotated[str, Field(max_length=60000)] = "",
                         task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Merge only an explicitly authorized, current-HEAD reviewed PR with complete checks/discussions and clean native policy. No bypass, auto-merge or queue support. Bind both observed SHAs when available. Only merged_verified confirms the actual commit on the destination branch; pending/unknown results require readback, not resubmission."""
            return _reply(lambda: application.github_merge(repository, number, expected_head_sha, expected_base_sha, method, commit_title, commit_message, _task_context(task_context), connection_id=connection_id))

    return mcp


def serve(config: Settings, state: Path, *, managed: bool = False) -> None:
    state = private_directory(state)
    path = state / "mcp.sock"
    locks = private_directory(state / "locks")
    with lease(locks / "listener.lock"), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        if managed and os.path.lexists(path):
            identity = path.lstat()
            if (not stat.S_ISSOCK(identity.st_mode) or identity.st_uid != os.getuid()
                    or identity.st_mode & 0o077 or service.socket_alive(path)):
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
            app = create_server(config, state).streamable_http_app(
                stateless_http=True, json_response=True, max_request_body_size=1048576,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=["localhost", "localhost:*"],
                    allowed_origins=["http://localhost", "http://localhost:*"],
                ),
            )
            uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning")).run(sockets=[listener])
        finally:
            signal.signal(signal.SIGTERM, previous)
            if path.exists() and path.stat().st_ino == identity.st_ino:
                path.unlink()
