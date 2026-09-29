"""Explicit local tools and optional GitHub operations on a private MCP service."""

import hashlib
import json
import os
from pathlib import Path
import socket
import signal
import sys
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolRequest, CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
import uvicorn

from openmachine import __version__, execution, github, skills, tasks, workspace
from openmachine.config import Settings
from openmachine.sanitize import Fault

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)


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

    @mcp.tool(annotations=READ, structured_output=False)
    def get_capabilities() -> CallToolResult:
        """Report implemented limits and configured targets, not live health or future features."""
        return _reply(lambda: {
            "version": __version__, "configuration_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
            "machines": {name: {**item.model_dump(), "probe_status": "not_probed"} for name, item in config.machines.items()},
            "workspaces": {name: item.model_dump() for name, item in config.workspaces.items()},
            "skills": ["openmachine"], "skill_roots": config.skills.roots, "task_continuity": True,
            "integrations": ["github"] if config.github is not None else [],
            "github": None if config.github is None else {"repositories": config.github.repositories,
                                                           "attribution": config.github.attribution},
            "transport": "private_unix_socket",
            "limits": {"default_timeout_seconds": execution.DEFAULT_TIMEOUT, "max_timeout_seconds": execution.MAX_TIMEOUT, "max_read_bytes": execution.MAX_READ,
                       "log_bytes": execution.MAX_LOG, "log_retention": "rotating", "log_segments": 4, "log_segment_bytes": execution.logs.SEGMENT_BYTES,
                       "workspace_access": ["exclusive", "shared"], "max_resource_keys": 16,
                       "git_capture_bytes": workspace.MAX_CAPTURE, "search_entries": workspace.MAX_SCAN_ENTRIES,
                       "search_bytes": workspace.MAX_SCAN_BYTES, "search_matches": workspace.MAX_MATCHES,
                       "text_file_bytes": workspace.MAX_TEXT_FILE, "text_page_bytes": workspace.TEXT_PAGE,
                       "checkpoint_bytes": tasks.MAX_CHECKPOINT, "task_index_bytes": tasks.MAX_INDEX},
            "boundary": "Native account access, not a sandbox. No live ChatGPT/Tunnel health probe.",
        })

    @mcp.tool(annotations=READ, structured_output=False)
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

    @mcp.tool(annotations=READ, structured_output=False)
    def read_workspace_file(machine: str, workspace_name: str, path: str,
                            offset: Annotated[int, Field(ge=0, le=131072)] = 0,
                            expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "") -> CallToolResult:
        """Read one configured-workspace text file, including untracked source, with whole-file SHA256, Git context and explicit absence. Follow next_offset and echo expected_sha256 until has_more=false; never overwrite a file from partial/redacted text. Bounded UTF-8 pages; no writes, execution or external access."""
        return _reply(lambda: workspace.read_text_file(config, machine, workspace_name, path, offset, expected_sha256))

    @mcp.tool(annotations=LOCAL_WRITE, structured_output=False)
    def write_workspace_file(machine: str, workspace_name: str, path: str,
                             content: Annotated[str, Field(max_length=131072)],
                             expected_head_sha: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40})?$")],
                             expected_sha256: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")]) -> CallToolResult:
        """Create or replace exactly one authorized UTF-8 file in a configured workspace with execution enabled. Requires observed Git HEAD and whole-file SHA256; empty HEAD explicitly means no HEAD, empty SHA256 means absent. Shares native execution locks and verifies atomic publication. Rejects traversal, credential paths, links and special files. No shell, script execution, deletion, parent creation, permission-change interface or network. Read back any unknown outcome; platform approval remains independent."""
        return _reply(lambda: workspace.write_text_file(config, state, machine, workspace_name, path, content,
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
        return _reply(lambda: tasks.save(state, task_id, expected_revision, title, body, anchors))

    @mcp.tool(annotations=WRITE, structured_output=False)
    def start_execution(machine: str, execution_id: str, cwd: str,
                        argv: Annotated[list[str], Field(min_length=1, max_length=100)],
                        timeout_seconds: Annotated[int, Field(ge=1, le=execution.MAX_TIMEOUT)] = execution.DEFAULT_TIMEOUT,
                        expected_head_sha: str = "", workspace_access: Literal["exclusive", "shared"] = "exclusive",
                        resource_keys: Annotated[list[str], Field(max_length=16)] = [], task_id: str = "") -> CallToolResult:
        """Start one explicitly authorized native command. Save its ID before launch and query that ID after uncertainty. Optional task_id references an existing checkpoint; it never writes task progress or supplies authorization."""
        return _reply(lambda: execution.start(config, state, machine, execution_id, cwd, argv,
                                              timeout_seconds, expected_head_sha, workspace_access, resource_keys, task_id))

    @mcp.tool(annotations=READ, structured_output=False)
    def read_execution(machine: str, execution_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                       max_bytes: Annotated[int, Field(ge=1, le=65536)] = 65536) -> CallToolResult:
        """Read the original receipt and bounded output. Missing worker evidence is unknown, never success or permission to retry."""
        return _reply(lambda: execution.read(config, state, machine, execution_id, offset, max_bytes))

    @mcp.tool(annotations=READ, structured_output=False)
    def list_executions(machine: str, cursor: str = "",
                        limit: Annotated[int, Field(ge=1, le=100)] = 20) -> CallToolResult:
        """Find original execution IDs in stable ID order. Follow next_cursor and unreadable records; this is not task recovery."""
        return _reply(lambda: execution.list_records(config, state, machine, cursor, limit))

    @mcp.tool(annotations=WRITE, structured_output=False)
    def cancel_execution(machine: str, execution_id: str) -> CallToolResult:
        """Request cancellation of this owned execution only; read its terminal receipt. Does not undo previous side effects."""
        return _reply(lambda: execution.cancel(config, state, machine, execution_id))

    if config.github is not None:
        @mcp.tool(annotations=WRITE, structured_output=False)
        def github_issue(action: Literal["create", "update"], repository: str, title: str | None = None,
                         body: str | None = None, number: Annotated[int, Field(ge=0)] = 0) -> CallToolResult:
            """Create or update an enabled GitHub Issue. New public artifacts get the configured Open Machine provenance footer."""
            return _reply(lambda: github.issue(config, action, repository, title, body, number))

        @mcp.tool(annotations=WRITE, structured_output=False)
        def github_pull_request(action: Literal["create", "update"], repository: str, title: str | None = None,
                                body: str | None = None, number: Annotated[int, Field(ge=0)] = 0,
                                head: str = "", base: str = "", draft: bool = False) -> CallToolResult:
            """Create or update an enabled GitHub Pull Request with readback-verified body attribution."""
            return _reply(lambda: github.pull_request(config, action, repository, title, body, number, head, base, draft))

        @mcp.tool(annotations=WRITE, structured_output=False)
        def github_comment(action: Literal["create", "update"], repository: str, body: str,
                           number: Annotated[int, Field(ge=0)] = 0,
                           comment_id: Annotated[int, Field(ge=0)] = 0) -> CallToolResult:
            """Create or update a top-level GitHub conversation comment. Inline review comments are outside this tool."""
            return _reply(lambda: github.comment(config, action, repository, body, number, comment_id))

        @mcp.tool(annotations=WRITE, structured_output=False)
        def github_review(action: Literal["create", "update"], repository: str, body: str,
                          number: Annotated[int, Field(ge=0)] = 0, review_id: Annotated[int, Field(ge=0)] = 0,
                          expected_head_sha: str = "",
                          event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT") -> CallToolResult:
            """Create a HEAD-bound review summary or update its body. Inline comments are never decorated here."""
            return _reply(lambda: github.review(config, action, repository, body, number, review_id, expected_head_sha, event))

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


def serve(config: Settings, state: Path) -> None:
    state = execution.private_directory(state)
    path = state / "mcp.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        # bind fails on an occupied/stale path; never unlink another listener.
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
