"""Connection records, compatibility checks and the shared persistence boundary."""

from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from openmachine.config import GitHubConfig
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

FORMAT = 3
MAX_BYTES = 128 * 1024
MAX_CONNECTIONS = 32
MAX_ATTEMPTS = 128
MAX_LOGINS = 4
ID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Number = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
WEB_INTENT_MS = 10 * 60 * 1000


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DeviceAction(Model):
    type: Literal["device_code"] = "device_code"
    verification_url: Literal["https://github.com/login/device"]
    installation_url: Literal["https://github.com/apps/open-machine-connector/installations/new"]
    user_code: str = Field(pattern=r"^[A-Z0-9-]{4,32}$")


class Connection(Model):
    id: ID
    provider: Literal["github"]
    backend: Literal["gh", "native"]
    account_id: Revision
    account_login: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    authorization_revision: Revision
    status: Literal["connected", "disconnected", "reauthorization_required"]
    policy: GitHubConfig
    verified_at_ms: Number
    credential_store: Literal["keychain", "file"] | None = None
    credential_ref: ID | None = None
    refresh_expires_at_ms: Number | None = None

    @model_validator(mode="after")
    def native_fields(self):
        values = (self.credential_store, self.credential_ref, self.refresh_expires_at_ms)
        invalid = any(v is None for v in values) if self.backend == "native" else any(v is not None for v in values)
        if invalid:
            raise ValueError("invalid credential binding")
        return self

    def binding(self) -> dict:
        return {"connection_id": self.id, "account_id": self.account_id,
                "authorization_revision": self.authorization_revision}

    def public(self) -> dict:
        value = self.model_dump(exclude={"credential_ref", "credential_store"}, exclude_none=True)
        value.update(historical_only=self.backend == "gh", resource_access="not_probed")
        return value


class Attempt(Model):
    intent: dict[str, str]
    status: Literal["pending", "succeeded", "failed", "cancelled", "expired"]
    created_at_ms: Number
    expires_at_ms: Number
    connection_id: ID | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,100}$")
    action: DeviceAction | None = None
    target_revision: Revision | None = None
    credential_store: Literal["keychain", "file"] | None = None

    @model_validator(mode="after")
    def valid(self):
        if (set(self.intent) != {"provider", "method", "connection_id"}
                or self.intent["provider"] != "github" or self.intent["method"] not in {"existing_gh", "device_code"}
                or self.intent["connection_id"] and not re.fullmatch(r"[0-9a-f]{32}", self.intent["connection_id"])
                or not self.created_at_ms < self.expires_at_ms <= self.created_at_ms + 3660000
                or (self.status == "succeeded") != (self.connection_id is not None)
                or (self.status in {"failed", "expired"}) != (self.error_code is not None)):
            raise ValueError("invalid login attempt")
        if self.intent["method"] == "device_code" and self.credential_store is None:
            raise ValueError("missing native login binding")
        return self


class WebBegin(Model):
    v: Annotated[int, Field(strict=True, ge=1, le=1)]
    intent_id: ID
    core_binding: Fingerprint
    expected_connection_id: ID | None = None
    expected_authorization_revision: Revision | None = None

    @model_validator(mode="after")
    def bound_revision(self):
        if type(self.v) is not int or ((self.expected_connection_id is None)
                != (self.expected_authorization_revision is None)):
            raise ValueError("reauthorization requires both original binding fields")
        return self


class WebComplete(Model):
    v: Annotated[int, Field(strict=True, ge=1, le=1)]
    intent_id: ID
    claim_id: ID
    github_account_id: Revision
    tokens: dict = Field(repr=False)

    @model_validator(mode="after")
    def version(self):
        if type(self.v) is not int or self.intent_id == self.claim_id:
            raise ValueError("independent original identifiers are required")
        return self


class WebCancel(Model):
    v: Annotated[int, Field(strict=True, ge=1, le=1)]
    intent_id: ID

    @model_validator(mode="after")
    def version(self):
        if type(self.v) is not int:
            raise ValueError("invalid version")
        return self


class WebIntent(Model):
    request: WebBegin
    configuration_sha256: Fingerprint
    credential_store: Literal["keychain", "file"]
    created_at_ms: Number
    expires_at_ms: Number
    status: Literal["pending", "claimed", "connected", "cancelled", "expired", "rejected", "outcome_unknown"]
    claim_id: ID | None = None
    github_account_id: Revision | None = None
    connection_id: ID | None = None
    authorization_revision: Revision | None = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,100}$")

    @model_validator(mode="after")
    def original(self):
        if (self.expires_at_ms != self.created_at_ms + WEB_INTENT_MS
                or (self.claim_id is None) != (self.github_account_id is None)
                or self.claim_id == self.request.intent_id
                or self.status in {"claimed", "connected", "outcome_unknown"} and self.claim_id is None
                or (self.status == "connected") != (self.connection_id is not None)
                or (self.connection_id is None) != (self.authorization_revision is None)):
            raise ValueError("invalid original web intent")
        return self


