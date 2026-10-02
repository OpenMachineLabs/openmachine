"""Bounded account connections; provider credentials remain with their owner."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openmachine.config import GitHubConfig, Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

PROTOCOL = 1
FORMAT = 1
MAX_BYTES = 128 * 1024
MAX_CONNECTIONS = 32
MAX_ATTEMPTS = 128
ID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Number = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Connection(Model):
    id: ID
    provider: Literal["github"]
    backend: Literal["gh"]
    account_id: Revision
    account_login: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    authorization_revision: Revision
    status: Literal["connected", "disconnected"]
    policy: GitHubConfig
    verified_at_ms: Number

    def binding(self) -> dict:
        return {"connection_id": self.id, "account_id": self.account_id,
                "authorization_revision": self.authorization_revision}


class Attempt(Model):
    intent: dict[str, str]
    status: Literal["pending", "succeeded", "failed"]
    created_at_ms: Number
    expires_at_ms: Number
    connection_id: ID | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def fields(self):
        if (set(self.intent) != {"provider", "method", "connection_id"}
                or self.intent["provider"] != "github" or self.intent["method"] != "existing_gh"
                or self.intent["connection_id"] and not re.fullmatch(r"[0-9a-f]{32}", self.intent["connection_id"])
                or self.expires_at_ms != self.created_at_ms + 120000
                or (self.status == "succeeded") != (self.connection_id is not None)
                or (self.status == "failed") != (self.error_code is not None)
                or self.error_code is not None and not re.fullmatch(r"[a-z0-9_]{1,100}", self.error_code)):
            raise ValueError("invalid connection attempt")
        return self


class Store(Model):
    format: Literal[1]
    connections: dict[ID, Connection] = Field(default_factory=dict, max_length=MAX_CONNECTIONS)
    attempts: dict[ID, Attempt] = Field(default_factory=dict, max_length=MAX_ATTEMPTS)

    @model_validator(mode="after")
    def references(self):
        if (any(key != item.id for key, item in self.connections.items())
                or any(item.connection_id not in self.connections for item in self.attempts.values()
                       if item.connection_id is not None)):
            raise ValueError("invalid saved connection reference")
        return self


def _private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not directory and (info.st_nlink != 1 or info.st_size > MAX_BYTES)):
        raise Fault("integration_store_invalid", "Connection state must remain private, bounded and free of links.")


def _load(path: Path) -> Store:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate connection field")
            value[key] = item
        return value
    _private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise ValueError("unsafe connection file")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("oversized connection file")
    value = json.loads(raw, object_pairs_hook=pairs)
    if (not isinstance(value, dict) or set(value) != {"format", "connections", "attempts"}
            or type(value.get("format")) is not int):
        raise ValueError("invalid format")
    return Store.model_validate(value)


def _read(state: Path) -> Store | None:
    path, lock = state / "connections.json", state / "locks/connections.lock"
    if not os.path.lexists(path):
        if os.path.lexists(lock):
            raise Fault("integration_store_invalid", "Saved connection state is missing; preserve the original state.")
        return None
    try:
        for directory in (state, state / "locks"):
            _private(directory, directory=True)
        _private(lock)
        with lease(lock, shared=True, create=False):
            return _load(path)
    except (OSError, ValueError) as error:
        raise Fault("integration_store_invalid", "Saved connections are unavailable or invalid; no state was reset.") from error


def check_state(state: Path) -> dict:
    value = _read(state)
    encoded = json.dumps(value.model_dump(), sort_keys=True).encode() if value is not None else None
    return {"connections_present": value is not None, "connections_format": FORMAT,
            "connections_sha256": hashlib.sha256(encoded).hexdigest() if encoded is not None else None}


class Connections:
    def __init__(self, config: Settings, state: Path, backend=None):
        self.config, self.state, self.backend = config, state, backend
        self._active: set[str] = set()
        self._active_lock = threading.Lock()

    @contextmanager
    def _writing(self):
        existing = _read(self.state)
        private_directory(self.state)
        locks = private_directory(self.state / "locks")
        with lease(locks / "connections.lock"):
            path = self.state / "connections.json"
            if existing is None and not path.exists():
                value = Store(format=FORMAT)
                self._save(value)
            else:
                try:
                    value = _load(path)
                except (OSError, ValueError) as error:
                    raise Fault("integration_store_invalid", "Existing connection state is invalid; preserve it.") from error
            yield value

    def _save(self, value: Store) -> None:
        data = value.model_dump()
        Store.model_validate(data)
        if len(json.dumps(data).encode()) > MAX_BYTES:
            raise Fault("integration_capacity", "Connection state is full; no saved connection was replaced.")
        save_json(self.state / "connections.json", data)

    def _enabled(self) -> None:
        if self.backend is None or self.config.github is None:
            raise Fault("github_not_allowed", "GitHub is not enabled in this Open Machine configuration.")

    def _policy(self, item: Connection, repository: str, *, write: bool = False) -> None:
        self._enabled()
        if repository:
            self.backend.authorize(self.config, repository, write=write)
            self.backend.authorize(self.config.model_copy(update={"github": item.policy}), repository, write=write)
        if self.config.github.account and item.account_login.casefold() != self.config.github.account.casefold():
            raise Fault("github_identity_changed", "This connection is not the configured GitHub account.")
        if item.status != "connected":
            raise Fault("integration_disconnected", "This OM connection is disconnected; explicitly reconnect it.")

    def _new(self, actor: dict) -> Connection:
        identity = hashlib.sha256(f"github.com:gh:{actor['id']}".encode()).hexdigest()[:32]
        return Connection(id=identity, provider="github", backend="gh", account_id=actor["id"],
                          account_login=actor["login"], authorization_revision=1, status="connected",
                          policy=self.config.github.model_copy(deep=True), verified_at_ms=time.time_ns() // 1000000)

    def list(self, provider: str = "", connection_id: str = "") -> dict:
        if provider not in {"", "github"}:
            raise Fault("integration_unsupported", "Select an installed integration.")
        value = _read(self.state) or Store(format=FORMAT)
        items = [item.model_dump() for item in value.connections.values()
                 if not connection_id or item.id == connection_id]
        return {"protocol": PROTOCOL, "providers": [{"provider": "github", "supported": True,
                "enabled": self.config.github is not None, "methods": ["existing_gh"], "native_login": False}],
                "connections": items, "observed_at_ms": time.time_ns() // 1000000,
                "provider_health": "not_probed"}

    def _attempt(self, identity: str, value: Store) -> dict:
        attempt = value.attempts.get(identity)
        if attempt is None:
            raise Fault("integration_login_not_found", "No retained attempt has this ID; absence does not authorize replay.")
        result = attempt.model_dump(exclude={"intent"})
        with self._active_lock:
            active = identity in self._active
        if attempt.status == "pending" and not active:
            result.update(status="failed", error_code="runtime_interrupted")
        return {"protocol": PROTOCOL, "login_id": identity, "action": None, **result}

    def read_login(self, login_id: str) -> dict:
        return self._attempt(login_id, _read(self.state) or Store(format=FORMAT))

    def connect(self, request_id: str, provider: str = "github", method: str = "existing_gh",
                connection_id: str = "") -> dict:
        if (not re.fullmatch(r"[0-9a-f]{32}", request_id)
                or connection_id and not re.fullmatch(r"[0-9a-f]{32}", connection_id)):
            raise Fault("invalid_integration_request", "Use the original bounded request/connection IDs.")
        intent = dict(provider=provider, method=method, connection_id=connection_id)
        original = _read(self.state)
        if original is not None and request_id in original.attempts:
            if original.attempts[request_id].intent != intent:
                raise Fault("integration_request_conflict", "This request ID belongs to another connection intent.")
            return self._attempt(request_id, original)
        if connection_id and (original is None or connection_id not in original.connections):
            raise Fault("integration_not_found", "The selected connection does not exist.")
        self._enabled()
        if provider != "github" or method != "existing_gh":
            raise Fault("integration_unsupported", "This increment only attaches the existing gh identity.")
        now = time.time_ns() // 1000000
        with self._writing() as value:
            if request_id in value.attempts:
                if value.attempts[request_id].intent != intent:
                    raise Fault("integration_request_conflict", "The original request has different intent.")
                return self._attempt(request_id, value)
            if len(value.attempts) >= MAX_ATTEMPTS:
                raise Fault("integration_capacity", "The bounded connection store is full; existing entries remain intact.")
            if connection_id and connection_id not in value.connections:
                raise Fault("integration_not_found", "The selected connection does not exist.")
            before = {key: item.authorization_revision for key, item in value.connections.items()}
            value.attempts[request_id] = Attempt(intent=intent, status="pending", created_at_ms=now, expires_at_ms=now + 120000)
            self._save(value)
            with self._active_lock:
                self._active.add(request_id)
        try:
            candidate = self._new(self.backend.read_actor(self.config))
            if connection_id and candidate.id != connection_id:
                raise Fault("github_identity_changed", "Reconnection must use the original account; no binding was replaced.")
            with self._writing() as value:
                previous = value.connections.get(candidate.id)
                if (previous.authorization_revision if previous else None) != before.get(candidate.id):
                    raise Fault("integration_connection_changed", "The connection changed during attachment; the late result was rejected.")
                if time.time_ns() // 1000000 >= value.attempts[request_id].expires_at_ms:
                    raise Fault("integration_login_expired", "The existing-account probe exceeded its deadline.")
                if previous is None and len(value.connections) >= MAX_CONNECTIONS:
                    raise Fault("integration_capacity", "The saved connection limit was reached.")
                if previous:
                    changed = previous.status != "connected" or previous.policy != candidate.policy
                    candidate.authorization_revision = previous.authorization_revision + int(changed)
                value.connections[candidate.id] = candidate
                value.attempts[request_id].status = "succeeded"
                value.attempts[request_id].connection_id = candidate.id
                self._save(value)
                return self._attempt(request_id, value)
        except Fault as error:
            with self._writing() as value:
                attempt = value.attempts[request_id]
                if attempt.status == "pending":
                    attempt.status, attempt.error_code = "failed", error.code
                    self._save(value)
            raise
        finally:
            with self._active_lock:
                self._active.discard(request_id)

    def disconnect(self, connection_id: str, expected_revision: int) -> dict:
        if type(expected_revision) is not int or not 1 <= expected_revision <= 2**53 - 1:
            raise Fault("invalid_integration_request", "Use the observed positive authorization revision.")
        observed = _read(self.state)
        if observed is None or connection_id not in observed.connections:
            raise Fault("integration_not_found", "The selected connection does not exist.")
        with self._writing() as value:
            item = value.connections.get(connection_id)
            if item is None:
                raise Fault("integration_not_found", "The selected connection does not exist.")
            pending = any(attempt.status == "pending" and attempt.intent["connection_id"] in {"", connection_id}
                          for attempt in value.attempts.values())
            if item.status != "disconnected" or pending and item.authorization_revision == expected_revision:
                if item.authorization_revision != expected_revision:
                    raise Fault("integration_connection_changed", "Read the current authorization revision before disconnecting.")
                item.status = "disconnected"
                item.authorization_revision += 1
                self._save(value)
            return {"protocol": PROTOCOL, "connection": item.model_dump(),
                    "credential_cleanup": "externally_managed", "remote_revocation": "not_requested"}

    def select(self, repository: str = "", *, connection_id: str = "", binding: dict | None = None,
               write: bool = False, legacy: bool = False, persist: bool = False) -> Connection:
        self._enabled()
        if repository:
            self.backend.authorize(self.config, repository, write=write)
        value = _read(self.state) or Store(format=FORMAT)
        if binding is not None and connection_id and connection_id != binding["connection_id"]:
            raise Fault("integration_connection_changed", "The caller selected a different account from the task binding.")
        selected = binding["connection_id"] if binding is not None else connection_id
        if selected:
            item = value.connections.get(selected)
            if item is None:
                raise Fault("integration_not_found", "The original connection is unavailable; no other account was selected.")
        else:
            # Legacy tasks never follow a future native default; they retain external gh ownership.
            candidates = [item for item in value.connections.values() if item.status == "connected"
                          and (not self.config.github.account or item.account_login.casefold() == self.config.github.account.casefold())] if not legacy else []
            if len(candidates) > 1:
                raise Fault("integration_selection_required", "Select a connection explicitly; multiple accounts are saved.")
            if candidates:
                item = candidates[0]
            else:
                if value.connections and not legacy:
                    raise Fault("integration_disconnected", "No eligible connection is active; explicitly attach the intended account.")
                item = self._new(self.backend.read_actor(self.config))
                item = value.connections.get(item.id, item)
        self._policy(item, repository, write=write)
        if binding is not None and item.binding() != binding:
            raise Fault("integration_connection_changed", "The admitted account or authorization revision changed.")
        if persist and item.id not in value.connections:
            with self._writing() as current:
                if item.id in current.connections:
                    item = current.connections[item.id]
                    self._policy(item, repository, write=write)
                else:
                    if len(current.connections) >= MAX_CONNECTIONS:
                        raise Fault("integration_capacity", "The saved connection limit was reached.")
                    current.connections[item.id] = item
                    self._save(current)
        return item

    def request(self, repository: str = "", *, connection_id: str = "", binding: dict | None = None,
                write: bool = False, legacy: bool = False):
        item = self.select(repository, connection_id=connection_id, binding=binding, write=write, legacy=legacy)
        selected = item.binding()
        saved = _read(self.state)
        was_saved = saved is not None and item.id in saved.connections
        def send(method: str, endpoint: str, payload: dict | None = None):
            current = _read(self.state)
            retained = current.connections.get(item.id) if current is not None else None
            if retained is not None:
                self._policy(retained, repository, write=write)
                if retained.binding() != selected:
                    raise Fault("integration_connection_changed", "This request's authorization is no longer current.")
            elif binding is not None or was_saved:
                raise Fault("integration_not_found", "The original saved connection is missing.")
            actor = self.backend.read_actor(self.config)
            if actor["id"] != item.account_id:
                raise Fault("github_identity_changed", "The external gh identity changed; no other account was selected.")
            return actor if method == "GET" and endpoint == "user" else self.backend.request(method, endpoint, payload)
        return send
