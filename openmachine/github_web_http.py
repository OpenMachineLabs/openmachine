"""Owner-only HTTP transport for the private GitHub authorization handoff."""

import asyncio
import ctypes
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys

from uvicorn.protocols.http.h11_impl import H11Protocol

from openmachine.foundation.sanitize import Fault


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