class Store(Model):
    format: Literal[1, 2, 3]
    connections: dict[ID, Connection] = Field(default_factory=dict, max_length=MAX_CONNECTIONS)
    attempts: dict[ID, Attempt] = Field(default_factory=dict, max_length=MAX_ATTEMPTS)
    web_core_id: ID | None = None
    web_intents: dict[ID, WebIntent] = Field(default_factory=dict, max_length=MAX_ATTEMPTS)

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        if self.format < 3:
            value.pop("web_core_id")
            value.pop("web_intents")
        return value

    @model_validator(mode="after")
    def valid(self):
        if (any(k != v.id for k, v in self.connections.items())
                or any(a.connection_id not in self.connections for a in self.attempts.values() if a.connection_id)):
            raise ValueError("invalid connection reference")
        if self.format == 1 and (any(c.backend != "gh" for c in self.connections.values())
                or any(a.intent["method"] != "existing_gh" for a in self.attempts.values())):
            raise ValueError("native state requires format 2")
        claims = [a.claim_id for a in self.web_intents.values() if a.claim_id]
        if (self.format < 3 and (self.web_core_id is not None or self.web_intents)
                or self.web_intents and self.web_core_id is None
                or any(k != a.request.intent_id for k, a in self.web_intents.items())
                or set(self.attempts) & set(self.web_intents)
                or len(self.attempts) + len(self.web_intents) > MAX_ATTEMPTS
                or len(claims) != len(set(claims))
                or set(claims) & (set(self.attempts) | set(self.web_intents))
                or any(a.connection_id not in self.connections for a in self.web_intents.values() if a.connection_id)):
            raise ValueError("invalid web authorization history")
        return self


@contextmanager
def _guard(path: Path, *, shared=False, create=True):
    deadline = time.monotonic() + 5
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(lease(path, shared=shared, create=create))
                break
            except Fault as error:
                if error.code != "resource_busy" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        yield


def _private(path: Path, *, directory=False):
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not directory and (info.st_nlink != 1 or info.st_size > MAX_BYTES)):
        raise ValueError("unsafe connection state")


def _load(path: Path) -> Store:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate field")
            value[key] = item
        return value
    _private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("unsafe connection state")
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("oversized state")
    value = json.loads(raw, object_pairs_hook=pairs)
    fields = {"format", "connections", "attempts"}
    if isinstance(value, dict) and value.get("format") == 3:
        fields |= {"web_core_id", "web_intents"}
    if not isinstance(value, dict) or set(value) != fields or type(value["format"]) is not int:
        raise ValueError("invalid state fields")
    return Store.model_validate(value)


def _read(state: Path) -> Store | None:
    path, lock = state / "connections.json", state / "locks/connections.lock"
    if not os.path.lexists(path) and not os.path.lexists(lock):
        return None
    try:
        _private(state, directory=True)
        _private(state / "locks", directory=True)
        _private(lock)
        # An initializing writer owns this lock before the first JSON publish.
        with _guard(lock, shared=True, create=False):
            return _load(path)
    except (OSError, ValueError):
        raise Fault("integration_store_invalid", "Connection state is missing or invalid; preserve it.") from None


def check_state(state: Path, *, require_idle: bool = False) -> dict:
    value = _read(state)
    if require_idle and value is not None and (any(item.status == "pending" for item in value.attempts.values())
            or any(item.status == "claimed" for item in value.web_intents.values())):
        raise Fault("activation_deferred", "An original native login is pending; activation must wait for its owner.")
    data = None if value is None else value.model_dump()
    if value is not None and value.format == 1:
        for item in data["connections"].values():
            for key in ("credential_store", "credential_ref", "refresh_expires_at_ms"):
                item.pop(key)
        for item in data["attempts"].values():
            for key in ("action", "target_revision", "credential_store"):
                item.pop(key)
    return {"connections_present": value is not None, "connections_format": value.format if value else FORMAT,
            "connections_sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() if data else None}



class ConnectionStore:
    def __init__(self, state: Path):
        self.state = state

    def read(self) -> Store | None:
        return _read(self.state)

    @contextmanager
    def writing(self):
        path, lock = self.state / "connections.json", self.state / "locks/connections.lock"
        fresh = not os.path.lexists(path) and not os.path.lexists(lock)
        private_directory(self.state)
        private_directory(self.state / "locks")
        with _guard(lock):
            try:
                if os.path.lexists(path):
                    value = _load(path)
                elif fresh:
                    value = Store(format=2)
                    self.save(value)
                else:
                    raise ValueError("state missing behind retained lock")
            except (OSError, ValueError):
                raise Fault("integration_store_invalid", "Do not reset missing or corrupt connection history.") from None
            yield value

    def save(self, value: Store):
        value.format = 3 if value.web_core_id is not None or value.web_intents else max(2, value.format)
        data = value.model_dump()
        Store.model_validate(data)
        if len(json.dumps(data).encode()) > MAX_BYTES:
            raise Fault("integration_capacity", "The bounded connection store is full.")
        try:
            save_json(self.state / "connections.json", data)
        except OSError:
            raise Fault("integration_store_unavailable", "Connection persistence is unverified; read the original request.") from None
