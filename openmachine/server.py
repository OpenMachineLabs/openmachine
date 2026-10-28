"""Explicit local tools and optional GitHub operations on a private MCP service."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import signal
import sys
import threading

from jsonschema import Draft202012Validator
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import ValidationError
import uvicorn

from openmachine import __version__, service, skills
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.github_web_http import _GitHubWebApp, _owner_http_protocol
from openmachine.service.updates import ReleaseUpdates
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from openmachine.mcp.protocol import READ, _reply, _result, MAX_WAITERS
from openmachine.mcp.connections import register_connections, register_github_tools
from openmachine.mcp.workspace import register_workspace_tools
from openmachine.mcp.environment import register_environment_tools
from openmachine.mcp.tasks import register_task_tools
from openmachine.mcp.execution import register_execution_tools


def create_server(config: Settings, state: Path, *, application=None) -> MCPServer:
    application = application if application is not None else create_application(config, state)
    waiting = threading.BoundedSemaphore(MAX_WAITERS)
    updates = ReleaseUpdates(state, config.check_for_updates)

    @asynccontextmanager
    async def lifespan(_server):
        # SDK 2.2 enters this once per HTTP manager, not per request/session.
        with application.connections.running(), application.task_observer.running():
            async with updates.running():
                yield {}

    validators: dict[str, Draft202012Validator] = {}
    catalog: dict = {}
    tool_entry: ContextVar[threading.Event | None] = ContextVar("om_tool_entry", default=None)

    async def load_catalog() -> dict:
        if not validators:
            tools = await mcp.list_tools()
            descriptors = [tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                           for tool in sorted(tools, key=lambda item: item.name)]
            raw = (json.dumps(descriptors, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            validators.update({tool.name: Draft202012Validator(tool.input_schema) for tool in tools})
            catalog.update(sha256=hashlib.sha256(raw).hexdigest(), tool_count=len(tools),
                           source="server_tools_list", client_match="not_observed")
        return dict(catalog)

    async def safe_tool_call(ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        await load_catalog()

        def invalid(reason: str, *, handler_entered: bool = False) -> dict:
            # Only pre-handler validation proves this call did not dispatch an effect.
            message = ("The Core handler did not return a verified result. Read the original "
                       "task/operation before any retry; execution may have occurred."
                       if handler_entered else
                       "Core rejected this call before its handler. Compare the name and inputs "
                       "with tools/list; refresh the client's stored catalog if it differs.")
            result = _result({"error": {"code": "invalid_tool_call", "message": message},
                "diagnostic": {"source": "core.mcp", "reason": reason,
                    "phase": "handler" if handler_entered else "validation",
                    "current_call_dispatch": "unknown" if handler_entered else "not_attempted",
                    "core_version": __version__, "catalog_sha256": catalog["sha256"]}}, True).model_dump(
                        mode="json", by_alias=True, exclude_none=True, exclude={"result_type"})
            # Middleware short-circuits bypass the SDK's per-version envelope serializer.
            if ctx.protocol_version == "2026-07-28":
                result.update(resultType="complete", _meta={
                    "io.modelcontextprotocol/serverInfo": {"name": mcp.name, "version": mcp.version}})
            return result

        params = ctx.params
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return invalid("invalid_request")
        validator = validators.get(params["name"])
        arguments = params.get("arguments")
        if validator is None:
            return invalid("unknown_tool")
        if not validator.is_valid({} if arguments is None else arguments):
            return invalid("invalid_arguments")
        # The new contract forbids extra fields before SDK argument filtering.
        guarded = {"inspect_context", "read_task", "wait_task", "start_codex_task", "read_codex_task", "create_task", "control_task", "start_execution", "create_worktree", "cancel_execution", "control_execution",
                   "write_workspace_file", "edit_workspace_file", "apply_workspace_patch", "github_issue", "github_release",
                   "github_pull_request", "github_comment", "github_review", "github_review_thread", "github_merge",
                   "list_integrations", "connect_integration", "read_integration_login",
                   "cancel_integration_login", "disconnect_integration"}
        if (params["name"] in guarded or "task_context" in (arguments or {})) and set(arguments or {}) - validator.schema["properties"].keys():
            return invalid("unexpected_arguments")
        # The mutable marker crosses the SDK's copied worker-thread context.
        entered = threading.Event()
        token = tool_entry.set(entered)
        try:
            try:
                response = await call_next(ctx)
            except ValidationError:
                return invalid("handler_validation" if entered.is_set() else "sdk_validation",
                               handler_entered=entered.is_set())
            if (isinstance(response, dict) and response.get("isError") is True
                    and response.get("structuredContent") is None):
                return invalid("handler_error" if entered.is_set() else "sdk_validation",
                               handler_entered=entered.is_set())
            return response
        finally:
            tool_entry.reset(token)

    mcp = MCPServer(
        "Open Machine", instructions="Read inspect_skills(action='read') for the operating overview. "
        "Each tool's Guide gives an exact bounded read for its instructions; guides never grant permission.",
        version=__version__, middleware=[safe_tool_call], lifespan=lifespan,
    )

    def guided_tool(*, annotations):
        def register(operation):
            operation.__doc__ = skills.describe_tool(operation.__name__, operation.__doc__ or "")
            def mark_entry():
                if (entered := tool_entry.get()) is not None:
                    entered.set()

            if asyncio.iscoroutinefunction(operation):
                @wraps(operation)
                async def handler(*args, **kwargs):
                    mark_entry()
                    return await operation(*args, **kwargs)
            else:
                @wraps(operation)
                def handler(*args, **kwargs):
                    mark_entry()
                    return operation(*args, **kwargs)
            return mcp.tool(annotations=annotations, structured_output=False)(handler)
        return register

    private_directory(state)
    private_directory(state / "locks")

    has_ssh = any(item.transport == "ssh" for item in config.machines.values())
    machine_read = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=has_ssh)
    file_write = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=has_ssh)

    @guided_tool(annotations=READ)
    async def get_capabilities() -> CallToolResult:
        """Report implemented limits and configured targets, not live health or future features."""
        advertised = await load_catalog()
        return await asyncio.to_thread(_reply, lambda: {
            **application.get_capabilities(), "mcp_catalog": advertised, "updates": updates.snapshot()})

    register_connections(application, guided_tool)

    register_environment_tools(application, guided_tool, machine_read)

    register_workspace_tools(application, guided_tool, machine_read, file_write)

    register_task_tools(application, guided_tool, waiting)

    register_execution_tools(application, guided_tool, waiting, machine_read, has_ssh)

    register_github_tools(application, config, guided_tool)

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
            application = create_application(config, state)
            mcp_app = create_server(config, state, application=application).streamable_http_app(
                stateless_http=True, json_response=True, max_request_body_size=1048576,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=["localhost", "localhost:*"],
                    allowed_origins=["http://localhost", "http://localhost:*"],
                ),
            )
            app = _GitHubWebApp(mcp_app, application.connections)
            uvicorn.Server(uvicorn.Config(app, http=_owner_http_protocol(state, identity),
                access_log=False, log_level="warning")).run(sockets=[listener])
        finally:
            signal.signal(signal.SIGTERM, previous)
            if path.exists() and path.stat().st_ino == identity.st_ino:
                path.unlink()
