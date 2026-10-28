"""One runtime owner for account bindings, native login and credential renewal."""

from contextlib import contextmanager, nullcontext
import hashlib
from pathlib import Path
import re
import threading

from integrations.connection_state import (
    Attempt, Connection, ConnectionStore, DeviceAction, FORMAT, MAX_ATTEMPTS,
    MAX_CONNECTIONS, MAX_LOGINS, Store, _guard, now_ms,
)
import integrations.credentials as credentials
from integrations.github.web_authorization import WebAuthorization
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory

PROTOCOL = 2
RENEW_WINDOW_MS = 30 * 86400 * 1000
MAINTENANCE_SECONDS = 6 * 3600


class Connections:
    def __init__(self, config: Settings, state: Path, native=None):
        self.config, self.state, self.native = config, state, native
        self.admit = nullcontext
        self._jobs: dict[str, tuple[threading.Event, threading.Thread | None]] = {}
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._running = False
        self._errors: dict[str, str] = {}
        self._store = ConnectionStore(state)
        self._web = WebAuthorization(self)

    @contextmanager
    def running(self):
        private_directory(self.state)
        private_directory(self.state / "locks")
        with lease(self.state / "locks/connections-owner.lock"):
            self._web.recover()
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
                self._web.idle.wait()

    def _enabled(self):
        if self.config.github is None or self.native is None:
            raise Fault("github_not_allowed", "GitHub is not enabled by the operator.")
        return self.config.github

    def web_identity(self) -> dict:
        return self._web.identity()

    def web_begin(self, request: dict) -> dict:
        return self._web.begin(request)

    def web_status(self, intent_id: str) -> dict:
        return self._web.status(intent_id)

    def web_cancel(self, request: dict) -> dict:
        return self._web.cancel(request)

    def web_complete(self, request: dict) -> dict:
        return self._web.complete(request)

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
        value = self._store.read() or Store(format=FORMAT)
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
        return self._attempt(login_id, self._store.read() or Store(format=FORMAT))

    def connect(self, request_id: str, provider="github", method="device_code", connection_id=""):
        if not re.fullmatch(r"[0-9a-f]{32}", request_id) or connection_id and not re.fullmatch(r"[0-9a-f]{32}", connection_id):
            raise Fault("invalid_integration_request", "Use the original bounded request and connection IDs.")
        intent = dict(provider=provider, method=method, connection_id=connection_id)
        with self._mutex:
            original = self._store.read()
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
        with self._mutex, self._store.writing() as value:
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
            self._store.save(value)
            event = threading.Event()
            self._jobs[request_id] = (event, None)
        try:
            device = self.native.start_device()
            with self._store.writing() as value:
                attempt = value.attempts[request_id]
                if attempt.status != "pending":
                    return self._attempt(request_id, value)
                attempt.expires_at_ms = now_ms() + device["expires_in"] * 1000
                attempt.action = DeviceAction(verification_url=device["verification_uri"],
                    installation_url=self.native.INSTALL_URL, user_code=device["user_code"])
                self._store.save(value)
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
            with self.admit(), self._store.writing() as value:
                item = value.attempts[identity]
                if item.status == "pending":
                    item.status = "expired" if code == "integration_login_expired" else "failed"
                    item.error_code, item.action = code, None
                    self._store.save(value)
        except (Fault, OSError, KeyError):
            self._errors[identity] = code

    def _poll(self, identity: str, device: dict, event: threading.Event, expires: int):
        interval = device["interval"]
        try:
            while not event.wait(min(interval, max(0, expires - now_ms()) / 1000)):
                with self.admit():
                    value = self._store.read()
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
                    value = self._store.read()
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
            with self._store.writing() as value:
                attempt, previous = self._pending(value, identity, actor["id"], connection_id)
                store = attempt.credential_store
            pair = pair.model_copy(update={"account_id": actor["id"], "phase": "ready"})
            # Unique login slots prevent a stale completion overwriting new secrets.
            credentials.save(self.state, identity, store, pair)
            with self._store.writing() as value:
                attempt, previous = self._pending(value, identity, actor["id"], connection_id)
                item = Connection(id=connection_id, provider="github", backend="native", account_id=actor["id"],
                    account_login=actor["login"], authorization_revision=previous.authorization_revision + 1 if previous else 1,
                    status="connected", policy=config.model_copy(deep=True),
                    verified_at_ms=now_ms(), credential_store=store, credential_ref=identity,
                    refresh_expires_at_ms=pair.refresh_expires_at_ms)
                value.connections[connection_id] = item
                attempt.status, attempt.connection_id, attempt.action = "succeeded", connection_id, None
                self._store.save(value)
            if previous is not None and previous.credential_ref != identity:
                self._cleanup(previous)

    def cancel_login(self, login_id: str):
        with self._store.writing() as value:
            item = value.attempts.get(login_id)
            if item is None:
                raise Fault("integration_login_not_found", "No retained login has this ID.")
            if item.status == "pending":
                item.status, item.error_code, item.action = "cancelled", None, None
                self._store.save(value)
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
            with self._store.writing() as value:
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
                    self._store.save(value)
            with self._mutex:
                for identity, _ in pending:
                    job = self._jobs.get(identity)
                    if job is not None:
                        job[0].set()
            cleanup, error = self._cleanup(item) if item.backend == "native" else ("externally_managed", None)
            return {"protocol": PROTOCOL, "connection": item.public(), "credential_cleanup": cleanup,
                    "cleanup_error": error, "remote_revocation": "not_requested"}

    def _reauthorize(self, item: Connection):
        with self._store.writing() as value:
            current = value.connections[item.id]
            if current.binding() == item.binding() and current.status == "connected":
                current.status = "reauthorization_required"
                current.authorization_revision += 1
                self._store.save(value)

    def _token(self, item: Connection, *, force=False, renew=False):
        try:
            current = credentials.load(self.state, item.credential_ref, item.credential_store)
        except Fault as error:
            if error.code in {"integration_credential_missing", "integration_credential_invalid"}:
                self._reauthorize(item)
            raise
        if current.source != "web":
            value = self._store.read()
            if value is None:
                raise Fault("integration_store_invalid", "The original credential owner is unavailable.")
            if item.credential_ref in value.web_intents:
                self._errors[item.id] = "integration_credential_invalid"
                self._reauthorize(item)
                raise Fault("integration_credential_invalid", "Credential source differs from its original web authorization.")
        if current.source == "web" and self._errors.get(item.id) == "integration_credential_store_unavailable":
            self._errors.pop(item.id, None)
        if current.account_id != item.account_id:
            self._reauthorize(item)
            raise Fault("integration_credential_invalid", "Credential identity differs from the selected connection.")
        if current.source == "web":
            return self._web.token(item, current, force=force)
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
            with self._store.writing() as value:
                retained = value.connections[item.id]
                if retained.binding() != item.binding() or retained.status != "connected":
                    raise Fault("integration_connection_changed", "Authorization changed during renewal.")
                retained.refresh_expires_at_ms, retained.verified_at_ms = current.refresh_expires_at_ms, now_ms()
                self._store.save(value)
        return current

    def _maintain(self):
        while not self._stop.is_set():
            try:
                value = self._store.read()
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
        value = self._store.read() or Store(format=FORMAT)
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
