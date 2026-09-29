"""Explicit local tools and optional GitHub operations on a private MCP service."""

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import signal
import sys
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolRequest, CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
import uvicorn

from openmachine import __version__, execution, github, service, skills, tasks, workspace
from openmachine.config import Settings
from openmachine.files import MAX_SCAN_ENTRIES, MAX_TEXT_FILE, TEXT_PAGE
from openmachine.sanitize import Fault
from openmachine.state import lease, private_directory

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)
GITHUB_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
GITHUB_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


def _reply(operation) -> CallToolResult:
    try:
        payload = operation()
        failed = False
    except Fault as error:
        payload = {"error": {"code": error.code, "message": str(error)}}
        failed = True
    except OSError as error:
        payload = {"error": {"code": "source_unavailable", "message": type(error).__name__}}
        failed = True
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                          structuredContent=payload, isError=failed)


def create_server(config: Settings, state: Path) -> FastMCP:
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

    private_directory(state / "locks")

    def mutate(operation) -> CallToolResult:
        def locked():
            with lease(state / "locks/lifecycle.lock", shared=True):
                service.guard_writes(state)
                return operation()
        return _reply(locked)

    has_ssh = any(item.transport == "ssh" for item in config.machines.values())
    machine_read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=has_ssh)
    file_write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=has_ssh)

    @mcp.tool(annotations=READ, structured_output=False)
    def get_capabilities() -> CallToolResult:
        """Report implemented limits and configured targets, not live health or future features."""
        return _reply(lambda: {
            "version": __version__, "configuration_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
            "machines": {name: {**item.model_dump(), "probe_status": "not_probed"} for name, item in config.machines.items()},
            "workspaces": {name: item.model_dump() for name, item in config.workspaces.items()},
            "skills": ["openmachine"], "skill_roots": config.skills.roots, "task_continuity": True,
            "integrations": (["github"] if config.github is not None else []) + (["ssh"] if has_ssh else []),
            "github": None if config.github is None else {**config.github.model_dump(), "backend": "gh",
                                                           "probe_status": "not_probed", "read_and_merge": True},
            "transport": "private_unix_socket",
            "lifecycle": {"protocol": service.PROTOCOL, "state_contract": service.STATE_CONTRACT, "pid": os.getpid()},
            "limits": {"default_timeout_seconds": execution.DEFAULT_TIMEOUT, "max_timeout_seconds": execution.MAX_TIMEOUT, "max_read_bytes": execution.MAX_READ,
                       "log_bytes": execution.MAX_LOG, "log_retention": "rotating", "log_segments": 4, "log_segment_bytes": execution.logs.SEGMENT_BYTES,
                       "workspace_access": ["exclusive", "shared"], "max_resource_keys": 16,
                       "git_capture_bytes": workspace.MAX_CAPTURE, "search_entries": MAX_SCAN_ENTRIES,
                       "search_bytes": workspace.MAX_SCAN_BYTES, "search_matches": workspace.MAX_MATCHES,
                       "text_file_bytes": MAX_TEXT_FILE, "text_page_bytes": TEXT_PAGE,
                       "checkpoint_bytes": tasks.MAX_CHECKPOINT, "task_index_bytes": tasks.MAX_INDEX},
            "boundary": "Native account access, not a sandbox. No live ChatGPT/Tunnel health probe.",
        })

    @mcp.tool(annotations=machine_read, structured_output=False)
    def inspect_workspace(machine: str, workspace_name: str, action: Literal["files", "read", "search", "status", "diff"],
                          path: str = ".", offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=200)] = 100, query: str = "",
                          staged: bool = False, expected_snapshot: str = "") -> CallToolResult:
        """Read files, literal-search, or inspect Git status/diff. Echo snapshot between pages; heed skipped/truncated results."""
        return _reply(lambda: workspace.inspect(config, machine, workspace_name, action, path, offset, limit,
                                                 query=query, staged=staged, expected_snapshot=expected_snapshot))

    @mcp.tool(annotations=READ, structured_output=False)
    def inspect_skills(action: Literal["list", "read", "files"] = "list", skill: str = "", path: str = "",
                       query: Annotated[str, Field(max_length=200)] = "", offset: Annotated[int, Field(ge=0)] = 0,
                       limit: Annotated[int, Field(ge=1, le=100)] = 100, expected_snapshot: str = "") -> CallToolResult:
        """Read the bundled Skill or explicit user sources. Select exact discovered identities, not names. Sources are a Skill directory or a collection of immediate Skill directories, including whole-directory links. Resource reads never execute scripts or escape the selected Skill. Follow pages/snapshots and warnings; empty skill with action=read selects the bundled operating Skill."""
        return _reply(lambda: skills.inspect(config, action, skill, path, query, offset, limit, expected_snapshot))

    @mcp.tool(annotations=machine_read, structured_output=False)
    def read_workspace_file(machine: str, workspace_name: str, path: str,
                            offset: Annotated[int, Field(ge=0, le=131072)] = 0,
                            expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "") -> CallToolResult:
        """Read one configured-workspace text file, including untracked source, with whole-file SHA256, Git context and explicit absence. Follow next_offset and echo expected_sha256 until has_more=false; never overwrite a file from partial/redacted text. Bounded UTF-8 pages; no writes or submitted-content execution. Configured SSH workspaces are read on their pinned remote target."""
        return _reply(lambda: workspace.read_text_file(config, machine, workspace_name, path, offset, expected_sha256))

    @mcp.tool(annotations=file_write, structured_output=False)
    def write_workspace_file(machine: str, workspace_name: str, path: str,
                             content: Annotated[str, Field(max_length=131072)],
                             expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                             expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")]) -> CallToolResult:
        """Create or replace exactly one authorized UTF-8 file in a configured workspace with execution enabled. Requires observed Git HEAD and whole-file SHA256; empty HEAD explicitly means no HEAD, empty SHA256 means absent. Shares native execution locks and verifies atomic publication. Hash checking and rename are separate operations, not atomic CAS against external editors; pause other writers to this file. Rejects traversal, credential paths, links and special files. No shell, submitted-content execution, deletion, parent creation or permission-change interface. Configured SSH writes use the pinned target and its own preconditions and locks. Read back any unknown outcome; platform approval remains independent."""
        return mutate(lambda: workspace.write_text_file(config, state, machine, workspace_name, path, content,
                                                        expected_head_sha, expected_sha256))

    @mcp.tool(annotations=READ, structured_output=False)
    def find_tasks(query: Annotated[str, Field(max_length=200)] = "", anchors: Annotated[list[str], Field(max_length=16)] = [],
                   task_id: str = "", offset: Annotated[int, Field(ge=0)] = 0,
                   limit: Annotated[int, Field(ge=1, le=50)] = 20,
                   expected_generation: Annotated[int, Field(ge=-1)] = -1) -> CallToolResult:
        """Find private checkpoints by exact task/anchors or title/ID keywords. Anchor filters intersect; descriptive results are candidates, never continuation authority. Follow pagination/generation. Reads do not initialize state or start work; missing/corrupt history is explicit."""
        return _reply(lambda: tasks.find(state, query, anchors, task_id, offset, limit, expected_generation))

    @mcp.tool(annotations=READ, structured_output=False)
    def read_task(task_id: str) -> CallToolResult:
        """Read the original bounded checkpoint and revision, verify its index hash, then recheck current authority, Git and execution receipts before continuation. This does not resume an agent or execute work."""
        return _reply(lambda: tasks.read(state, task_id))

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=False)
    def save_task(task_id: str, expected_revision: Annotated[int, Field(ge=0)],
                  title: Annotated[str, Field(min_length=1, max_length=200)],
                  body: Annotated[str, Field(min_length=1, max_length=32768)],
                  anchors: Annotated[list[str], Field(max_length=16)] = []) -> CallToolResult:
        """Save one private material checkpoint and lookup metadata. Expected revision zero creates; updates compare the observed revision under a store lock. Identical last-write retries are no-ops; stale/corrupt state is not overwritten. Lookup anchors are additive; omitting them preserves prior references. Keep goals, authority references, original executions, evidence and next action, not credentials, transcripts or hidden reasoning. No code execution, external publication or automatic resume. Torn two-file saves are reported, not silently repaired."""
        return mutate(lambda: tasks.save(state, task_id, expected_revision, title, body, anchors))

    @mcp.tool(annotations=WRITE, structured_output=False)
    def start_execution(machine: str, execution_id: str, cwd: str,
                        argv: Annotated[list[str], Field(min_length=1, max_length=100)],
                        timeout_seconds: Annotated[int, Field(ge=1, le=execution.MAX_TIMEOUT)] = execution.DEFAULT_TIMEOUT,
                        expected_head_sha: str = "", workspace_access: Literal["exclusive", "shared"] = "exclusive",
                        resource_keys: Annotated[list[str], Field(max_length=16)] = [], task_id: str = "") -> CallToolResult:
        """Start one explicitly authorized native command. Save its ID before launch and query that ID after uncertainty. Optional task_id references an existing checkpoint; it never writes task progress or supplies authorization."""
        return mutate(lambda: execution.start(config, state, machine, execution_id, cwd, argv,
                                              timeout_seconds, expected_head_sha, workspace_access, resource_keys, task_id))

    @mcp.tool(annotations=machine_read, structured_output=False)
    def read_execution(machine: str, execution_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                       max_bytes: Annotated[int, Field(ge=1, le=65536)] = 65536) -> CallToolResult:
        """Read the original local or remote receipt and bounded output. finishing means remote scope cleanup is pending. Unreachable targets or missing worker evidence are unknown, never success or permission to retry."""
        return _reply(lambda: execution.read(config, state, machine, execution_id, offset, max_bytes))

    @mcp.tool(annotations=machine_read, structured_output=False)
    def list_executions(machine: str, cursor: str = "",
                        limit: Annotated[int, Field(ge=1, le=100)] = 20) -> CallToolResult:
        """Find original execution IDs in stable ID order. Follow next_cursor and unreadable records; this is not task recovery."""
        return _reply(lambda: execution.list_records(config, state, machine, cursor, limit))

    @mcp.tool(annotations=WRITE, structured_output=False)
    def cancel_execution(machine: str, execution_id: str) -> CallToolResult:
        """Request cancellation of this owned execution only; read its terminal receipt. Does not undo previous side effects."""
        return mutate(lambda: execution.cancel(config, state, machine, execution_id))

    if config.github is not None:
        @mcp.tool(annotations=GITHUB_READ, structured_output=False)
        def github_read(action: github.ReadAction, repository: str = "", number: Annotated[int, Field(ge=0)] = 0,
                        ref: str = "", path: str = "", page: Annotated[int, Field(ge=1, le=1000)] = 1,
                        limit: Annotated[int, Field(ge=1, le=100)] = 30, cursor: Annotated[str, Field(max_length=1024)] = "",
                        expected_head_sha: str = "", expected_base_sha: str = "",
                        offset: Annotated[int, Field(ge=0, le=1048576)] = 0) -> CallToolResult:
            """Read scoped GitHub identity, repositories, Issues and version-bound PR evidence. Read pull_request before candidate pages; echo both HEAD/base on continuation. Follow next_page/next_cursor and evidence gaps. checks uses an exact commit ref. file uses a fixed commit and 16 KiB UTF-8 byte pages; no symlink/credential reads. API patches are not proof of complete diffs. status probes the existing gh identity without credentials or login changes. merge_context observes gates and never merges. No arbitrary endpoint or mutation dispatch."""
            return _reply(lambda: github.read(config, action, repository, number, ref, path, page, limit, cursor,
                                              expected_head_sha, expected_base_sha, offset))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_issue(action: Literal["create", "update"], repository: str,
                         title: Annotated[str | None, Field(max_length=256)] = None,
                         body: Annotated[str | None, Field(max_length=60000)] = None,
                         number: Annotated[int, Field(ge=0)] = 0,
                         state: Literal["open", "closed"] | None = None, expected_updated_at: str = "") -> CallToolResult:
            """Create or update an authorized Issue with field/identity readback and preserved provenance. state is update-only. Supply observed updated_at to detect stale edits; this is not atomic CAS. Creation is not idempotent: reconcile an uncertain destination before retrying."""
            return mutate(lambda: github.issue(config, action, repository, title, body, number, state, expected_updated_at))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_pull_request(action: Literal["create", "update"], repository: str,
                                title: Annotated[str | None, Field(max_length=256)] = None,
                                body: Annotated[str | None, Field(max_length=60000)] = None,
                                number: Annotated[int, Field(ge=0)] = 0,
                                head: str = "", base: str = "", draft: bool = False,
                                state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                                expected_head_sha: str = "") -> CallToolResult:
            """Create/update a scoped PR using the same gh connection. Prefer expected_head_sha and observed updated_at. Pinned creation uses an existing same-repository branch. Readback verifies supplied fields and identity; uncertain creation must not be repeated. Local commits and push stay with native execution."""
            return mutate(lambda: github.pull_request(config, action, repository, title, body, number, head, base, draft,
                                                      state, expected_updated_at, expected_head_sha))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_comment(action: Literal["create", "update"], repository: str,
                           body: Annotated[str, Field(max_length=60000)], number: Annotated[int, Field(ge=0)] = 0,
                           comment_id: Annotated[int, Field(ge=0)] = 0, expected_updated_at: str = "") -> CallToolResult:
            """Create/update a top-level Issue/PR comment and verify its fields. Inline comments are outside this writer. Preserve the original comment identity after uncertainty; do not post it again. updated_at detects an observed edit conflict without promising atomic CAS."""
            return mutate(lambda: github.comment(config, action, repository, body, number, comment_id, expected_updated_at))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_review(action: Literal["create", "update"], repository: str,
                          body: Annotated[str, Field(max_length=60000)],
                          number: Annotated[int, Field(ge=0)] = 0, review_id: Annotated[int, Field(ge=0)] = 0,
                          expected_head_sha: str = "",
                          event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT") -> CallToolResult:
            """Submit an exact-HEAD review summary after reading the complete relevant diff and discussions, or update its body. Missing gate evidence rejects creation; known failures can still be reviewed. Self-review must use COMMENT and is not independent approval. Uncertain writes require original-review readback, never blind retry."""
            return mutate(lambda: github.review(config, action, repository, body, number, review_id, expected_head_sha, event))

        @mcp.tool(annotations=GITHUB_WRITE, structured_output=False)
        def github_merge(repository: str, number: Annotated[int, Field(ge=1)],
                         expected_head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")],
                         expected_base_sha: str = "", method: Literal["squash", "merge", "rebase"] = "squash",
                         commit_title: Annotated[str, Field(max_length=256)] = "",
                         commit_message: Annotated[str, Field(max_length=60000)] = "") -> CallToolResult:
            """Merge only an explicitly authorized, current-HEAD reviewed PR with complete checks/discussions and clean native policy. No bypass, auto-merge or queue support. Bind both observed SHAs when available. Only merged_verified confirms the actual commit on the destination branch; pending/unknown results require readback, not resubmission."""
            return mutate(lambda: github.merge(config, repository, number, expected_head_sha, expected_base_sha,
                                               method, commit_title, commit_message))

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
