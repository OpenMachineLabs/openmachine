"""One fixed private Connector refresh exchange; no retry or provider routing."""

import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys
import time

from integrations.github.auth import WEB_TOKEN_FIELDS, accept_web_tokens
from openmachine.foundation.sanitize import Fault

SOCKET_NAME = "github-web-broker.sock"
REFRESH_PATH = "/internal/github/web/refresh"
MAX_HEADERS = 8192
MAX_BODY = 16384
TIMEOUT_SECONDS = 10


def _failure():
    return Fault("github_reauthorization_required",
                 "The original web-token refresh could not be verified; authorize the same account again.")


def _peer_uid(peer) -> int:
    if sys.platform == "linux":
        return struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                  struct.calcsize("3i")))[1]
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        read = ctypes.CDLL(None, use_errno=True).getpeereid
        read.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        read.restype = ctypes.c_int
        if read(peer.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return uid.value
    raise ValueError("unverified local peer")


def _identity(path: Path, *, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISSOCK
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe local endpoint")
    return info.st_dev, info.st_ino, info.st_uid


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate response field")
        value[key] = item
    return value


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("refresh exchange timed out")
    return remaining


def _response(peer, deadline: float) -> dict:
    def receive(limit):
        peer.settimeout(_remaining_timeout(deadline))
        chunk = peer.recv(limit)
        if not chunk:
            raise ValueError("incomplete refresh response")
        return chunk

    raw = bytearray()
    while b"\r\n\r\n" not in raw:
        raw.extend(receive(4096))
        end = raw.find(b"\r\n\r\n")
        if end > MAX_HEADERS or end < 0 and len(raw) > MAX_HEADERS:
            raise ValueError("oversized refresh headers")
    header, body = bytes(raw).split(b"\r\n\r\n", 1)
    lines = header.decode("ascii").split("\r\n")
    if not re.fullmatch(r"HTTP/1\.[01] 200(?: [\x20-\x7e]*)?", lines[0]):
        raise ValueError("refresh not accepted")
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        name = name.lower()
        if not separator or not re.fullmatch(r"[a-z0-9-]+", name) or name in headers:
            raise ValueError("invalid refresh headers")
        headers[name] = value.strip()
    if (not re.fullmatch(r"[0-9]{1,5}", headers.get("content-length", ""))
            or headers.get("content-type", "").split(";", 1)[0].lower() != "application/json"
            or "no-store" not in {part.strip().lower() for part in headers.get("cache-control", "").split(",")}
            or "transfer-encoding" in headers or headers.get("content-encoding", "identity") != "identity"):
        raise ValueError("invalid refresh framing")
    size = int(headers["content-length"])
    if not 0 < size <= MAX_BODY or len(body) > size:
        raise ValueError("invalid refresh size")
    while len(body) < size:
        body += receive(min(4096, size - len(body)))
    value = json.loads(body, object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise ValueError("invalid refresh object")
    return value


def refresh(state: Path, *, refresh_id: str, connection_id: str, github_account_id: int,
            core_binding: str, expected_authorization_revision: int, refresh_token: str):
    """The owner must durably reserve refresh_id before calling; every failure is final."""
    client_path, client_identity = None, None
    try:
        for identity in (refresh_id, connection_id):
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
                raise ValueError("invalid original identity")
        if not isinstance(core_binding, str) or not re.fullmatch(r"[0-9a-f]{64}", core_binding):
            raise ValueError("invalid Core binding")
        for number in (github_account_id, expected_authorization_revision):
            if type(number) is not int or not 0 < number <= 2**53 - 1:
                raise ValueError("invalid account or revision")
        if (not isinstance(refresh_token, str) or not 16 <= len(refresh_token) <= 2048
                or not refresh_token.isascii()
                or any(ord(char) <= 32 or ord(char) == 127 for char in refresh_token)):
            raise ValueError("invalid refresh token")
        state_identity = _identity(state, directory=True)
        state = state.resolve(strict=True)
        path = state / SOCKET_NAME
        server_identity = _identity(path)
        data = json.dumps(dict(v=1, refresh_id=refresh_id, connection_id=connection_id,
            github_account_id=github_account_id, core_binding=core_binding,
            expected_authorization_revision=expected_authorization_revision,
            refresh_token=refresh_token), separators=(",", ":")).encode("ascii")
        request = (f"POST {REFRESH_PATH} HTTP/1.1\r\nHost: localhost\r\n"
                   f"Content-Type: application/json\r\nAccept: application/json\r\n"
                   f"Cache-Control: no-store\r\nConnection: close\r\nContent-Length: {len(data)}\r\n\r\n").encode("ascii")
        deadline = time.monotonic() + TIMEOUT_SECONDS
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            client_path = state / ("web-" + secrets.token_hex(16) + ".sock")
            peer.bind(str(client_path))
            bound = client_path.lstat()
            client_identity = bound.st_dev, bound.st_ino, bound.st_uid
            os.chmod(client_path, 0o600)
            if _identity(client_path) != client_identity:
                raise ValueError("local client changed")
            peer.settimeout(_remaining_timeout(deadline))
            peer.connect(str(path))
            if (_peer_uid(peer) != os.getuid() or peer.getpeername() != str(path)
                    or _identity(path) != server_identity or _identity(state, directory=True) != state_identity
                    or _identity(client_path) != client_identity):
                raise ValueError("local endpoint changed")
            peer.settimeout(_remaining_timeout(deadline))
            peer.sendall(request + data)
            value = _response(peer, deadline)
        if (set(value) != WEB_TOKEN_FIELDS | {"v", "refresh_id"}
                or type(value.get("v")) is not int or value["v"] != 1
                or value.get("refresh_id") != refresh_id):
            raise ValueError("refresh response binding changed")
        pair = accept_web_tokens({key: value[key] for key in WEB_TOKEN_FIELDS}, core_binding=core_binding)
        return pair.model_copy(update={"refresh_id": refresh_id})
    except (Fault, OSError, ValueError, TypeError, AttributeError, struct.error, RecursionError):
        # Socket/parser/provider diagnostics must never include exchange material.
        raise _failure() from None
    finally:
        if client_path is not None and client_identity is not None:
            try:
                current = client_path.lstat()
                if (stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid()
                        and (current.st_dev, current.st_ino, current.st_uid) == client_identity):
                    client_path.unlink()
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                # Never remove a replacement path or overwrite an original outcome.
                pass
