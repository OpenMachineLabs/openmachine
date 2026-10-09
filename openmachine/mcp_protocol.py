"""Shared MCP wire schemas, bounded replies and owned-request submission."""

from concurrent.futures import Future, ThreadPoolExecutor
import json
from typing import Annotated

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from openmachine import tasks
from openmachine.app import DEFAULT_TIMEOUT, MAX_TIMEOUT, MAX_WAIT, MAX_WAITERS, MAX_RESULT_BYTES, bound_output, MAX_CONTROL_SEQ, MAX_INPUT, MAX_TASK_WAIT
from openmachine.app import MAX_SOURCE, MAX_WORKSPACE_RESULT, MAX_TASK_RESULT_BYTES, bound_workspace_result
from openmachine.foundation.sanitize import Fault

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
