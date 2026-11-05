"""Credential-free connections and fixed owner-only provider transport."""

import json
import os
import re
import secrets
import socket
import stat
import time

from pydantic import Field

from integrations.connection_state import CloudBinding, Connection, Model, Revision, now_ms
from integrations.github.web_broker import _identity, _peer_uid, _response
from openmachine.foundation.sanitize import Fault


class BindingRequest(CloudBinding):
    v: int = Field(strict=True, ge=1, le=1)
    github_id: Revision
    login: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    revision: Revision


def bind(owner, raw):
    try:
        request = BindingRequest.model_validate(raw)
    except (TypeError, ValueError):
        raise Fault("invalid_github_cloud_binding", "Use the exact credential-free Cloud binding.") from None
    actual = owner.web_identity()["core_binding"]
    if actual != request.core_binding:
        raise Fault("github_cloud_binding_changed", "The approved Core changed.")
    source = CloudBinding.model_validate(request.model_dump(include=set(CloudBinding.model_fields)))
    identity = source.account_id
    with owner.admit(), owner._mutex, owner._store.writing() as value:
        owner._enabled()
        previous = value.connections.get(identity)
        if previous:
            if (previous.backend != "cloud" or previous.cloud != source or previous.account_id != request.github_id
                    or previous.authorization_revision > request.revision
                    or previous.status == "disconnected" and previous.authorization_revision >= request.revision):
                raise Fault("integration_connection_changed", "The original Cloud connection changed.")
            if previous.authorization_revision == request.revision:
                if previous.status != "connected":
                    raise Fault("integration_connection_changed", "Reconnect the same Cloud account before using this binding.")
                return {"v": 1, "status": "connected", "connection_id": identity, "authorization_revision": request.revision}
        connection = Connection(id=identity, provider="github", backend="cloud", cloud=source,
            account_id=request.github_id, account_login=request.login, authorization_revision=request.revision,
            status="connected", policy=(previous.policy if previous else owner.config.github).model_copy(deep=True), verified_at_ms=now_ms())
        value.connections[identity] = connection
        owner._store.save(value)
        return {"v": 1, "status": "connected", "connection_id": identity, "authorization_revision": request.revision}


def request(state, connection, repository, write, method, endpoint, payload, scope=None):
    """One bounded forwarding attempt; uncertain business effects are never retried."""
    if connection.cloud is None:
        raise Fault("integration_connection_changed", "No Cloud authorization is bound.", effect="known_no_effect")
    source = connection.cloud.model_dump()
    value = {"v": 1, "request_id": secrets.token_hex(16), "connection_id": connection.id,
             "authorization_revision": connection.authorization_revision, "github_id": connection.account_id,
             "source": source, "repository": repository, "write": write,
             "scope": scope or {"operation": "read", "action": "", "number": 0, "project_number": 0},
             "method": method, "endpoint": endpoint, "payload": payload}
    return _exchange(state, value)


def resolve(owner):
    value = {"v": 1, "action": "resolve", "request_id": secrets.token_hex(16),
             "core_binding": owner.web_identity()["core_binding"]}
    result = _exchange(owner.state, value)
    bound = owner.cloud_bind(result)
    return bound["connection_id"]


def _exchange(state, value):
    data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > 131072:
        raise Fault("github_request_too_large", "The bounded GitHub request is too large.", effect="known_no_effect")
    client, client_identity, sent = None, None, False
    try:
        state = state.resolve(strict=True)
        state_identity = _identity(state, directory=True)
        path = state / "github-web-broker.sock"
        server_identity = _identity(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            client = state / ("web-" + secrets.token_hex(16) + ".sock")
            peer.bind(str(client)); os.chmod(client, 0o600)
            client_identity = _identity(client)
            deadline = time.monotonic() + 25
            peer.settimeout(25); peer.connect(str(path))
            if (_peer_uid(peer) != os.getuid() or peer.getpeername() != str(path)
                    or _identity(path) != server_identity or _identity(state, directory=True) != state_identity):
                raise ValueError("owner changed")
            headers = ("POST /internal/github/cloud/request HTTP/1.1\r\nHost: localhost\r\n"
                       "Content-Type: application/json\r\nConnection: close\r\n"
                       f"Content-Length: {len(data)}\r\n\r\n").encode()
            sent = True
            peer.sendall(headers + data)
            reply = _response(peer, deadline, maximum=2 * 1024 * 1024)
        if set(reply) != {"request_id", "result"} or reply["request_id"] != value["request_id"]:
            raise ValueError("original response changed")
        result = reply["result"]
        if not isinstance(result, dict) or set(result) not in ({"data"}, {"error"}):
            raise ValueError("invalid response")
        if "error" in result:
            error = result["error"]
            if (not isinstance(error, dict) or set(error) != {"code", "effect"}
                    or not isinstance(error["code"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error["code"])
                    or error["effect"] not in {"known_no_effect", "effect_unknown"}):
                raise ValueError("invalid error")
            raise Fault(error["code"], "GitHub proxy did not confirm the operation. Read the original result.", effect=error["effect"])
        return result["data"]
    except Fault:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise Fault("github_cloud_unavailable", "The original Cloud request could not be verified; do not replay it.",
                    effect="effect_unknown" if sent else "known_no_effect") from None
    finally:
        if client is not None and client_identity is not None:
            try:
                info = client.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino, info.st_uid) == client_identity:
                    client.unlink()
            except OSError:
                pass
