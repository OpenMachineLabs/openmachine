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

from openmachine import __version__, service, tasks
from openmachine.app import DEFAULT_TIMEOUT, MAX_TIMEOUT, MAX_WAIT, MAX_WAITERS, MAX_RESULT_BYTES, bound_output, MAX_CONTROL_SEQ, MAX_INPUT, MAX_TASK_WAIT
from openmachine.app import MAX_SOURCE, MAX_WORKSPACE_RESULT, bound_workspace_result
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


class RecoveryChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["enable", "disable"]


GitHubNames = Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=100)]


def _task_context(value: tasks.TaskContext | None) -> dict | None:
    return value.model_dump() if value is not None else None


def _result(payload: dict, failed: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                          structured_content=payload, is_error=failed)


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
        guarded = {"read_task", "wait_task", "start_codex_task", "read_codex_task", "configure_chat_recovery", "create_task", "control_task", "start_execution", "cancel_execution", "control_execution",
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
        if (isinstance(response, dict) and not response.get("isError")
                and isinstance(response.get("structuredContent"), dict)
                and application.chat_recovery is not None):
            try:
                await asyncio.to_thread(application.chat_recovery.external_activity,
                                        params["name"], arguments or {}, response["structuredContent"])
            except (Fault, OSError) as error:
                # A correlated read is not acknowledged until its evidence is
                # durable. Ordinary business effects retain their existing hook.
                code = error.code if isinstance(error, Fault) else "chat_recovery_store_unavailable"
                response = _result({"error": {"code": code,
                    "message": "Receiver evidence was not acknowledged; retain the original preparation."}}, True).model_dump(
                        mode="json", by_alias=True, exclude_none=True, exclude={"result_type"})
                if ctx.protocol_version == "2026-07-28":
                    response.update(resultType="complete", _meta={
                        "io.modelcontextprotocol/serverInfo": {"name": mcp.name, "version": mcp.version}})
        return response

    mcp = MCPServer(
        "Open Machine", instructions="Read inspect_skills(action='read') before operating this runtime.",
        version=__version__, middleware=[safe_tool_call], lifespan=lifespan,
    )

    private_directory(state)
    private_directory(state / "locks")

    has_ssh = any(item.transport == "ssh" for item in config.machines.values())
    machine_read = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=has_ssh)
    file_write = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=has_ssh)

    @mcp.tool(annotations=READ, structured_output=False)
    def get_capabilities() -> CallToolResult:
        """Report implemented limits and configured targets, not live health or future features."""
        return _reply(lambda: application.get_capabilities())

    @mcp.tool(annotations=READ, structured_output=False)
    def list_integrations(provider: Annotated[str, Field(max_length=64)] = "",
                          connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
        """List installed connection methods and retained accounts without network access or state writes. Saved connection status is not live provider health."""
        return _reply(lambda: application.list_integrations(provider, connection_id))

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def connect_integration(request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                                  provider: Annotated[str, Field(max_length=64)] = "github",
                                  method: Annotated[str, Field(max_length=64)] = "device_code",
                                  connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
        """Start native GitHub Device Flow owned by this runtime. Return only the public verification URL/code; credentials never enter tool results. Preserve request_id after uncertainty."""
        operation = lambda: _reply(lambda: application.connect_integration(request_id, provider, method, connection_id))
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @mcp.tool(annotations=READ, structured_output=False)
    def read_integration_login(login_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> CallToolResult:
        """Read the original login result without restarting it. A runtime restart interrupts unfinished Device Flow but preserves established connections."""
        return _reply(lambda: application.read_integration_login(login_id))

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=False)
    def cancel_integration_login(login_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> CallToolResult:
        """Cancel one unfinished native login. This does not disconnect an established account or undo provider-side consent."""
        return _reply(lambda: application.cancel_integration_login(login_id))

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=False)
    async def disconnect_integration(connection_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                                     expected_revision: Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]) -> CallToolResult:
        """Disconnect only this saved OM connection using its observed authorization revision. Native credentials are removed from their pinned local store; remote GitHub grants are not claimed revoked."""
        operation = lambda: _reply(lambda: application.disconnect_integration(connection_id, expected_revision))
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @mcp.tool(annotations=machine_read, structured_output=False)
    def inspect_workspace(machine: str, workspace_name: str, action: Literal["files", "read", "search", "status", "diff"],
                          path: str = ".", offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=200)] = 100, query: str = "",
                          staged: bool = False, expected_snapshot: str = "",
                          path_glob: Annotated[str, Field(max_length=1024)] = "") -> CallToolResult:
        """Read files, literal-search, or inspect Git status/diff. Optional path_glob selects workspace-relative, case-sensitive file discovery/search (* never crosses /; no **). Git discovery includes tracked and ordinary untracked source; omitted filter preserves traversal. Echo snapshot between pages; heed skipped/truncated results."""
        return _reply(lambda: application.inspect_workspace(machine, workspace_name, action, path, offset, limit, query, staged, expected_snapshot, path_glob), workspace_output=True)

    @mcp.tool(annotations=READ, structured_output=False)
    def inspect_skills(action: Literal["list", "read", "files"] = "list", skill: str = "", path: str = "",
                       query: Annotated[str, Field(max_length=200)] = "", offset: Annotated[int, Field(ge=0)] = 0,
                       limit: Annotated[int, Field(ge=1, le=100)] = 100, expected_snapshot: str = "") -> CallToolResult:
        """Read the bundled Skill or explicit user sources. Select exact discovered identities, not names. Sources are a Skill directory or a collection of immediate Skill directories, including whole-directory links. Resource reads never execute scripts or escape the selected Skill. Follow pages/snapshots and warnings; empty skill with action=read selects the bundled operating Skill."""
        return _reply(lambda: application.inspect_skills(action, skill, path, query, offset, limit, expected_snapshot))

    @mcp.tool(annotations=machine_read, structured_output=False)
    def read_workspace_file(machine: str, workspace_name: str, path: str,
                            offset: Annotated[int, Field(ge=0, le=MAX_SOURCE)] = 0,
                            expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                            start_line: Annotated[int | None, Field(strict=True, ge=1)] = None,
                            line_count: Annotated[int, Field(strict=True, ge=1, le=500)] = 200) -> CallToolResult:
        """Read up to 2 MiB of configured UTF-8 source with whole-file SHA256/context and 32 KiB pages. Optional 1-based start_line requires offset=0; line_count is 1..500. range_complete describes available requested bytes, while has_more describes remaining source. A long line continues in byte mode at next_offset with the same SHA; next_line exists only at a known boundary. Numbers remain metadata, not source text. Redaction uses full bounded source. Never reconstruct full files from partial/redacted pages. SSH reads occur only on the pinned target."""
        return _reply(lambda: application.read_workspace_file(machine, workspace_name, path, offset, expected_sha256, start_line, line_count), workspace_output=True)

    @mcp.tool(annotations=file_write, structured_output=False)
    async def write_workspace_file(machine: str, workspace_name: str, path: str,
                             content: Annotated[str, Field(max_length=131072)],
                             expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                             expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")],
                             change_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                             expected_workspace_identity: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                             task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Publish one bounded whole file. A change_id requires observed workspace identity and retains the original result; no-ID calls retain legacy behavior without durable recovery. Empty source hash means absence. Uses the independent effective edit grant and the original workspace lease; no commands, parents or implicit replay. Unknown effects require original-ID readback. Native checks are not a kernel CAS or sandbox."""
        operation = partial(_reply, lambda: application.write_workspace_file(machine, workspace_name, path, content,
            expected_head_sha, expected_sha256, change_id, expected_workspace_identity, _task_context(task_context)), workspace_output=True,
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
                            expected_plan_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                            task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Replace exact non-overlapping fragments located in the same original UTF-8 file. Preview requires empty ID and has no writes; apply requires a caller-generated ID. Echo observed whole-file hash, HEAD and workspace identity; optionally bind preview plan SHA. Same ID/request observes, never replays; ambiguous matches fail. Sources/results support 2 MiB; combined replacement strings remain 128 KiB. File changes are not test results."""
        operation = partial(_reply, lambda: application.edit_workspace_file(machine, workspace_name, path,
            [edit.model_dump() for edit in edits], expected_workspace_identity, expected_head_sha, expected_sha256,
            change_id, dry_run, expected_plan_sha256, _task_context(task_context)), workspace_output=True, workspace_mutation=True, change_id=change_id)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @mcp.tool(annotations=file_write, structured_output=False)
    async def apply_workspace_patch(machine: str, workspace_name: str,
                            patch: Annotated[str, Field(max_length=131072)],
                            expected_workspace_identity: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
                            expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                            preconditions: Annotated[list[PatchPrecondition], Field(min_length=1, max_length=32)],
                            create_parents: Annotated[bool, Field(strict=True)] = False,
                            change_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                            dry_run: Annotated[bool, Field(strict=True)] = False,
                            expected_plan_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                            task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Apply one strict om_patch_v1 add/update/delete/move plan on one workspace. Exact preconditions cover both move endpoints; empty hash means absence. All files are prepared before effects, then published in declared order, not as an atomic batch. Preview has no state or filesystem writes and requires an empty ID. Apply requires a caller-generated ID; retained identical requests never replay. Explicit parent creation is bounded. Partial and unknown outcomes retain the original effect evidence; do not retry or roll back automatically. File changes are not test results."""
        operation = partial(_reply, lambda: application.apply_workspace_patch(machine, workspace_name, patch,
            expected_workspace_identity, expected_head_sha, [item.model_dump() for item in preconditions],
            create_parents, change_id, dry_run, expected_plan_sha256, _task_context(task_context)), workspace_output=True,
            workspace_mutation=True, change_id=change_id)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @mcp.tool(annotations=machine_read, structured_output=False)
    def read_workspace_change(machine: str, change_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                              observe_current: Annotated[bool, Field(strict=True)] = False) -> CallToolResult:
        """Read original target-owned change evidence without applying, repairing or initializing state. Optional current hashes are separate observations, not historical proof. Missing history is not non-admission; do not retry blindly. Original root must remain authorized. No write grant is required."""
        return _reply(lambda: application.read_workspace_change(machine, change_id, observe_current),
                      workspace_output=True, change_id=change_id)

    @mcp.tool(annotations=WRITE, structured_output=False)
    def create_task(task_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                    update_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")],
                    title: Annotated[str, Field(min_length=1, max_length=200)],
                    anchors: Annotated[list[str], Field(max_length=16)], origin: tasks.Origin, contract: tasks.Contract,
                    stages: Annotated[list[tasks.StageDefinition], Field(min_length=1, max_length=16)]) -> CallToolResult:
        """Create a bounded structured task for authorized multi-stage work. New tasks default to recovery disabled and create no Chat receiver. Set recovery_policy=task_chat only when the user explicitly requests recovery for that task; an explicit later enable may enroll the same task under its original authority. No originating Chat URL is required. Read any returned recovery state and blocker; committed creation is not receiver readiness. Preserve necessary instructions and readable inputs, not just source hashes. Stage source_sha256 hashes the selected full source objects sorted by locator using UTF-8 canonical JSON and a final newline; see the operating Skill. A committed record or stale index is never permission to recreate it. Explicit claim and current task_context remain required; recovery grants no business authority. One-step reads need no task. Never convert legacy history or supply runtime facts."""
        return _reply(lambda: application.create_task(task_id, update_id, title, anchors, origin.model_dump(),
                      contract.model_dump(), [stage.model_dump() for stage in stages]))

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def control_task(task_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                           expected_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                           expected_plan_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                           expected_epoch: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)],
                           update_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")],
                           transition: tasks.Transition) -> CallToolResult:
        """Explicitly claim/renew/release, pause/resume/cancel, revise a plan, bind an exact historical effect scope, accept verified stages or finish the original task. Control writes require current task/plan/epoch; retain update_id after uncertainty. Renew names the original claim_id and preserves its principal and epoch. Expired authority needs an explicit revised contract with fresh authority evidence. bind_effect_scope accepts the original normalized GitHub arguments, proves their retained fingerprint and appends scope evidence without rewriting or resolving the original unknown operation. Claims provide cooperative ownership, not ChatGPT identity or new permission. Pause blocks new admission, not admitted effects; cancellation retains unknown and partial outcomes. Finish requires action, outcome, acceptance and limitations; acceptance contains {stage_id, operation_id} references to accepted original evidence. Reads never claim. Pure observations may advance revision without invalidating business admission after control_revision; controls still require exact revision. Inspect returned state and recovery projection."""
        operation = partial(_reply, lambda: application.control_task(task_id, expected_revision,
            expected_plan_revision, expected_epoch, update_id, transition.model_dump()))
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @mcp.tool(annotations=WRITE, structured_output=False)
    def configure_chat_recovery(task_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                                update_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")],
                                expected_revision: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)],
                                change: RecoveryChange) -> CallToolResult:
        """Explicitly enable or disable task-owned recovery under the original task authority. Runtime owns the single destination and controller; callers cannot supply a URL or another account. Enable may schedule preparation but never claims business work, extends task authority, resets budgets or retries sent/uncertain messages. Disable suppresses future setup/delivery and retains original evidence; it does not cancel admitted commands. Preserve update_id and observed recovery revision after uncertainty. Historical unbound tasks need explicit guarded plan enrollment, not a legacy URL-binding fallback."""
        return _reply(lambda: application.configure_chat_recovery(
            task_id, update_id, expected_revision, change.model_dump()))

    @mcp.tool(annotations=READ, structured_output=False)
    def find_tasks(query: Annotated[str, Field(max_length=200)] = "", anchors: Annotated[list[str], Field(max_length=16)] = [],
                   task_id: str = "", offset: Annotated[int, Field(ge=0)] = 0,
                   limit: Annotated[int, Field(ge=1, le=50)] = 20,
                   expected_generation: Annotated[int, Field(ge=-1)] = -1) -> CallToolResult:
        """Find private checkpoints by exact task/anchors or title/ID keywords. Anchor filters intersect; descriptive results are candidates, never continuation authority. Follow pagination/generation. Reads do not initialize state or start work; missing/corrupt history is explicit. resume_authorized=false means this lookup grants no authority, not that current user authorization is denied."""
        return _reply(lambda: application.find_tasks(query, anchors, task_id, offset, limit, expected_generation))

    @mcp.tool(annotations=READ, structured_output=False)
    def read_task(task_id: str, preparation_id: Annotated[str | None,
                  Field(pattern=r"^[0-9a-f]{32}$")] = None) -> CallToolResult:
        """Read original task history without initializing, repairing, claiming or executing. Canonical records validate independently of derived index metadata and report current/stale/incomplete lookup status; legacy Markdown retains its original index-hash contract. An optional original preparation_id correlates a read-only receiver check; only the successful external MCP call records that evidence. It does not claim, enable recovery, prove a Chat identity or advance work. Omitted means a passive ordinary read. Reading grants no authority and never reactivates terminal history."""
        return _reply(lambda: application.read_task(task_id, preparation_id))

    @mcp.tool(annotations=READ, structured_output=False)
    async def wait_task(task_id: Annotated[str, Field(min_length=1, max_length=64,
                        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                        expected_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                        wait_seconds: Annotated[float, Field(strict=True, ge=0, le=MAX_TASK_WAIT,
                        allow_inf_nan=False)] = MAX_TASK_WAIT) -> CallToolResult:
        """Wait once for bounded structured-task progress. Attention or wait expiry sets foreground.stop_foreground and supplies a safe resume prompt; callers must end the current Chat turn. This read never claims, retries, cancels, or extends original work."""
        if wait_seconds and not waiting.acquire(blocking=False):
            return _result({"error": {"code": "reader_busy",
                "message": "At most 16 bounded waits are admitted; immediate task reads and cancellation remain available."}}, True)
        cancelled = threading.Event()
        operation = partial(_reply, lambda: application.wait_task(
            task_id, expected_revision, wait_seconds, cancelled=cancelled))
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

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=False)
    def save_task(task_id: str, expected_revision: Annotated[int, Field(ge=0)],
                  title: Annotated[str, Field(min_length=1, max_length=200)],
                  body: Annotated[str, Field(min_length=1, max_length=32768)],
                  anchors: Annotated[list[str], Field(max_length=16)] = []) -> CallToolResult:
        """Save a legacy private checkpoint and lookup metadata. Continue existing legacy work here; use create_task/control_task for new structured multi-stage work, without a parallel checkpoint or conversion. Expected revision zero creates; updates compare the observed revision under a store lock. Identical last-write retries are no-ops; stale/corrupt state is not overwritten. Lookup anchors are additive; omitting them preserves prior references. Keep goals, authority references, original executions, evidence and next action, not credentials, transcripts or hidden reasoning. No code execution, external publication or automatic resume. Torn two-file saves are reported, not silently repaired."""
        return _reply(lambda: application.save_task(task_id, expected_revision, title, body, anchors))

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def start_codex_task(machine: str, execution_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                               cwd: str, goal: Annotated[str, Field(min_length=1, max_length=6000)],
                               start_url: Annotated[str, Field(min_length=1, max_length=2048)],
                               allowed_origins: Annotated[list[Annotated[str, Field(max_length=2048)]], Field(min_length=1, max_length=8)],
                               mode: Literal["browser"] = "browser", browser: Literal["auto", "iab", "chrome", "edge"] = "auto",
                               timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=1800)] = 600,
                               expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")] = "",
                               task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Delegate one authorized browser/UI goal to installed native Codex. Chat remains controller. Use exact navigation origins and a task-owned tab; no secrets or unrelated context. Local execution grant, workspace scope and existing task_context/start_execution admission apply. CLI installation alone is not browser availability. Preserve the original execution_id; read before retrying uncertainty. Cancel with cancel_execution. No computer mode, model fallback or automatic Chat wake; native browser scope is not a sandbox."""
        operation = partial(_reply, lambda: application.start_codex_task(machine, execution_id, cwd, goal,
            start_url, allowed_origins, mode, browser, timeout_seconds, expected_head_sha, _task_context(task_context)), execution_output=True)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @mcp.tool(annotations=READ, structured_output=False)
    def read_codex_task(machine: str, execution_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> CallToolResult:
        """Read the original Codex browser report and native final-page evidence without starting work. Process success is not semantic completion; Chat must inspect this report before stage acceptance. Missing evidence stays unknown, and cancellation/timeout is never promoted to completion."""
        return _reply(lambda: application.read_codex_task(machine, execution_id))

    @mcp.tool(annotations=WRITE, structured_output=False)
    def start_execution(machine: str, execution_id: str, cwd: str,
                        argv: Annotated[list[str], Field(min_length=1, max_length=100)],
                        timeout_seconds: Annotated[int, Field(ge=1, le=MAX_TIMEOUT)] = DEFAULT_TIMEOUT,
                        expected_head_sha: str = "", workspace_access: Literal["exclusive", "shared"] = "exclusive",
                        resource_keys: Annotated[list[str], Field(max_length=16)] = [], task_id: str = "",
                        termination_grace_seconds: Annotated[float | None, Field(strict=True, ge=0, le=30, allow_inf_nan=False)] = None,
                        tty: Annotated[bool, Field(strict=True)] = False,
                        terminal_size: TerminalSize | None = None,
                        task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Start one explicitly authorized native command. Save its ID before launch and query that ID after uncertainty. Optional task_id is a legacy checkpoint association, not permission. Structured work requires current task_context; the original execution_id must equal operation_id. Admission precedes dispatch; unknown outcomes never permit a new ID."""
        return _reply(lambda: application.start_execution(machine, execution_id, cwd, argv, timeout_seconds, expected_head_sha, workspace_access, resource_keys, task_id, termination_grace_seconds, tty, terminal_size.model_dump() if terminal_size is not None else None, _task_context(task_context)), execution_output=True)

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
    async def cancel_execution(machine: str, execution_id: str, task_context: tasks.TaskContext | None = None) -> CallToolResult:
        """Request cancellation of this owned execution only; read its terminal receipt. Does not undo previous side effects."""
        operation = partial(_reply, lambda: application.cancel_execution(machine, execution_id, _task_context(task_context)), execution_output=True)
        return await asyncio.wrap_future(_submit_owned(operation))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=has_ssh), structured_output=False)
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
        @mcp.tool(annotations=GITHUB_READ, structured_output=False)
        def github_read(action: github.ReadAction, repository: str = "", number: Annotated[int, Field(ge=0)] = 0,
                        ref: str = "", path: str = "", page: Annotated[int, Field(ge=1, le=1000)] = 1,
                        limit: Annotated[int, Field(ge=1, le=100)] = 30, cursor: Annotated[str, Field(max_length=1024)] = "",
                        expected_head_sha: str = "", expected_base_sha: str = "",
                        offset: Annotated[int, Field(ge=0, le=1048576)] = 0, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Read scoped GitHub identity, repositories, Issues and version-bound PR evidence. Read pull_request before candidate pages; echo both HEAD/base on continuation. Follow next_page/next_cursor and evidence gaps. checks uses an exact commit ref. file uses a fixed commit and 16 KiB UTF-8 byte pages; no symlink/credential reads. API patches are not proof of complete diffs. status probes the selected native GitHub identity without credentials or login changes. merge_context observes gates and never merges. No arbitrary endpoint or mutation dispatch."""
            return _reply(lambda: application.github_read(action, repository, number, ref, path, page, limit, cursor, expected_head_sha, expected_base_sha, offset, connection_id=connection_id))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_issue(action: Literal["create", "update"], repository: str,
                         title: Annotated[str | None, Field(max_length=256)] = None,
                         body: Annotated[str | None, Field(max_length=60000)] = None,
                         number: Annotated[int, Field(ge=0)] = 0,
                         state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                         labels: GitHubNames | None = None, assignees: GitHubNames | None = None,
                         task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                         expected_fields: dict | None = None) -> CallToolResult:
            """Create or update an authorized Issue with field/identity readback and preserved provenance. labels/assignees replace the complete supplied set; omit them to preserve current metadata and pass [] to clear. state is update-only. On update, expected_fields must contain the observed values of every replaced field and no others; named metadata uses complete sets. It avoids conflicts from unrelated object activity. Supplied expected_updated_at remains an additional whole-object guard. Guards are sequential checks, not atomic CAS. Creation is not idempotent: reconcile an uncertain destination before retrying."""
            return _reply(lambda: application.github_issue(action, repository, title, body, number, state, expected_updated_at, labels, assignees, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
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
            """Create/update a scoped PR, or mark an observed Draft PR ready for review, using the selected native GitHub connection. action='ready_for_review' accepts only number, exact expected_head_sha, optional draft/updated_at guards, and connection identity; it uses GitHub's fixed Ready-for-review mutation and verifies the same PR, HEAD and actor with an open, non-Draft readback before success. GitHub provides no conditional HEAD input: concurrent pushes can precede the transition, so this does not atomically pin or approve a commit; changed readback remains unverified even if the transition occurred. On update, labels/assignees replace the complete supplied set through GitHub's shared Issue metadata endpoint; omit them to preserve metadata and pass [] to clear. Keep metadata updates separate from title/body/state so one provider endpoint owns each effect. expected_fields must contain the observed values of every replaced field and no others; Ready for review uses only draft. Supplied expected_updated_at remains an additional whole-object guard. These checks are not atomic CAS. PR creation accepts neither metadata nor expected_fields. Pinned creation uses an existing same-repository branch. Uncertain writes require original-destination readback, never blind retry. Local commits and push stay with native execution."""
            return _reply(lambda: application.github_pull_request(action, repository, title, body, number, head, base, draft, state, expected_updated_at, expected_head_sha, labels, assignees, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_comment(action: Literal["create", "update"], repository: str,
                           body: Annotated[str, Field(max_length=60000)], number: Annotated[int, Field(ge=0)] = 0,
                           comment_id: Annotated[int, Field(ge=0)] = 0, expected_updated_at: str = "",
                           task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                           expected_fields: dict | None = None) -> CallToolResult:
            """Create/update a top-level Issue/PR comment and verify its fields. Inline comments are outside this writer. Preserve the original comment identity after uncertainty; do not post it again. On update, expected_fields contains only the observed body value. Supplied expected_updated_at remains an additional whole-object guard. These detect observed edit conflicts without promising atomic CAS."""
            return _reply(lambda: application.github_comment(action, repository, body, number, comment_id, expected_updated_at, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_review(action: Literal["create", "update"], repository: str,
                          body: Annotated[str, Field(max_length=60000)],
                          number: Annotated[int, Field(ge=0)] = 0, review_id: Annotated[int, Field(ge=0)] = 0,
                          expected_head_sha: str = "",
                          event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT",
                          task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                          expected_fields: dict | None = None) -> CallToolResult:
            """Submit an exact-HEAD review summary after reading the complete relevant diff and discussions, or update its body. Missing gate evidence rejects creation; known failures can still be reviewed. Self-review must use COMMENT and is not independent approval. On update, expected_fields contains only the observed body value; this is a sequential precondition, not atomic CAS. Uncertain writes require original-review readback, never blind retry."""
            return _reply(lambda: application.github_review(action, repository, body, number, review_id, expected_head_sha, event, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_review_thread(action: Literal["resolve", "unresolve"], repository: str,
                                 number: Annotated[int, Field(ge=1)],
                                 thread_id: Annotated[str, Field(pattern=r"^PRRT_[A-Za-z0-9_-]{1,128}$")],
                                 expected_head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")],
                                 expected_base_sha: str = "",
                                 task_context: tasks.TaskContext | None = None,
                                 connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Resolve or unresolve one verified review thread on the specified PR and exact HEAD. Verify its membership, state and account before and after mutation; never replay uncertain results."""
            return _reply(lambda: application.github_review_thread(action, repository, number, thread_id, expected_head_sha, expected_base_sha, _task_context(task_context), connection_id=connection_id))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
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
