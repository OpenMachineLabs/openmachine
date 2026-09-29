"""Five explicit tools on a private Unix-socket Streamable HTTP service."""

import hashlib
from importlib.resources import files
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

from openmachine import __version__, execution, workspace
from openmachine.config import Settings
from openmachine.sanitize import Fault

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
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
        stateless_http=True, json_response=True, max_request_body_size=65536,
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
            "skills": ["openmachine"], "integrations": [], "transport": "private_unix_socket",
            "limits": {"max_timeout_seconds": execution.MAX_TIMEOUT, "max_read_bytes": execution.MAX_READ,
                       "log_bytes": execution.MAX_LOG, "log_retention": "prefix_only", "workspace_access": "exclusive"},
            "boundary": "Native account access, not a sandbox. No live ChatGPT/Tunnel health probe.",
        })

    @mcp.tool(annotations=READ, structured_output=False)
    def inspect_workspace(machine: str, workspace_name: str, action: Literal["files", "read"],
                          path: str = ".", offset: Annotated[int, Field(ge=0)] = 0,
                          limit: Annotated[int, Field(ge=1, le=200)] = 100) -> CallToolResult:
        """List configured local files or read up to 64 KiB by byte offset; exclude credential paths."""
        return _reply(lambda: workspace.inspect(config, machine, workspace_name, action, path, offset, limit))

    @mcp.tool(annotations=READ, structured_output=False)
    def inspect_skills(action: Literal["list", "read"] = "list") -> CallToolResult:
        """List or read the bundled operating Skill. User Skill roots are not implemented yet."""
        def inspect():
            resource = files("openmachine").joinpath("_skills/SKILL.md")
            content = resource.read_bytes()
            result = {"name": "openmachine", "source": "package:openmachine/_skills/SKILL.md",
                      "sha256": hashlib.sha256(content).hexdigest()}
            if action == "read":
                result["text"] = content.decode("utf-8")
            return result
        return _reply(inspect)

    @mcp.tool(annotations=WRITE, structured_output=False)
    def start_execution(machine: str, execution_id: str, cwd: str,
                        argv: Annotated[list[str], Field(min_length=1, max_length=100)],
                        timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 600,
                        expected_head_sha: str = "") -> CallToolResult:
        """Start one explicitly authorized native command. Retain its ID before launch and query that ID after uncertainty."""
        return _reply(lambda: execution.start(config, state, machine, execution_id, cwd, argv,
                                              timeout_seconds, expected_head_sha))

    @mcp.tool(annotations=READ, structured_output=False)
    def read_execution(machine: str, execution_id: str, offset: Annotated[int, Field(ge=0)] = 0,
                       max_bytes: Annotated[int, Field(ge=1, le=65536)] = 65536) -> CallToolResult:
        """Read the original receipt and bounded output. Missing worker evidence is unknown, never success or permission to retry."""
        return _reply(lambda: execution.read(config, state, machine, execution_id, offset, max_bytes))

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
