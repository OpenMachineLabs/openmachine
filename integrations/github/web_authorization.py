"""GitHub Web authorization using the runtime owner's state and account locks."""

import hashlib
import json
import re
import secrets
import threading

from integrations.connection_state import (
    Connection, MAX_ATTEMPTS, MAX_CONNECTIONS, MAX_LOGINS, Store, WEB_INTENT_MS,
    WebBegin, WebCancel, WebComplete, WebIntent, _guard, _private, now_ms,
)
import integrations.credentials as credentials
from openmachine.foundation.sanitize import Fault


class WebAuthorization:
    def __init__(self, owner):
        self.owner = owner
        self._active: set[str] = set()
        self.idle = threading.Event()
        self.idle.set()

    @staticmethod
    def _request(model, request):
        try:
            if not isinstance(request, dict) or type(request.get("v")) is not int or request["v"] != 1:
                raise ValueError("invalid version")
            return model.model_validate(request)
        except (ValueError, TypeError):
            raise Fault("invalid_github_web_request", "Use the exact version-1 private authorization schema.") from None

    def _binding(self, value: Store) -> str:
        _private(self.owner.state, directory=True)
        info = self.owner.state.lstat()
        if value.web_core_id is None:
            raise Fault("github_web_binding_required", "Read this Core's private identity before beginning authorization.")
        # The opaque identifier is owned by this existing store, never by a caller.
        # The local directory identity also rejects copied or replaced Core state.
        raw = [value.web_core_id, str(self.owner.state.resolve()), info.st_dev, info.st_ino, info.st_uid]
        return hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest()

    def identity(self) -> dict:
        with self.owner.admit(), self.owner._mutex, self.owner._store.writing() as value:
            self.owner._enabled()
            if not self.owner._running:
                raise Fault("integration_runtime_unavailable", "Start the selected Core before local authorization.")
            if value.web_core_id is None:
                value.web_core_id = secrets.token_hex(16)
                self.owner._store.save(value)
            return {"v": 1, "core_binding": self._binding(value)}

    def _retained(self, value: Store | None, identity: str) -> WebIntent:
        item = value.web_intents.get(identity) if value is not None else None
        if item is None:
            raise Fault("github_web_intent_not_found", "No original web intent has this ID; do not replay it.")
        return item

    def _check(self, value: Store, item: WebIntent):
        config = self.owner._enabled()
        if (item.request.core_binding != self._binding(value)
                or item.configuration_sha256 != self.owner.config.configuration_sha256()):
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

    def _status(self, item: WebIntent) -> dict:
        result = {"v": 1, "intent_id": item.request.intent_id,
                  "core_binding": item.request.core_binding, "expires_at_ms": item.expires_at_ms,
                  "status": item.status}
        if item.error_code is not None:
            result["error_code"] = item.error_code
        if item.status == "connected":
            result.update(connection_id=item.connection_id, authorization_revision=item.authorization_revision)
        return result

    def _invalidate_pending(self, value: Store, item: WebIntent) -> bool:
        if item.status != "pending":
            return False
        try:
            self._check(value, item)
        except Fault as error:
            if error.code not in {"github_not_allowed", "github_web_binding_changed",
                                  "integration_connection_changed", "github_web_intent_expired"}:
                raise
            item.status = "expired" if error.code == "github_web_intent_expired" else "rejected"
            item.error_code = error.code
            return True
        return False

    def begin(self, request: dict) -> dict:
        supplied = self._request(WebBegin, request)
        with self.owner.admit(), self.owner._mutex, self.owner._store.writing() as value:
            if not self.owner._running:
                raise Fault("integration_runtime_unavailable", "Start the selected Core before local authorization.")
            retained = value.web_intents.get(supplied.intent_id)
            if retained is not None:
                if retained.request != supplied:
                    raise Fault("integration_request_conflict", "This original ID belongs to another intent.")
                if self._invalidate_pending(value, retained):
                    self.owner._store.save(value)
                    raise Fault(retained.error_code, "The original web authorization is no longer valid; authorize again.")
                if retained.status != "pending":
                    raise Fault("github_web_intent_consumed", "Observe the original status; use new authorization to retry.")
                return self._status(retained)
            self.owner._enabled()
            if supplied.core_binding != self._binding(value):
                raise Fault("github_web_binding_changed", "Select the original locally verified Core identity.")
            if (supplied.intent_id in value.attempts
                    or any(a.claim_id == supplied.intent_id for a in value.web_intents.values())):
                raise Fault("integration_request_conflict", "This original ID already belongs to another login.")
            active = sum(a.status == "pending" and a.expires_at_ms > now_ms() or a.status == "claimed"
                         for a in value.web_intents.values())
            if len(value.attempts) + len(value.web_intents) >= MAX_ATTEMPTS or active + len(self.owner._jobs) >= MAX_LOGINS:
                raise Fault("integration_capacity", "The bounded original authorization history is full.")
            previous = value.connections.get(supplied.expected_connection_id)
            created = now_ms()
            item = WebIntent(request=supplied, configuration_sha256=self.owner.config.configuration_sha256(),
                credential_store=previous.credential_store if previous and previous.backend == "native" else credentials.select(),
                created_at_ms=created, expires_at_ms=created + WEB_INTENT_MS, status="pending")
            self._check(value, item)
            value.web_intents[supplied.intent_id] = item
            self.owner._store.save(value)
            return self._status(item)

    def status(self, intent_id: str) -> dict:
        if not isinstance(intent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", intent_id):
            raise Fault("invalid_github_web_request", "Use the original web intent ID.")
        with self.owner.admit():
            item = self._retained(self.owner._store.read(), intent_id)
            if item.status == "pending":
                with self.owner._store.writing() as value:
                    item = self._retained(value, intent_id)
                    if self._invalidate_pending(value, item):
                        self.owner._store.save(value)
            return self._status(item)

    def cancel(self, request: dict) -> dict:
        supplied = self._request(WebCancel, request)
        with self.owner.admit(), self.owner._store.writing() as value:
            item = self._retained(value, supplied.intent_id)
            if item.status in {"pending", "claimed"}:
                item.status, item.error_code = "cancelled", None
                self.owner._store.save(value)
            return self._status(item)

    def _fail(self, identity: str, code: str, *, unknown=False):
        try:
            with self.owner._store.writing() as value:
                item = self._retained(value, identity)
                if item.status in {"pending", "claimed"}:
                    item.status = "outcome_unknown" if unknown and item.claim_id else (
                        "expired" if code == "github_web_intent_expired" else "rejected")
                    item.error_code = code
                    self.owner._store.save(value)
        except (Fault, OSError):
            self.owner._errors[identity] = "github_web_outcome_unknown"

    def _cleanup(self, identity: str):
        try:
            value = self.owner._store.read()
            item = self._retained(value, identity)
            if not any(c.credential_ref == identity for c in value.connections.values()):
                credentials.delete(self.owner.state, identity, item.credential_store)
        except (Fault, OSError):
            self.owner._errors[identity] = "integration_cleanup_unverified"

    def recover(self):
        value = self.owner._store.read()
        if value is None:
            return
        if any(item.status == "pending" for item in value.web_intents.values()):
            with self.owner._store.writing() as value:
                changed = False
                for item in value.web_intents.values():
                    if self._invalidate_pending(value, item):
                        changed = True
                if changed:
                    self.owner._store.save(value)
        # Pending local approval has no transient secret and survives restart.
        # A consumed claim can never reconstruct or repeat its exchange/import.
        interrupted = [key for key, item in value.web_intents.items() if item.status == "claimed"]
        for identity in interrupted:
            self._fail(identity, "github_web_completion_interrupted", unknown=True)
            self._cleanup(identity)
        for item in value.connections.values():
            if (item.status != "connected" or item.backend != "native"
                    or item.credential_ref not in value.web_intents):
                continue
            with _guard(self.owner.state / "locks" / f"credential-{item.id}.lock"):
                try:
                    pair = credentials.load(self.owner.state, item.credential_ref, item.credential_store)
                    interrupted = (pair.source != "web" or pair.phase != "ready"
                        or pair.core_binding != self._binding(value)
                        or pair.account_id != item.account_id or pair.refresh_expires_at_ms <= now_ms())
                except Fault as error:
                    self.owner._errors[item.id] = error.code
                    if error.code == "integration_credential_store_unavailable":
                        # A locked store cannot prove interruption or revoke an account.
                        continue
                    interrupted = True
                else:
                    if self.owner._errors.get(item.id) == "integration_credential_store_unavailable":
                        self.owner._errors.pop(item.id, None)
                if interrupted:
                    self.owner._reauthorize(item)

    def complete(self, request: dict) -> dict:
        supplied = self._request(WebComplete, request)
        identity, reserved = supplied.intent_id, False
        try:
            with self.owner.admit():
                with self.owner._mutex, self.owner._store.writing() as value:
                    if not self.owner._running:
                        raise Fault("integration_runtime_unavailable", "The selected Core is not accepting authorization.")
                    item = self._retained(value, identity)
                    if item.status != "pending" or identity in self._active:
                        raise Fault("github_web_intent_consumed", "This intent was consumed; observe its original status.")
                    if (supplied.claim_id in value.attempts or supplied.claim_id in value.web_intents
                            or any(a.claim_id == supplied.claim_id for a in value.web_intents.values())):
                        raise Fault("github_web_claim_consumed", "This claim ID has already been consumed.")
                    self._check(value, item)
                    item.status, item.claim_id, item.github_account_id = "claimed", supplied.claim_id, supplied.github_account_id
                    reserved = True
                    self._active.add(identity)
                    self.idle.clear()
                    self.owner._store.save(value)  # Consume durably before any token processing or GitHub IO.
                from integrations.github.auth import accept_web_tokens
                pair = accept_web_tokens(supplied.tokens, core_binding=item.request.core_binding)
                actor = self.owner.native.read_actor(pair.access_token)
                if actor["id"] != supplied.github_account_id:
                    raise Fault("github_identity_changed", "GitHub did not verify the claimed account.")
                connection_id = hashlib.sha256(f"github.com:official:{actor['id']}".encode()).hexdigest()[:32]
                with _guard(self.owner.state / "locks" / f"credential-{connection_id}.lock"):
                    with self.owner._store.writing() as value:
                        item, previous, config = self._completion(value, supplied, actor, connection_id)
                        store = item.credential_store
                    pair = pair.model_copy(update={"account_id": actor["id"], "phase": "ready"})
                    credentials.save(self.owner.state, identity, store, pair)
                    with self.owner._store.writing() as value:
                        item, previous, config = self._completion(value, supplied, actor, connection_id)
                        connected = Connection(id=connection_id, provider="github", backend="native", account_id=actor["id"],
                            account_login=actor["login"], authorization_revision=previous.authorization_revision + 1 if previous else 1,
                            status="connected", policy=(previous.policy if previous else config).model_copy(deep=True),
                            verified_at_ms=now_ms(), credential_store=store, credential_ref=identity,
                            refresh_expires_at_ms=pair.refresh_expires_at_ms)
                        value.connections[connection_id] = connected
                        item.status, item.connection_id = "connected", connection_id
                        item.authorization_revision = connected.authorization_revision
                        self.owner._store.save(value)
                    if previous is not None and previous.credential_ref != identity:
                        self.owner._cleanup(previous)
                result = self.status(identity)
                return {key: result[key] for key in ("v", "intent_id", "connection_id", "authorization_revision", "status")}
        except (Fault, OSError) as error:
            code = error.code if isinstance(error, Fault) else "integration_store_unavailable"
            if reserved:
                self._fail(identity, code, unknown=code in {
                    "integration_store_unavailable", "integration_store_invalid", "integration_credential_store_unavailable"})
            elif isinstance(error, Fault) and code in {"github_web_intent_expired", "github_web_binding_changed", "integration_connection_changed"}:
                self._fail(identity, code)
            raise Fault(code, "Web authorization did not complete; read the original status and authorize again.") from None
        except Exception:
            if reserved:
                self._fail(identity, "github_web_completion_interrupted", unknown=True)
            raise Fault("github_web_completion_interrupted", "The original completion is unverified; do not repeat its claim.") from None
        finally:
            if reserved:
                self._cleanup(identity)
                with self.owner._mutex:
                    self._active.discard(identity)
                    if not self._active:
                        self.idle.set()

    def _completion(self, value: Store, supplied: WebComplete, actor: dict, connection_id: str):
        item = self._retained(value, supplied.intent_id)
        config, previous = self._check(value, item)
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

    def token(self, item: Connection, current, *, force=False):
        from integrations.github import web_broker
        configuration = self.owner.config.configuration_sha256()
        value = self.owner._store.read()
        if (value is None or current.core_binding != self._binding(value)
                or current.phase != "ready" or current.refresh_expires_at_ms <= now_ms()):
            self.owner._reauthorize(item)
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
            credentials.save(self.owner.state, item.credential_ref, item.credential_store,
                current.model_copy(update={"phase": "refreshing", "refresh_id": refresh_id}))
            replacement = web_broker.refresh(self.owner.state, refresh_id=refresh_id,
                connection_id=item.id, github_account_id=item.account_id,
                core_binding=current.core_binding, expected_authorization_revision=item.authorization_revision,
                refresh_token=current.refresh_token)
            if (replacement.source != "web" or replacement.core_binding != current.core_binding
                    or replacement.refresh_id != refresh_id or replacement.refresh_token == current.refresh_token):
                raise Fault("github_reauthorization_required", "The original broker rotation could not be verified.")
            replacement = replacement.model_copy(update={"account_id": item.account_id, "phase": "unverified"})
            credentials.save(self.owner.state, item.credential_ref, item.credential_store, replacement)
            actor = self.owner.native.read_actor(replacement.access_token)
            config = self.owner._enabled()
            if (actor["id"] != item.account_id
                    or config.account and actor["login"].casefold() != config.account.casefold()
                    or item.policy.account and actor["login"].casefold() != item.policy.account.casefold()):
                raise Fault("github_identity_changed", "The rotated web grant belongs to another account.")
            with self.owner._store.writing() as value:
                retained = value.connections[item.id]
                if (retained.binding() != item.binding() or retained.status != "connected"
                        or replacement.core_binding != self._binding(value)
                        or configuration != self.owner.config.configuration_sha256()):
                    raise Fault("integration_connection_changed", "The approved connection changed during web rotation.")
                self.owner._policy(retained, "")
                retained.refresh_expires_at_ms, retained.verified_at_ms = replacement.refresh_expires_at_ms, now_ms()
                self.owner._store.save(value)
            replacement = replacement.model_copy(update={"phase": "ready"})
            credentials.save(self.owner.state, item.credential_ref, item.credential_store, replacement)
            return replacement
        except (Fault, OSError):
            # Dispatch may have consumed the rotating grant. Neither credential
            # restoration nor a second exchange is safe, even for Cloud outages.
            self.owner._reauthorize(item)
            raise Fault("github_reauthorization_required", "Web token renewal failed; authorize the same account again.") from None
        except Exception:
            self.owner._reauthorize(item)
            raise Fault("github_reauthorization_required", "Web token renewal is unverified; do not repeat its original exchange.") from None
