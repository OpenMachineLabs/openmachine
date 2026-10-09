"""MCP workspace, Skills, and change tool registration."""

import asyncio
from functools import partial
from typing import Annotated, Literal
from mcp.types import CallToolResult
from pydantic import Field
from openmachine import tasks
from openmachine.mcp.protocol import (READ, MAX_SOURCE, ExactEdit, PatchPrecondition,
                                      _reply, _submit_owned, _task_context)

def register_workspace_tools(application, guided_tool, machine_read, file_write):
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
