"""One runtime owner for account bindings, native login and credential renewal."""

from contextlib import contextmanager, ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator
import integrations.credentials as credentials
from openmachine.config import GitHubConfig, Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

PROTOCOL = 2
FORMAT = 3
MAX_BYTES = 128 * 1024
MAX_CONNECTIONS = 32
MAX_ATTEMPTS = 128
MAX_LOGINS = 4
RENEW_WINDOW_MS = 30 * 86400 * 1000
MAINTENANCE_SECONDS = 6 * 3600
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


class Connections:
    def __init__(self, config: Settings, state: Path, native=None):
        self.config, self.state, self.native = config, state, native
        self.admit = nullcontext
        self._jobs: dict[str, tuple[threading.Event, threading.Thread | None]] = {}
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._running = False
        self._errors: dict[str, str] = {}
        self._web_active: set[str] = set()
        self._web_idle = threading.Event()
        self._web_idle.set()

    @contextmanager
    def running(self):
        private_directory(self.state)
        private_directory(self.state / "locks")
        with lease(self.state / "locks/connections-owner.lock"):
            self._recover_web()
            self._stop.clear()
            self._running = True
            thread = threading.Thread(target=self._maintain, name="om-connections", daemon=True)
            thread.start()
            try:
                yield self
            finally:
                with self._mutex:
                    self._running = False
                    self._stop.set()
                    jobs = list(self._jobs.values())
                    for event, _ in jobs:
                        event.set()
                # Keep ownership until already dispatched exchanges are drained.
                thread.join()
                for _, worker in jobs:
                    if worker is not None:
                        worker.join()
                self._web_idle.wait()

    @contextmanager
    def _writing(self):
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
                    self._save(value)
                else:
                    raise ValueError("state missing behind retained lock")
            except (OSError, ValueError):
                raise Fault("integration_store_invalid", "Do not reset missing or corrupt connection history.") from None
            yield value

    def _save(self, value: Store):
        value.format = 3 if value.web_core_id is not None or value.web_intents else max(2, value.format)
        data = value.model_dump()
        Store.model_validate(data)
        if len(json.dumps(data).encode()) > MAX_BYTES:
            raise Fault("integration_capacity", "The bounded connection store is full.")
        try:
            save_json(self.state / "connections.json", data)
        except OSError:
            raise Fault("integration_store_unavailable", "Connection persistence is unverified; read the original request.") from None

    def _enabled(self):
        if self.config.github is None or self.native is None:
            raise Fault("github_not_allowed", "GitHub is not enabled by the operator.")
        return self.config.github

    @staticmethod
    def _web_request(model, request):
        try:
            if not isinstance(request, dict) or type(request.get("v")) is not int or request["v"] != 1:
                raise ValueError("invalid version")
            return model.model_validate(request)
        except (ValueError, TypeError):
            raise Fault("invalid_github_web_request", "Use the exact version-1 private authorization schema.") from None

    def _web_binding(self, value: Store) -> str:
        _private(self.state, directory=True)
        info = self.state.lstat()
        if value.web_core_id is None:
            raise Fault("github_web_binding_required", "Read this Core's private identity before beginning authorization.")
        # The opaque identifier is owned by this existing store, never by a caller.
        # The local directory identity also rejects copied or replaced Core state.
        raw = [value.web_core_id, str(self.state.resolve()), info.st_dev, info.st_ino, info.st_uid]
        return hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest()

    def web_identity(self) -> dict:
        with self.admit(), self._mutex, self._writing() as value:
            self._enabled()
            if not self._running:
                raise Fault("integration_runtime_unavailable", "Start the selected Core before local authorization.")
            if value.web_core_id is None:
                value.web_core_id = secrets.token_hex(16)
                self._save(value)
            return {"v": 1, "core_binding": self._web_binding(value)}

    def _web_retained(self, value: Store | None, identity: str) -> WebIntent:
        item = value.web_intents.get(identity) if value is not None else None
        if item is None:
            raise Fault("github_web_intent_not_found", "No original web intent has this ID; do not replay it.")
        return item

    def _web_check(self, value: Store, item: WebIntent):
        config = self._enabled()
        if (item.request.core_binding != self._web_binding(value)
                or item.configuration_sha256 != self.config.configuration_sha256()):
            raise Fault("github_web_binding_changed", "The approved Core or configuration changed; authorize again.")
        if now_ms() >= item.expires_at_ms:
            raise Fault("github_web_intent_expired", "The original web authorization expired.")
        target = item.request.expected_connection_id
        previous = value.connections.get(target) if target else None
        if target and (previous is None or previous.backend != "native"
                or previous.authorization_revision != item.request.expected_authorization_revision
                or previous.status == "connected"):
            raise Fault("integration_connection_changed", "The approved account revision changed; authorize again.")
        return config, previous

    def _web_status(self, item: WebIntent) -> dict:
        result = {"v": 1, "intent_id": item.request.intent_id,
                  "core_binding": item.request.core_binding, "expires_at_ms": item.expires_at_ms,
                  "status": item.status}
        if item.error_code is not None:
            result["error_code"] = item.error_code
        if item.status == "connected":
            result.update(connection_id=item.connection_id, authorization_revision=item.authorization_revision)
        return result

    def _invalidate_web_pending(self, value: Store, item: WebIntent) -> bool:
        if item.status != "pending":
            return False
        try:
            self._web_check(value, item)
        except Fault as error:
            if error.code not in {"github_not_allowed", "github_web_binding_changed",
                                  "integration_connection_changed", "github_web_intent_expired"}:
                raise
            item.status = "expired" if error.code == "github_web_intent_expired" else "rejected"
            item.error_code = error.code
            return True
        return False

    def web_begin(self, request: dict) -> dict:
        supplied = self._web_request(WebBegin, request)
        with self.admit(), self._mutex, self._writing() as value:
            if not self._running:
                raise Fault("integration_runtime_unavailable", "Start the selected Core before local authorization.")
            retained = value.web_intents.get(supplied.intent_id)
            if retained is not None:
                if retained.request != supplied:
                    raise Fault("integration_request_conflict", "This original ID belongs to another intent.")
                if self._invalidate_web_pending(value, retained):
                    self._save(value)
                    raise Fault(retained.error_code, "The original web authorization is no longer valid; authorize again.")
                if retained.status != "pending":
                    raise Fault("github_web_intent_consumed", "Observe the original status; use new authorization to retry.")
                return self._web_status(retained)
            self._enabled()
            if supplied.core_binding != self._web_binding(value):
                raise Fault("github_web_binding_changed", "Select the original locally verified Core identity.")
            if (supplied.intent_id in value.attempts
                    or any(a.claim_id == supplied.intent_id for a in value.web_intents.values())):
                raise Fault("integration_request_conflict", "This original ID already belongs to another login.")
            active = sum(a.status == "pending" and a.expires_at_ms > now_ms() or a.status == "claimed"
                         for a in value.web_intents.values())
            if len(value.attempts) + len(value.web_intents) >= MAX_ATTEMPTS or active + len(self._jobs) >= MAX_LOGINS:
                raise Fault("integration_capacity", "The bounded original authorization history is full.")
            previous = value.connections.get(supplied.expected_connection_id)
            created = now_ms()
            item = WebIntent(request=supplied, configuration_sha256=self.config.configuration_sha256(),
                credential_store=previous.credential_store if previous and previous.backend == "native" else credentials.select(),
                created_at_ms=created, expires_at_ms=created + WEB_INTENT_MS, status="pending")
            self._web_check(value, item)
            value.web_intents[supplied.intent_id] = item
            self._save(value)
            return self._web_status(item)

    def web_status(self, intent_id: str) -> dict:
        if not isinstance(intent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", intent_id):
            raise Fault("invalid_github_web_request", "Use the original web intent ID.")
        with self.admit():
            item = self._web_retained(_read(self.state), intent_id)
            if item.status == "pending":
                with self._writing() as value:
                    item = self._web_retained(value, intent_id)
                    if self._invalidate_web_pending(value, item):
                        self._save(value)
            return self._web_status(item)

    def web_cancel(self, request: dict) -> dict:
        supplied = self._web_request(WebCancel, request)
        with self.admit(), self._writing() as value:
            item = self._web_retained(value, supplied.intent_id)
            if item.status in {"pending", "claimed"}:
                item.status, item.error_code = "cancelled", None
                self._save(value)
            return self._web_status(item)

    def _web_fail(self, identity: str, code: str, *, unknown=False):
        try:
            with self._writing() as value:
                item = self._web_retained(value, identity)
                if item.status in {"pending", "claimed"}:
                    item.status = "outcome_unknown" if unknown and item.claim_id else (
                        "expired" if code == "github_web_intent_expired" else "rejected")
                    item.error_code = code
                    self._save(value)
        except (Fault, OSError):
            self._errors[identity] = "github_web_outcome_unknown"

    def _web_cleanup(self, identity: str):
        try:
            value = _read(self.state)
            item = self._web_retained(value, identity)
            if not any(c.credential_ref == identity for c in value.connections.values()):
                credentials.delete(self.state, identity, item.credential_store)
        except (Fault, OSError):
            self._errors[identity] = "integration_cleanup_unverified"

    def _recover_web(self):
        value = _read(self.state)
        if value is None:
            return
        if any(item.status == "pending" for item in value.web_intents.values()):
            with self._writing() as value:
                changed = False
                for item in value.web_intents.values():
                    if self._invalidate_web_pending(value, item):
                        changed = True
                if changed:
                    self._save(value)
        # Pending local approval has no transient secret and survives restart.
        # A consumed claim can never reconstruct or repeat its exchange/import.
        interrupted = [key for key, item in value.web_intents.items() if item.status == "claimed"]
        for identity in interrupted:
            self._web_fail(identity, "github_web_completion_interrupted", unknown=True)
            self._web_cleanup(identity)
        for item in value.connections.values():
            if (item.status != "connected" or item.backend != "native"
                    or item.credential_ref not in value.web_intents):
                continue
            with _guard(self.state / "locks" / f"credential-{item.id}.lock"):
                try:
                    pair = credentials.load(self.state, item.credential_ref, item.credential_store)
                    interrupted = (pair.source != "web" or pair.phase != "ready"
                        or pair.core_binding != self._web_binding(value)
                        or pair.account_id != item.account_id or pair.refresh_expires_at_ms <= now_ms())
                except Fault as error:
                    self._errors[item.id] = error.code
                    interrupted = True
                if interrupted:
                    self._reauthorize(item)

    def web_complete(self, request: dict) -> dict:
        supplied = self._web_request(WebComplete, request)
        identity, reserved = supplied.intent_id, False
        try:
            with self.admit():
                with self._mutex, self._writing() as value:
                    if not self._running:
                        raise Fault("integration_runtime_unavailable", "The selected Core is not accepting authorization.")
                    item = self._web_retained(value, identity)
                    if item.status != "pending" or identity in self._web_active:
                        raise Fault("github_web_intent_consumed", "This intent was consumed; observe its original status.")
                    if (supplied.claim_id in value.attempts or supplied.claim_id in value.web_intents
                            or any(a.claim_id == supplied.claim_id for a in value.web_intents.values())):
                        raise Fault("github_web_claim_consumed", "This claim ID has already been consumed.")
                    self._web_check(value, item)
                    item.status, item.claim_id, item.github_account_id = "claimed", supplied.claim_id, supplied.github_account_id
                    reserved = True
                    self._web_active.add(identity)
                    self._web_idle.clear()
                    self._save(value)  # Consume durably before any token processing or GitHub IO.
                from integrations.github.auth import accept_web_tokens
                pair = accept_web_tokens(supplied.tokens, core_binding=item.request.core_binding)
                actor = self.native.read_actor(pair.access_token)
                if actor["id"] != supplied.github_account_id:
                    raise Fault("github_identity_changed", "GitHub did not verify the claimed account.")
                connection_id = hashlib.sha256(f"github.com:official:{actor['id']}".encode()).hexdigest()[:32]
                with _guard(self.state / "locks" / f"credential-{connection_id}.lock"):
                    with self._writing() as value:
                        item, previous, config = self._web_completion(value, supplied, actor, connection_id)
                        store = item.credential_store
                    pair = pair.model_copy(update={"account_id": actor["id"], "phase": "ready"})
                    credentials.save(self.state, identity, store, pair)
                    with self._writing() as value:
                        item, previous, config = self._web_completion(value, supplied, actor, connection_id)
                        connected = Connection(id=connection_id, provider="github", backend="native", account_id=actor["id"],
                            account_login=actor["login"], authorization_revision=previous.authorization_revision + 1 if previous else 1,
                            status="connected", policy=(previous.policy if previous else config).model_copy(deep=True),
                            verified_at_ms=now_ms(), credential_store=store, credential_ref=identity,
                            refresh_expires_at_ms=pair.refresh_expires_at_ms)
                        value.connections[connection_id] = connected
                        item.status, item.connection_id = "connected", connection_id
                        item.authorization_revision = connected.authorization_revision
                        self._save(value)
                    if previous is not None and previous.credential_ref != identity:
                        self._cleanup(previous)
                result = self.web_status(identity)
                return {key: result[key] for key in ("v", "intent_id", "connection_id", "authorization_revision", "status")}
        except (Fault, OSError) as error:
            code = error.code if isinstance(error, Fault) else "integration_store_unavailable"
            if reserved:
                self._web_fail(identity, code, unknown=code in {
                    "integration_store_unavailable", "integration_store_invalid", "integration_credential_store_unavailable"})
            elif isinstance(error, Fault) and code in {"github_web_intent_expired", "github_web_binding_changed", "integration_connection_changed"}:
                self._web_fail(identity, code)
            raise Fault(code, "Web authorization did not complete; read the original status and authorize again.") from None
        except Exception:
            if reserved:
                self._web_fail(identity, "github_web_completion_interrupted", unknown=True)
            raise Fault("github_web_completion_interrupted", "The original completion is unverified; do not repeat its claim.") from None
        finally:
            if reserved:
                self._web_cleanup(identity)
                with self._mutex:
                    self._web_active.discard(identity)
                    if not self._web_active:
                        self._web_idle.set()

    def _web_completion(self, value: Store, supplied: WebComplete, actor: dict, connection_id: str):
        item = self._web_retained(value, supplied.intent_id)
        config, previous = self._web_check(value, item)
        if (item.status != "claimed" or item.claim_id != supplied.claim_id
                or item.github_account_id != actor["id"]):
            raise Fault("github_web_intent_consumed", "The original completion is no longer active.")
        if (config.account and actor["login"].casefold() != config.account.casefold()
                or previous and (previous.id != connection_id or previous.account_id != actor["id"]
                    or previous.policy.account and actor["login"].casefold() != previous.policy.account.casefold())):
            raise Fault("github_identity_changed", "The authorized account differs from the approved local binding.")
        if previous is None and connection_id in value.connections:
            raise Fault("integration_connection_exists", "Reauthorization needs the original connection and revision.")
        if previous is None and len(value.connections) >= MAX_CONNECTIONS:
            raise Fault("integration_capacity", "The saved account limit was reached.")
        return item, previous, config

    def _policy(self, item: Connection, repository: str, write=False):
        config = self._enabled()
        from integrations.github import connection
        if repository:
            connection.authorize(self.config, repository, write=write)
            connection.authorize(self.config.model_copy(update={"github": item.policy}), repository, write=write)
        if config.account and item.account_login.casefold() != config.account.casefold():
            raise Fault("github_identity_changed", "The account no longer matches configured policy.")
        if item.status != "connected":
            raise Fault("integration_disconnected", "The selected account requires explicit reconnection.")

    def list(self, provider="", connection_id=""):
        if provider not in {"", "github"}:
            raise Fault("integration_unsupported", "Select an installed provider.")
        value = _read(self.state) or Store(format=FORMAT)
        configured = bool(self.config.github and self.native and self.native.CLIENT_ID)
        return {"protocol": PROTOCOL, "providers": [{"provider": "github", "supported": True,
                "enabled": self.config.github is not None, "native_login": configured,
                "methods": ["device_code"] if configured else [],
                "installation_url": self.native.INSTALL_URL if configured else None}],
                "connections": [c.public() for c in value.connections.values() if not connection_id or c.id == connection_id],
                "observed_at_ms": now_ms(), "provider_health": "not_probed",
                "renewal": {"running": self._running, "errors": dict(self._errors)}}

    def _attempt(self, identity: str, value: Store):
        item = value.attempts.get(identity)
        if item is None:
            raise Fault("integration_login_not_found", "No retained login has this ID; absence does not authorize replay.")
        result = item.model_dump(exclude={"intent", "target_revision", "credential_store"})
        if item.status == "pending" and identity not in self._jobs:
            result.update(status="failed", error_code="runtime_interrupted", action=None)
        elif item.status == "pending" and now_ms() >= item.expires_at_ms:
            result.update(status="expired", error_code="integration_login_expired", action=None)
        elif item.status != "pending":
            result["action"] = None
        return {"protocol": PROTOCOL, "login_id": identity, **result}

    def read_login(self, login_id: str):
        return self._attempt(login_id, _read(self.state) or Store(format=FORMAT))

    def connect(self, request_id: str, provider="github", method="device_code", connection_id=""):
        if not re.fullmatch(r"[0-9a-f]{32}", request_id) or connection_id and not re.fullmatch(r"[0-9a-f]{32}", connection_id):
            raise Fault("invalid_integration_request", "Use the original bounded request and connection IDs.")
        intent = dict(provider=provider, method=method, connection_id=connection_id)
        with self._mutex:
            original = _read(self.state)
        if original is not None and request_id in original.attempts:
            if original.attempts[request_id].intent != intent:
                raise Fault("integration_request_conflict", "The request ID belongs to different intent.")
            return self._attempt(request_id, original)
        if original is not None and (request_id in original.web_intents
                or any(a.claim_id == request_id for a in original.web_intents.values())):
            raise Fault("integration_request_conflict", "This original ID belongs to a retained web authorization.")
        config = self._enabled()
        if provider != "github" or method != "device_code":
            raise Fault("integration_unsupported", "New accounts use native GitHub Device Flow only.")
        if self.native is None or not self.native.CLIENT_ID:
            raise Fault("integration_configuration_required",
                        "This build has no official Open Machine GitHub App client ID.")
        with self._mutex, self._writing() as value:
            if request_id in value.attempts:
                if value.attempts[request_id].intent != intent:
                    raise Fault("integration_request_conflict", "The request ID belongs to different intent.")
                return self._attempt(request_id, value)
            if request_id in value.web_intents or any(a.claim_id == request_id for a in value.web_intents.values()):
                raise Fault("integration_request_conflict", "This original ID belongs to a retained web authorization.")
            if not self._running:
                raise Fault("integration_runtime_unavailable", "Start the owning OM runtime before initiating login.")
            if (len(self._jobs) + sum(a.status == "claimed" or a.status == "pending" and a.expires_at_ms > now_ms()
                                      for a in value.web_intents.values()) >= MAX_LOGINS
                    or len(value.attempts) + len(value.web_intents) >= MAX_ATTEMPTS
                    or request_id in value.web_intents):
                raise Fault("integration_capacity", "The bounded login capacity is occupied.")
            target = value.connections.get(connection_id)
            if connection_id and (target is None or target.backend != "native"):
                raise Fault("integration_not_found", "Select an existing native connection for reauthorization.")
            if target and target.status == "connected":
                raise Fault("integration_connection_active", "Do not replace an active connection.")
            store = target.credential_store if target else credentials.select()
            created = now_ms()
            value.attempts[request_id] = Attempt(intent=intent, status="pending", created_at_ms=created,
                expires_at_ms=created + 60000, credential_store=store,
                target_revision=target.authorization_revision if target else None)
            self._save(value)
            event = threading.Event()
            self._jobs[request_id] = (event, None)
        try:
            device = self.native.start_device()
            with self._writing() as value:
                attempt = value.attempts[request_id]
                if attempt.status != "pending":
                    return self._attempt(request_id, value)
                attempt.expires_at_ms = now_ms() + device["expires_in"] * 1000
                attempt.action = DeviceAction(verification_url=device["verification_uri"],
                    installation_url=self.native.INSTALL_URL, user_code=device["user_code"])
                self._save(value)
            with self._mutex:
                if self._stop.is_set():
                    raise Fault("runtime_interrupted", "The runtime stopped during authorization setup.")
                worker = threading.Thread(target=self._poll, args=(request_id, device, event, attempt.expires_at_ms), name="om-github-login", daemon=True)
                self._jobs[request_id] = (event, worker)
                worker.start()
            return self.read_login(request_id)
        except (Fault, OSError) as error:
            self._fail(request_id, error.code if isinstance(error, Fault) else "integration_store_unavailable")
            raise
        finally:
            with self._mutex:
                job = self._jobs.get(request_id)
                if job is not None and job[1] is None:
                    self._jobs.pop(request_id)

    def _fail(self, identity: str, code: str):
        try:
            with self.admit(), self._writing() as value:
                item = value.attempts[identity]
                if item.status == "pending":
                    item.status = "expired" if code == "integration_login_expired" else "failed"
                    item.error_code, item.action = code, None
                    self._save(value)
        except (Fault, OSError, KeyError):
            self._errors[identity] = code

    def _poll(self, identity: str, device: dict, event: threading.Event, expires: int):
        interval = device["interval"]
        try:
            while not event.wait(min(interval, max(0, expires - now_ms()) / 1000)):
                with self.admit():
                    value = _read(self.state)
                    attempt = value.attempts[identity]
                    if attempt.status != "pending":
                        return
                    if now_ms() >= attempt.expires_at_ms:
                        raise Fault("integration_login_expired", "The original device code expired.")
                    try:
                        pair, interval = self.native.poll_device(device["device_code"], interval)
                    except Fault as error:
                        if error.code == "github_network_unavailable":
                            continue
                        raise
                    if pair is not None:
                        self._complete(identity, pair)
                        return
            if self._stop.is_set():
                with self.admit():
                    self._fail(identity, "runtime_interrupted")
        except (Fault, OSError) as error:
            self._fail(identity, error.code if isinstance(error, Fault) else "integration_store_unavailable")
        except Exception:
            self._fail(identity, "integration_internal_error")
        finally:
            try:
                with self.admit():
                    value = _read(self.state)
                    attempt = value.attempts[identity]
                    if not any(c.credential_ref == identity for c in value.connections.values()):
                        credentials.delete(self.state, identity, attempt.credential_store)
            except (Fault, OSError, KeyError):
                self._errors[identity] = "integration_cleanup_unverified"
            with self._mutex:
                self._jobs.pop(identity, None)

    def _pending(self, value: Store, identity: str, account_id: int, connection_id: str):
        attempt = value.attempts[identity]
        if attempt.status != "pending" or now_ms() >= attempt.expires_at_ms:
            raise Fault("integration_login_expired", "The original login is no longer pending.")
        previous = value.connections.get(connection_id)
        target = attempt.intent["connection_id"]
        if target:
            if (target != connection_id or previous is None or previous.account_id != account_id
                    or previous.authorization_revision != attempt.target_revision):
                raise Fault("github_identity_changed", "Reauthorization must retain the original account and revision.")
        elif previous is not None:
            raise Fault("integration_connection_exists", "Use the saved connection ID for reauthorization.")
        if previous is None and len(value.connections) >= MAX_CONNECTIONS:
            raise Fault("integration_capacity", "The saved account limit was reached.")
        return attempt, previous

    def _complete(self, identity: str, pair):
        actor = self.native.read_actor(pair.access_token)
        config = self._enabled()
        if config.account and actor["login"].casefold() != config.account.casefold():
            raise Fault("github_identity_changed", "The authorized account differs from operator policy.")
        connection_id = hashlib.sha256(f"github.com:official:{actor['id']}".encode()).hexdigest()[:32]
        with _guard(self.state / "locks" / f"credential-{connection_id}.lock"):
            with self._writing() as value:
                attempt, previous = self._pending(value, identity, actor["id"], connection_id)
                store = attempt.credential_store
            pair = pair.model_copy(update={"account_id": actor["id"], "phase": "ready"})
            # Unique login slots prevent a stale completion overwriting new secrets.
            credentials.save(self.state, identity, store, pair)
            with self._writing() as value:
                attempt, previous = self._pending(value, identity, actor["id"], connection_id)
                item = Connection(id=connection_id, provider="github", backend="native", account_id=actor["id"],
                    account_login=actor["login"], authorization_revision=previous.authorization_revision + 1 if previous else 1,
                    status="connected", policy=config.model_copy(deep=True),
                    verified_at_ms=now_ms(), credential_store=store, credential_ref=identity,
                    refresh_expires_at_ms=pair.refresh_expires_at_ms)
                value.connections[connection_id] = item
                attempt.status, attempt.connection_id, attempt.action = "succeeded", connection_id, None
                self._save(value)
            if previous is not None and previous.credential_ref != identity:
                self._cleanup(previous)

    def cancel_login(self, login_id: str):
        with self._writing() as value:
            item = value.attempts.get(login_id)
            if item is None:
                raise Fault("integration_login_not_found", "No retained login has this ID.")
            if item.status == "pending":
                item.status, item.error_code, item.action = "cancelled", None, None
                self._save(value)
        with self._mutex:
            if login_id in self._jobs:
                self._jobs[login_id][0].set()
        return self.read_login(login_id)

    def _cleanup(self, item: Connection):
        try:
            result = credentials.delete(self.state, item.credential_ref, item.credential_store)
            self._errors.pop(item.id, None)
            return result, None
        except Fault as error:
            self._errors[item.id] = error.code
            return "failed", error.code

    def disconnect(self, connection_id: str, expected_revision: int):
        if not re.fullmatch(r"[0-9a-f]{32}", connection_id) or type(expected_revision) is not int or expected_revision < 1:
            raise Fault("invalid_integration_request", "Use the observed connection ID and revision.")
        with _guard(self.state / "locks" / f"credential-{connection_id}.lock"):
            with self._writing() as value:
                item = value.connections.get(connection_id)
                if item is None:
                    raise Fault("integration_not_found", "The original connection is unavailable.")
                pending = [(identity, attempt) for identity, attempt in value.attempts.items()
                           if attempt.status == "pending" and attempt.intent["connection_id"] == connection_id]
                web_pending = [attempt for attempt in value.web_intents.values()
                               if attempt.status in {"pending", "claimed"}
                               and attempt.request.expected_connection_id == connection_id]
                if item.status != "disconnected" or pending or web_pending:
                    if item.authorization_revision != expected_revision:
                        raise Fault("integration_connection_changed", "Read the current revision before disconnecting.")
                    item.status = "disconnected"
                    item.authorization_revision += 1
                    for _, attempt in pending:
                        attempt.status, attempt.action = "cancelled", None
                    for attempt in web_pending:
                        attempt.status, attempt.error_code = "cancelled", None
                    self._save(value)
            with self._mutex:
                for identity, _ in pending:
                    job = self._jobs.get(identity)
                    if job is not None:
                        job[0].set()
            cleanup, error = self._cleanup(item) if item.backend == "native" else ("externally_managed", None)
            return {"protocol": PROTOCOL, "connection": item.public(), "credential_cleanup": cleanup,
                    "cleanup_error": error, "remote_revocation": "not_requested"}

    def _reauthorize(self, item: Connection):
        with self._writing() as value:
            current = value.connections[item.id]
            if current.binding() == item.binding() and current.status == "connected":
                current.status = "reauthorization_required"
                current.authorization_revision += 1
                self._save(value)

    def _token(self, item: Connection, *, force=False, renew=False):
        try:
            current = credentials.load(self.state, item.credential_ref, item.credential_store)
        except Fault as error:
            if error.code in {"integration_credential_missing", "integration_credential_invalid"}:
                self._reauthorize(item)
            raise
        if current.account_id != item.account_id:
            self._reauthorize(item)
            raise Fault("integration_credential_invalid", "Credential identity differs from the selected connection.")
        if current.source == "web":
            return self._web_token(item, current, force=force)
        if (current.phase == "refreshing" or current.refresh_expires_at_ms <= now_ms()
                or current.phase == "unverified" and current.access_expires_at_ms <= now_ms()):
            self._reauthorize(item)
            raise Fault("github_reauthorization_required", "An expired or interrupted rotating grant requires reauthorization.")
        needed = force or current.access_expires_at_ms <= now_ms() + 300000 or renew and current.refresh_expires_at_ms <= now_ms() + RENEW_WINDOW_MS
        if current.phase == "ready" and needed:
            credentials.save(self.state, item.credential_ref, item.credential_store, current.model_copy(update={"phase": "refreshing"}))
            try:
                replacement = self.native.refresh(current.refresh_token)
            except Fault as error:
                if error.code in {"github_network_unavailable", "github_rate_limited"}:
                    credentials.save(self.state, item.credential_ref, item.credential_store, current)
                else:
                    self._reauthorize(item)
                raise
            current = replacement.model_copy(update={"account_id": item.account_id, "phase": "unverified"})
            credentials.save(self.state, item.credential_ref, item.credential_store, current)
        if current.phase == "unverified":
            actor = self.native.read_actor(current.access_token)
            if actor["id"] != item.account_id:
                self._reauthorize(item)
                raise Fault("github_identity_changed", "The refreshed grant belongs to another account.")
            current = current.model_copy(update={"phase": "ready"})
            credentials.save(self.state, item.credential_ref, item.credential_store, current)
        if current.refresh_expires_at_ms != item.refresh_expires_at_ms:
            with self._writing() as value:
                retained = value.connections[item.id]
                if retained.binding() != item.binding() or retained.status != "connected":
                    raise Fault("integration_connection_changed", "Authorization changed during renewal.")
                retained.refresh_expires_at_ms, retained.verified_at_ms = current.refresh_expires_at_ms, now_ms()
                self._save(value)
        return current

    def _web_token(self, item: Connection, current, *, force=False):
        from integrations.github import web_broker
        configuration = self.config.configuration_sha256()
        value = _read(self.state)
        if (value is None or current.core_binding != self._web_binding(value)
                or current.phase != "ready" or current.refresh_expires_at_ms <= now_ms()):
            self._reauthorize(item)
            raise Fault("github_reauthorization_required", "An interrupted, expired or differently bound web grant requires authorization.")
        # Cloud is not required while this locally held access token is usable.
        # In particular, idle maintenance must not rotate an unexpired web token.
        if not force and current.access_expires_at_ms > now_ms():
            return current
        refresh_id = secrets.token_hex(16)
        try:
            if (refresh_id == current.refresh_id or refresh_id in value.attempts or refresh_id in value.web_intents
                    or any(a.claim_id == refresh_id for a in value.web_intents.values())):
                raise Fault("github_web_refresh_conflict", "The new refresh ID conflicts with retained original work.")
            credentials.save(self.state, item.credential_ref, item.credential_store,
                current.model_copy(update={"phase": "refreshing", "refresh_id": refresh_id}))
            replacement = web_broker.refresh(self.state, refresh_id=refresh_id,
                connection_id=item.id, github_account_id=item.account_id,
                core_binding=current.core_binding, expected_authorization_revision=item.authorization_revision,
                refresh_token=current.refresh_token)
            if (replacement.source != "web" or replacement.core_binding != current.core_binding
                    or replacement.refresh_id != refresh_id or replacement.refresh_token == current.refresh_token):
                raise Fault("github_reauthorization_required", "The original broker rotation could not be verified.")
            replacement = replacement.model_copy(update={"account_id": item.account_id, "phase": "unverified"})
            credentials.save(self.state, item.credential_ref, item.credential_store, replacement)
            actor = self.native.read_actor(replacement.access_token)
            config = self._enabled()
            if (actor["id"] != item.account_id
                    or config.account and actor["login"].casefold() != config.account.casefold()
                    or item.policy.account and actor["login"].casefold() != item.policy.account.casefold()):
                raise Fault("github_identity_changed", "The rotated web grant belongs to another account.")
            with self._writing() as value:
                retained = value.connections[item.id]
                if (retained.binding() != item.binding() or retained.status != "connected"
                        or replacement.core_binding != self._web_binding(value)
                        or configuration != self.config.configuration_sha256()):
                    raise Fault("integration_connection_changed", "The approved connection changed during web rotation.")
                self._policy(retained, "")
                retained.refresh_expires_at_ms, retained.verified_at_ms = replacement.refresh_expires_at_ms, now_ms()
                self._save(value)
            replacement = replacement.model_copy(update={"phase": "ready"})
            credentials.save(self.state, item.credential_ref, item.credential_store, replacement)
            return replacement
        except (Fault, OSError):
            # Dispatch may have consumed the rotating grant. Neither credential
            # restoration nor a second exchange is safe, even for Cloud outages.
            self._reauthorize(item)
            raise Fault("github_reauthorization_required", "Web token renewal failed; authorize the same account again.") from None
        except Exception:
            self._reauthorize(item)
            raise Fault("github_reauthorization_required", "Web token renewal is unverified; do not repeat its original exchange.") from None

    def _maintain(self):
        while not self._stop.is_set():
            try:
                value = _read(self.state)
                items = [] if value is None else list(value.connections.values())
                for item in items:
                    if self._stop.is_set():
                        break
                    if item.backend != "native" or item.status != "connected" or item.refresh_expires_at_ms > now_ms() + RENEW_WINDOW_MS:
                        continue
                    try:
                        with self.admit(), _guard(self.state / "locks" / f"credential-{item.id}.lock"):
                            selected = self.select(connection_id=item.id, binding=item.binding())
                            self._token(selected, renew=True)
                        self._errors.pop(item.id, None)
                    except Fault as error:
                        self._errors[item.id] = error.code
            except (Fault, OSError) as error:
                self._errors["store"] = error.code if isinstance(error, Fault) else "integration_store_unavailable"
            self._stop.wait(MAINTENANCE_SECONDS)

    def select(self, repository="", *, connection_id="", binding=None, write=False, historical_unbound=False):
        config = self._enabled()
        from integrations.github import connection
        if historical_unbound:
            raise Fault("integration_legacy_connection", "The gh backend for unbound historical GitHub tasks is retired. "
                        "Retain original outcomes; use native connections for separately authorized work.")
        if repository:
            connection.authorize(self.config, repository, write=write)
        value = _read(self.state) or Store(format=FORMAT)
        if binding and connection_id and connection_id != binding["connection_id"]:
            raise Fault("integration_connection_changed", "The task is bound to another connection.")
        selected = binding["connection_id"] if binding else connection_id
        if selected:
            item = value.connections.get(selected)
            if item is None:
                raise Fault("integration_not_found", "The original connection is unavailable; no fallback was selected.")
            if item.backend == "gh":
                raise Fault("integration_legacy_connection", "The gh backend is retired; its connection and task history remain readable. "
                            "Use native connections for separately authorized work; do not replay original effects.")
        else:
            candidates = [c for c in value.connections.values() if c.backend == "native" and c.status == "connected"
                          and (not config.account or c.account_login.casefold() == config.account.casefold())]
            if len(candidates) > 1:
                raise Fault("integration_selection_required", "Select one of the saved native accounts.")
            if not candidates:
                raise Fault("integration_authentication_required", "Authorize native GitHub before starting new work.")
            item = candidates[0]
        self._policy(item, repository, write)
        if binding and item.binding() != binding:
            raise Fault("integration_connection_changed", "The original authorization revision changed.")
        return item

    def request(self, repository="", *, connection_id="", binding=None, write=False, historical_unbound=False):
        item = self.select(repository, connection_id=connection_id, binding=binding, write=write, historical_unbound=historical_unbound)
        selected = item.binding()

        def send(method, endpoint, payload=None):
            preparing, request_error = True, None
            try:
                with self.admit(), _guard(self.state / "locks" / f"credential-{item.id}.lock"):
                    retained = self.select(repository, binding=selected, write=write)
                    for force in (False, True):
                        current = self._token(retained, force=force)
                        preparing, request_error = False, None
                        try:
                            result = self.native.request(current.access_token, method, endpoint, payload)
                        except Fault as error:
                            request_error = error
                            if force or error.code != "github_authentication_required" or error.effect == "effect_unknown":
                                raise
                            # A definite first 401 rejected the business request. A
                            # refresh failure cannot make that request take effect.
                            preparing = True
                        else:
                            return result
            except Fault as error:
                if preparing:
                    error.effect = "known_no_effect"
                elif error is not request_error:
                    # Admission/lock cleanup after dispatch is not a provider rejection.
                    error.effect = "effect_unknown"
                raise
            except OSError as error:
                if not preparing:
                    raise
                raise Fault("integration_store_unavailable",
                            "GitHub request preparation could not complete; no business mutation took effect.",
                            effect="known_no_effect") from error
        return send
