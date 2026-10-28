"""Explicit local tools and optional GitHub operations on a private MCP service."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import signal
import struct
import sys
import threading

from jsonschema import Draft202012Validator
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import ValidationError
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from openmachine import __version__, service, skills
from openmachine.bootstrap import create_application
from openmachine.config import Settings
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


_WEB_PREFIX = "/internal/github/web/"
_WEB_BYTES = 32768
_WEB_OWNER = object()


def _peer_uid(sock) -> int:
    if sys.platform == "linux":
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        getpeereid = ctypes.CDLL(None, use_errno=True).getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        getpeereid.restype = ctypes.c_int
        if getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return uid.value
    raise OSError("Native Unix peer ownership is unavailable.")


def _owner_http_protocol(state: Path, listener_identity):
    state_identity = state.stat()

    def owner_peer(sock):
        try:
            if sock is None or sock.family != socket.AF_UNIX or _peer_uid(sock) != os.getuid():
                return False
            current = state.lstat()
            if ((current.st_dev, current.st_ino) != (state_identity.st_dev, state_identity.st_ino)
                    or not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid()
                    or current.st_mode & 0o077):
                return False
            listener = state / "mcp.sock"
            current = listener.lstat()
            if (sock.getsockname() != str(listener) or not stat.S_ISSOCK(current.st_mode)
                    or current.st_uid != os.getuid() or current.st_mode & 0o077
                    or (current.st_dev, current.st_ino) != (listener_identity.st_dev, listener_identity.st_ino)):
                return False
            name = sock.getpeername()
            if not isinstance(name, str):
                return False
            peer = Path(name)
            if (peer.parent != state or str(peer) != name
                    or not re.fullmatch(r"web-[0-9a-f]{32}\.sock", peer.name)):
                return False
            current = peer.lstat()
            return (stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid()
                    and current.st_mode & 0o777 == 0o600 and current.st_nlink == 1)
        except (OSError, ValueError, AttributeError, struct.error):
            return False

    class OwnerH11Protocol(H11Protocol):
        def connection_made(self, transport):
            super().connection_made(transport)
            application = self.app
            sock = transport.get_extra_info("socket")

            async def native_scope(scope, receive, send):
                # Only kernel peer facts set this marker. HTTP headers cannot do so.
                scope = dict(scope)
                scope["om.github_web_owner"] = _WEB_OWNER if owner_peer(sock) else None
                await application(scope, receive, send)

            self.app = native_scope

    return OwnerH11Protocol


def _web_json(raw: bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate private request field.")
            value[key] = item
        return value
    def constant(_):
        raise ValueError("Invalid JSON number.")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a private request object.")
    return value


def _web_public_result(value):
    allowed = {"v", "core_binding", "intent_id", "expires_at_ms", "status",
               "connection_id", "authorization_revision", "error_code"}
    if not isinstance(value, dict) or set(value) - allowed or type(value.get("v")) is not int or value["v"] != 1:
        raise ValueError("Invalid private result.")
    for key, item in value.items():
        if key in {"core_binding", "intent_id", "connection_id"}:
            width = 64 if key == "core_binding" else 32
            if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{" + str(width) + "}", item):
                raise ValueError("Invalid private result identity.")
        elif key in {"expires_at_ms", "authorization_revision"}:
            if type(item) is not int or not 0 <= item <= 2**53 - 1:
                raise ValueError("Invalid private result revision.")
        elif key == "status" and item not in {"pending", "awaiting_browser", "awaiting_claim", "claimed",
                "connected", "cancelled", "expired", "rejected", "outcome_unknown"}:
            raise ValueError("Invalid private result status.")
        elif key == "error_code" and item is not None and (
                not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", item)):
            raise ValueError("Invalid private error code.")
    return value


class _GitHubWebApp:
    def __init__(self, mcp_app, connections):
        self.mcp_app, self.connections = mcp_app, connections

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(_WEB_PREFIX):
            return await self.mcp_app(scope, receive, send)

        async def respond(status, value):
            raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                (b"content-length", str(len(raw)).encode()), (b"connection", b"close")]})
            await send({"type": "http.response.body", "body": raw})

        async def fail(status, code):
            await respond(status, {"v": 1, "error": {"code": code}})

        headers = scope.get("headers", [])
        if (scope.get("om.github_web_owner") is not _WEB_OWNER
                or any(name in {b"origin", b"forwarded", b"via"} or name.startswith(b"x-forwarded-")
                       for name, _ in headers)):
            return await fail(403, "github_web_owner_required")
        hosts = [value for name, value in headers if name == b"host"]
        if len(hosts) != 1 or not re.fullmatch(rb"localhost(?::[0-9]{1,5})?", hosts[0]):
            return await fail(421, "github_web_invalid_host")
        path, method = scope["path"], scope["method"]
        if scope.get("raw_path", path.encode()) != path.encode():
            return await fail(400, "invalid_github_web_request")
        action = path[len(_WEB_PREFIX):]
        status_match = re.fullmatch(r"status/([0-9a-f]{32})", action)
        if action not in {"identity", "begin", "complete", "cancel"} and status_match is None:
            return await fail(404, "github_web_route_not_found")
        reading = action == "identity" or status_match is not None
        if method != ("GET" if reading else "POST"):
            return await fail(405, "github_web_method_not_allowed")
        if scope.get("query_string", b"") != (b"v=1" if reading else b""):
            return await fail(400, "invalid_github_web_request")
        content_types = [value.lower() for name, value in headers if name == b"content-type"]
        if not reading and (len(content_types) != 1 or content_types[0] not in {
                b"application/json", b"application/json; charset=utf-8"}):
            return await fail(415, "github_web_json_required")
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > _WEB_BYTES:
                        return await fail(413, "github_web_request_too_large")
                    if not event.get("more_body", False):
                        break
            if reading:
                if body:
                    return await fail(400, "invalid_github_web_request")
                operation = self.connections.web_identity if action == "identity" else (
                    lambda: self.connections.web_status(status_match[1]))
            else:
                request = _web_json(bytes(body))
                handler = {"begin": self.connections.web_begin, "complete": self.connections.web_complete,
                           "cancel": self.connections.web_cancel}[action]
                operation = lambda: handler(request)
        except TimeoutError:
            return await fail(408, "github_web_request_timeout")
        except (ValueError, UnicodeError, RecursionError):
            return await fail(400, "invalid_github_web_request")
        try:
            result = _web_public_result(await asyncio.to_thread(operation))
        except Fault as error:
            # Never serialize exception text, provider evidence or submitted tokens.
            code = error.code if re.fullmatch(r"[a-z][a-z0-9_]{0,99}", error.code) else "github_web_unavailable"
            return await fail(409, code)
        except Exception:
            # This credential-bearing boundary must not reach ASGI traceback logs.
            return await fail(500, "github_web_unavailable")
        await respond(200, result)


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
