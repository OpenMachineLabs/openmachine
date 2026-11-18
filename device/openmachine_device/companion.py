"""Typed companion operations retain device identity and original effects."""

import re
import secrets
import socket
import sys
import time

from . import remote_access
from .errors import ConnectorError
from .cloud import KEY_REFUSALS, Cloud, identifier, unpair
from .store import (Identity, Store, device_record, digest, disable, hardware_id, key_file, key_fingerprint, revocation_record,
                    revoking)

REDUCTIONS = {"sign-out", "cancel-login", "unpair"}

ACTIONS = {
    "snapshot", "connection", "operation", "login", "cancel-login", "sign-out", "policy", "remote-access",
    "unpair", "continue-revocation", "move",
}
# A move's sign-in is approved by its request or never: this long bounds how late a lost request can still arrive.
MOVE_SECONDS = 60


def configuration(config: dict) -> str:
    """What a sign-in's connect decision compares: the configuration apart from its devices and enabled state,
    which the sign-in itself changes."""
    return digest({key: value for key, value in config.items() if key not in {"device", "devices", "revoked", "enabled"}})


class Companion:
    def __init__(self, store: Store):
        self.store = store
        self.config = store.read("config.json")
        self.cloud = Cloud(self.config["server"])

    def binding(self) -> str:
        config = self.store.read("config.json")
        device = config.get("device") or {}
        return digest({"server": config["server"], "fingerprint": device.get("key_fingerprint"),
                       "device_id": device.get("device_id")})

    @staticmethod
    def receipt(device: dict, principal: str, fingerprint: str, *, enrollment: bool = False) -> dict:
        """Accept only the signed session's exact account/key/device receipt."""
        if (not isinstance(device, dict) or device.get("principal_id") != principal
                or device.get("key_fingerprint") != fingerprint):
            raise ConnectorError("account_binding_changed", "This sign-in does not own the local device.")
        for key in ("principal_id", "device_id", "grant_id"):
            identifier(device.get(key))
        if (type(device.get("grant_revision")) is not int
                or enrollment and "revoked_at" not in device):
            raise ConnectorError("incompatible_grant", "Cloud returned an unsupported device registration.")
        return {"device_id": device["device_id"], "grant_id": device["grant_id"], "principal_id": principal}

    def save_device(self, device: dict, principal: str, *, connect: bool = False) -> None:
        """Keeps the device in use current from its signed receipt."""
        config = self.store.read("config.json")
        existing = config.get("device") or {}
        self.receipt(device, principal, existing.get("key_fingerprint"))
        if (existing.get("device_id"), existing.get("principal_id"), existing.get("grant_id")) != (
                device["device_id"], principal, device["grant_id"]):
            raise ConnectorError("device_binding_changed", "The existing device account and grant were preserved.")
        revoked = device.get("revoked_at") is not None
        if connect and not revoked and not revoking(self.store, config):
            config["enabled"] = True
        if revoked:
            config["revoked"] = True
        if config.get("revoked"):
            config["enabled"] = False
        if config != self.store.read("config.json"):
            self.store.write("config.json", config)
        self.config = config

    def signer(self, session: dict) -> Identity:
        """A sign-in's candidate signs it until its approved device is recorded; that device's key does after."""
        registration = session.get("registration")
        return Identity.load(self.store, registration["device"]["key_fingerprint"] if registration
                             else key_fingerprint(session["public_key"]))

    def discard(self, fingerprint: str) -> None:
        """Deletes a sign-in's candidate unless it is a stored device's key."""
        if fingerprint not in {device["key_fingerprint"] for device in self.store.read("config.json").get("devices", [])}:
            self.store.delete(key_file(fingerprint))

    async def native(self, action: str, value: dict | None = None, original: dict | None = None) -> dict:
        current = self.store.read("native-session.json", missing=True)
        session = current if current and (original is None or current["session_id"] == original["session_id"]) else original
        if session is None:
            raise ConnectorError("sign_in_required", "Sign in on this Connector first.")
        if session["server"] != self.cloud.server:
            raise ConnectorError("account_binding_changed", "The native session belongs to another Connector.")
        if session.get("ended"):
            # Read back as ended, which is final; the key that signed it may be gone.
            if action in {"device", "policy"}:
                raise ConnectorError("sign_in_required", "Sign in explicitly to manage this account.")
            return dict(session["ended"])
        candidate = key_fingerprint(session["public_key"])
        try:
            result = await self.cloud.native(f"/api/native-sessions/{session['session_id']}/{action}", session,
                                             value or {}, self.signer(session))
        except ConnectorError as error:
            if action != "status" or error.code != "native_session_unavailable" or session.get("expires_at", float("inf")) > time.time():
                raise
            result = {"session_id": session["session_id"], "key_fingerprint": candidate, "status": "expired", "account": None}
        if action not in {"status", "cancel", "sign-out"}:
            return result
        if (result.get("session_id") != session["session_id"]
                or result.get("key_fingerprint") not in {candidate, *session.get("listed", [])}):
            raise ConnectorError("account_binding_changed", "The original native sign-in identity changed.")
        if session.get("signed_out_locally") and result.get("status") == "active":
            result.update(status="sign_out_pending", account=None)
            result.pop("device", None)
        elif session is not current:
            if result.get("status") == "active":
                raise ConnectorError("account_binding_changed", "The original sign-in is no longer selected on this device.")
        elif result.get("status") in {"denied", "cancelled", "expired", "signed_out"}:
            device = result.get("device")
            if (result["status"] == "signed_out" and session.get("registration_applied")
                    and isinstance(device, dict) and device.get("revoked_at") is not None):
                self.save_device(device, session["registration"]["device"]["principal_id"])
            session["ended"] = result
            self.store.write("native-session.json", session)
            self.discard(candidate)
        elif result.get("status") == "active":
            account = result.get("account")
            if not isinstance(account, dict):
                raise ConnectorError("invalid_cloud_response", "Cloud did not verify the signed-in account.")
            if result.get("device") is None:
                raise ConnectorError("device_registration_unavailable", "Cloud has not verified this device registration.")
            self.adopt(session, result["device"], identifier(account.get("principal_id")), result["key_fingerprint"])
        return result

    def connects(self, session: dict) -> bool:
        """The recorded connect decision, while the intent is on. A Stop after the sign-in began, a sign-out or
        cancel, marks this session signed out, and a switched-off intent sets enabled false; both win."""
        return (session["registration"]["connect"] and not session.get("signed_out_locally")
                and remote_access.policy(self.store.read("config.json"))["enabled"])

    def adopt(self, session: dict, device: dict, principal: str, fingerprint: str) -> None:
        """Applies an approved sign-in. Its device and connect decision are recorded once;
        the device in use is then saved, and another device is switched to."""
        if not session.get("registration"):
            # Completing an explicit login may reconnect the device. A configuration
            # change after login began supersedes that intent.
            config = self.store.read("config.json")
            session["registration"] = {
                "device": device_record(self.store, self.receipt(device, principal, fingerprint, enrollment=True), fingerprint),
                "connect": session["configuration_hash"] == configuration(config),
                "switch": (config.get("device") or {}).get("device_id") != device["device_id"]}
            self.store.write("native-session.json", session)
        if session.get("registration_applied"):
            self.save_device(device, principal)
        elif session["registration"]["switch"]:
            self.switch(session)
        else:
            self.quiescent()  # as for a switch: an unpair that never reached Cloud does not hold this device
            connect = self.connects(session)
            self.save_device(device, principal, connect=connect)
            self.discard(key_fingerprint(session["public_key"]))
            session["registration_applied"] = True
            self.store.write("native-session.json", session)

    def switch(self, session: dict) -> None:
        """Moves this computer to the device its sign-in approved. The config.json write
        commits; until registration_applied, any later command resumes here with no Cloud read. Nothing is revoked.
        om follows config.json to the new device."""
        device = session["registration"]["device"]
        config = self.store.read("config.json")
        if config.get("device") != device:
            if not self.quiescent():
                return
            # The device replaces any other device of its account.
            devices = [stored for stored in config.get("devices", [])
                       if stored == device or stored["principal_id"] != device["principal_id"]]
            config.update(device=device, revoked=False, enabled=self.connects(session),
                          devices=devices if device in devices else [*devices, device])
            self.store.write("config.json", config)
        self.discard(key_fingerprint(session["public_key"]))
        session["registration_applied"] = True
        self.store.write("native-session.json", session)
        self.config = config

    def quiescent(self) -> bool:
        """No unpair that may have reached Cloud is unknown. One without a stored proof never did: it is not admitted,
        and its revocation record is deleted, so its device works again when it is used again."""
        pending = self.store.read("companion-pending.json", missing=True) or {"operations": []}
        held = False
        for identity in pending["operations"]:
            record = self.operation(identity)
            if record["status"] != "unknown" or record["action"] != "unpair":
                continue
            if self.revocation_proof(record):
                held = True
            else:
                # The record goes first: a crash before the rejection repeats this, never strands the record.
                if record.get("device_id"):
                    self.store.delete(revocation_record(record["device_id"]))
                record.update(status="rejected", result={"status": "not_admitted"})
                self.save(record)
        return not held

    def settle(self) -> None:
        """Finishes a switch an earlier process committed or held."""
        session = self.store.read("native-session.json", missing=True)
        if remote_access.switching(session):
            self.switch(session)

    def forget_stale(self, session: dict, stale: object) -> None:
        """Forgets the stored devices whose keys a sign-in listed and Cloud reports as no current device's.
        config.json is written first: a key file nothing names is deleted at the next sign-in. The fields it
        changes are not part of the sign-in's configuration digest."""
        if not isinstance(stale, list):
            raise ConnectorError("invalid_cloud_response", "Cloud returned an invalid sign-in.")
        stale = {key for key in stale if isinstance(key, str)} & set(session["listed"])
        config = self.store.read("config.json")
        forgotten = [device for device in config.get("devices", []) if device["key_fingerprint"] in stale]
        if not forgotten:
            return
        config["devices"] = [device for device in config["devices"] if device not in forgotten]
        if config.get("device") in forgotten:
            for key in ("device", "revoked"):
                config.pop(key, None)
            config["enabled"] = False
        self.store.write("config.json", config)
        for device in forgotten:
            self.store.delete(key_file(device["key_fingerprint"]))
            self.store.delete(revocation_record(device["device_id"]))

    async def settle_move(self, record: dict) -> dict | None:
        """Reads back a move whose reply was lost. Its sign-in becomes this computer's only if Cloud approved it;
        any other outcome leaves the sign-in in use as it was. None while the outcome is unknown."""
        session = record["session"]
        current = self.store.read("native-session.json", missing=True)
        if not current or current["session_id"] != session["session_id"]:
            candidate = key_fingerprint(session["public_key"])
            try:
                observed = await self.cloud.native(f"/api/native-sessions/{session['session_id']}/status", session, {},
                                                   Identity.load(self.store, candidate))
            except ConnectorError as error:
                # Cloud holds no such sign-in and, once it has expired, admits none: the move did not happen.
                if error.code != "native_session_unavailable" or session["expires_at"] > time.time():
                    raise
                observed = {"status": "expired"}
            if observed.get("status") == "pending":
                return None
            if observed.get("status") != "active":
                self.discard(candidate)
                return {"status": observed.get("status"), "session_id": session["session_id"]}
            self.store.write("native-session.json", session)
        return await self.native("status")

    def connection(self) -> dict:
        config = self.store.read("config.json")
        value = {"protocol": 1, "capabilities": ["device-registration", "remote-access"],
                 "binding": self.binding(), "server": self.cloud.server,
                 "key_fingerprint": (config.get("device") or {}).get("key_fingerprint"),
                 "observed_at_ms": time.time_ns() // 1_000_000, "paired": bool(config.get("device")),
                 "remote_access": remote_access.status(self.store)}
        if self.store.read("config.json") != config or config["server"] != self.cloud.server:
            raise ConnectorError("connector_binding_changed", "The connection changed during observation.")
        return value

    async def snapshot(self) -> dict:
        value = {"account": None, "device": None, "errors": {}}
        session = self.store.read("native-session.json", missing=True)
        if session and not session.get("ended"):
            try:
                value["account"] = await self.native("status")
            except ConnectorError as error:
                value["errors"]["account"] = error.code
        device = self.store.read("config.json").get("device")
        if device:
            value["device"] = {"device_id": device["device_id"], "name": "This device",
                               "key_fingerprint": device["key_fingerprint"], "status": "unverified"}
        if value["account"] and remote_access.switching(self.store.read("native-session.json", missing=True)):
            # Approved, but this computer has not moved to its new device yet.
            value["account"] = {**value["account"], "status": "pending", "account": None, "device": None}
        if device and (value.get("account") or {}).get("status") == "active":
            try:
                observed = await self.native("device", {"device_id": device["device_id"]})
                self.save_device(observed, value["account"]["account"]["principal_id"])
                value["device"] = {**observed, "status": "verified"}
            except ConnectorError as error:
                value["errors"]["device"] = error.code
        self.config = self.store.read("config.json")
        return {**value, **self.connection()}

    def operation(self, identity: str) -> dict:
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise ConnectorError("invalid_operation", "Use the original operation identity.")
        return self.store.read("companion-" + identity + ".json")

    def save(self, record: dict) -> None:
        self.store.write("companion-" + record["operation_id"] + ".json", record)

    def can_continue_revocation(self, record: dict) -> bool:
        return record["status"] == "unknown" and record["action"] == "unpair" and not self.revocation_proof(record)

    def revocation_proof(self, record: dict) -> dict | None:
        """The retained revocation of an unpair operation's device, once its proof was stored."""
        pending = record.get("device_id") and self.store.read(revocation_record(record["device_id"]), missing=True)
        return pending if pending and pending.get("headers") else None

    def public_operation(self, record: dict) -> dict:
        return {**{key: record[key] for key in ("operation_id", "action", "status", "result", "error") if key in record},
                "can_continue": self.can_continue_revocation(record)}

    async def recover(self, identity: str) -> dict:
        try:
            record = self.operation(identity)
        except ConnectorError as error:
            if error.code != "missing_local_state":
                raise
            # The operation lock serializes this tombstone with any late process.
            # A missing original can never be admitted after its result was read.
            record = {"operation_id": identity, "action": "not-admitted", "input": {},
                      "status": "rejected", "result": {"status": "not_admitted"}}
            self.store.write("companion-" + identity + ".json", record, create=True)
        if record["status"] in {"verified", "rejected"}:
            return self.public_operation(record)
        action = record["action"]
        if action not in ACTIONS:
            # Retired actions keep their original evidence, including unknown
            # effects. They are never replayed or rewritten as successful.
            return self.public_operation(record)
        if not record.get("admitted"):
            record.update(status="rejected", result={"status": "not_admitted"})
            self.save(record)
            return self.public_operation(record)
        result = None
        try:
            if action == "login":
                result = await self.native("status", original=record["session"])
                if result["status"] == "pending":
                    result["verification_uri"] = self.cloud.server + "/native-login?id=" + result["session_id"]
            elif action == "move":
                result = await self.settle_move(record)
            elif action in {"cancel-login", "sign-out"}:
                observed = await self.native("status", original=record["session"])
                if observed["status"] == "sign_out_pending":
                    # Cloud never saw it. Repeating a sign-out is harmless, and a lasting
                    # sign-in would otherwise never end. A cancel an approval won also stops, as perform does.
                    if action == "cancel-login" and not record.get("cancel_active"):
                        record["cancel_active"] = True
                        self.save(record)
                        disable(self.store)
                    observed = await self.native("sign-out", original=record["session"])
                if observed["status"] in {"cancelled", "signed_out", "denied", "expired"}:
                    result = observed
            elif action == "unpair":
                pending = self.revocation_proof(record)
                if pending:
                    name = revocation_record(record["device_id"])
                    observed = await self.cloud.request("POST", pending["path"], body=b"{}",
                        headers={**pending["headers"], "X-OM-Result-Only": "true"})
                    if observed.get("status") == "not_admitted":
                        pending.setdefault("expired_challenges", []).append(pending.pop("headers"))
                        pending["stage"] = "awaiting_challenge"
                        self.store.write(name, pending)
                        result = {"status": "expired_without_admission", "device_id": pending["device_id"]}
                    if observed.get("status") == "revoked":
                        config = self.store.read("config.json")
                        if config.get("device", {}).get("device_id") == record["device_id"]:
                            config["revoked"] = True
                            self.store.write("config.json", config)
                        pending["receipt"] = observed
                        self.store.write(name, pending)
                        result = observed
            elif action == "remote-access":
                # A local, idempotent write: applying the same revision again completes it.
                current = remote_access.policy(self.store.read("config.json"))
                if current["revision"] is not None and current["revision"] != record["input"]["revision"]:
                    result = {"status": "superseded", "remote_access": self.connection()["remote_access"]}
                else:
                    result = self.apply_intent(record["input"])
            if result is not None:
                record.update(status="verified", result=result)
                record.pop("error", None)
                self.save(record)
        except ConnectorError as error:
            record["error"] = error.code
            self.save(record)
        return self.public_operation(record)

    async def act(self, action: str, value: dict) -> dict:
        if action not in ACTIONS or not isinstance(value, dict):
            raise ConnectorError("invalid_action", "Select a supported account action.")
        self.settle()
        if action == "snapshot":
            return await self.snapshot()
        if action == "connection":
            return self.connection()
        if action == "operation":
            return await self.recover(value["operation_id"])
        if value.get("expected_binding") != self.binding():
            raise ConnectorError("connector_binding_changed", "The server or device changed.")
        key = None
        if action in {"login", "move"} and "key" in value:
            # A key from the Console, or a move's secret, goes only to Cloud's create: never into the operation
            # record, its retry comparison, a state file or an error.
            value = dict(value)
            key = value.pop("key")
        if action == "policy":
            # Direct, with no operation record: after a lost reply Desktop reads snapshot.
            device = self.store.read("config.json").get("device") or {}
            return await self.native("policy", {**{key: item for key, item in value.items() if key != "expected_binding"},
                                                "device_id": device.get("device_id")})
        if action == "continue-revocation":
            record = self.operation(value["operation_id"])
            if record["action"] == "unpair" and record.get("device_id") != self.config.get("device", {}).get("device_id"):
                raise ConnectorError("connector_binding_changed", "Continue the original device revocation only.")
            if not self.can_continue_revocation(record):
                return await self.recover(record["operation_id"])
            # No effect proof was persisted, so the prior call could not send its
            # revocation. Continue only on a separate explicit user action.
            try:
                result = await self.perform(record["action"], record["input"], record)
                record.update(status="verified", result=result)
                record.pop("error", None)
            except ConnectorError as error:
                record["error"] = error.code
            self.save(record)
            return self.public_operation(record)
        identity = value.get("operation_id")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise ConnectorError("invalid_operation", "A new explicit action needs its original operation ID.")
        name = "companion-" + identity + ".json"
        previous = self.store.read(name, missing=True)
        if previous is not None:
            if previous["action"] != action or previous["input"] != value:
                raise ConnectorError("operation_changed", "The original operation was preserved.")
            return await self.recover(identity)
        intent = None
        if action == "remote-access":
            intent = remote_access.read_intent(value.get("intent_path"), value.get("revision"))
            if type(value.get("revision")) is not int:
                raise ConnectorError("remote_access_invalid", "Use the exact intent revision.")
        if action in {"login", "move"}:
            self.quiescent()  # an unpair that never reached Cloud holds no sign-in, as it holds no switch
        pending = self.store.read("companion-pending.json", missing=True) or {"operations": []}
        unresolved = [identity for identity in pending["operations"] if self.operation(identity)["status"] == "unknown" and self.operation(identity)["action"] in ACTIONS]
        if action == "remote-access":
            for original in unresolved:
                retained = self.operation(original)
                if retained["action"] == action and retained["input"].get("revision") != value["revision"]:
                    await self.recover(original)
            unresolved = [identity for identity in unresolved if self.operation(identity)["status"] == "unknown"]
        for original in unresolved:
            retained = self.operation(original)
            if retained["action"] == action and (action != "remote-access"
                    or retained["input"].get("revision") == value["revision"]):
                return await self.recover(original)
        reducing = action in REDUCTIONS or action == "remote-access" and not intent["enabled"]
        if unresolved and not reducing:
            raise ConnectorError("operation_pending", "Read the original pending operation before another change.")
        if len(unresolved) >= 8:
            raise ConnectorError("operation_limit", "Read the retained original operations before another change.")
        # Only unresolved effects consume admission capacity. Completed receipts
        # stay addressable by ID; never scan or expire history to admit new work.
        record = {"operation_id": identity, "action": action, "input": value, "status": "unknown"}
        # The admission record precedes every effect, including local session changes.
        self.store.write(name, record, create=True)
        self.store.write("companion-pending.json", {"operations": [*unresolved, identity]})
        try:
            result = await self.perform(action, value, record, key)
            if result.get("status") == "unknown":
                raise ConnectorError("operation_unknown", "Read the operation back; completion was not verified.")
            record.update(status="verified", result=result)
        except ConnectorError as error:
            record["error"] = error.code
            if not record.get("admitted"):
                record["status"] = "rejected"
        self.save(record)
        return self.public_operation(record)

    def admit(self, record: dict) -> None:
        record["admitted"] = True
        self.save(record)

    def apply_intent(self, value: dict) -> dict:
        """Binds Desktop's intent and sets enabled from it."""
        config = self.store.read("config.json")
        binding = {"intent_path": value["intent_path"]}
        if config.get("remote_access") is not None and config["remote_access"] != binding:
            raise ConnectorError("remote_access_changed", "The original intent owner was preserved.")
        remote_access.read_intent(value["intent_path"], value["revision"])
        config["remote_access"] = binding
        config["enabled"] = remote_access.admits(self.store, config)
        if config != self.store.read("config.json"):
            self.store.write("config.json", config)
        current = self.connection()["remote_access"]
        return {"status": current["state"], "remote_access": current}

    async def perform(self, action: str, value: dict, record: dict, key: object = None) -> dict:
        if action == "remote-access":
            record["intent"] = remote_access.read_intent(value["intent_path"], value["revision"])
            self.admit(record)
            return self.apply_intent(value)
        if action in {"login", "move"}:
            # A move (Console, Move) registers this computer again in another account while it stays signed in.
            # Its sign-in is written only once Cloud approved it, so a move that fails changes nothing here.
            moving = action == "move"
            if moving:
                if not isinstance(key, str) or not remote_access.admits(self.store, self.store.read("config.json")):
                    raise ConnectorError("move_unavailable", "Only a connected computer can be moved.")
            elif self.store.read("native-session.json", missing=True):
                observed = await self.native("status")
                device = observed.get("device")
                if (observed["status"] in {"pending", "sign_out_pending"}
                        or observed["status"] == "active" and isinstance(device, dict) and device.get("revoked_at") is None):
                    if observed["status"] == "pending":
                        observed["verification_uri"] = self.cloud.server + "/native-login?id=" + observed["session_id"]
                    return observed
            # Cloud names a moved machine as the device it leaves.
            name = value.get("device_name", socket.gethostname()[:80] or "This computer")
            if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise ConnectorError("invalid_device_name", "Use a device name with 1 to 80 printable characters.")
            # An unreadable ID refuses sign-in; a Linux machine without one gets it here.
            hardware = hardware_id(self.store, create=True)
            stored = self.store.read("config.json").get("devices", [])
            if any(device.get("hardware_id") != hardware for device in stored):
                # Only an ID created just now can differ: the next command forgets these devices.
                raise ConnectorError("hardware_changed", "This registration belongs to another computer; sign in again.")
            # Any other key file is the candidate of an ended sign-in or the key of a forgotten device.
            named = {key_file(device["key_fingerprint"]) for device in stored}
            for file in self.store.names("key-"):
                if file not in named:
                    self.store.delete(file)
            keys = tuple(Identity.load(self.store, device["key_fingerprint"]) for device in stored)
            candidate = Identity.create(self.store)
            identity = secrets.token_urlsafe(32)
            session = {"session_id": identity, "challenge_id": identity, "challenge": secrets.token_urlsafe(32),
                       "server": self.cloud.server, "public_key": candidate.public_key,
                       "listed": [key.fingerprint for key in keys], "device_name": name,
                       "expires_at": int(time.time()) + (MOVE_SECONDS if moving else 300),
                       "configuration_hash": configuration(self.store.read("config.json"))}
            record["session"] = session
            self.admit(record)
            if not moving:
                self.store.write("native-session.json", session)
            # Cloud checks expires_at by its own clock.
            try:
                result = await self.cloud.native("/api/native-sessions", session, lambda now: {
                    "session_id": identity, "challenge": session["challenge"], "public_key": candidate.public_key,
                    "device_keys": [stored.public_key for stored in keys], "device_name": name,
                    "expires_at": now + (MOVE_SECONDS if moving else 300),
                    "platform": "macos" if sys.platform == "darwin" else "linux",
                    **({} if key is None else {"key": key})}, candidate, keys)
            except ConnectorError as error:
                if error.code in KEY_REFUSALS:
                    # Cloud created no sign-in, so nothing is left to read back: the next sign-in starts clean.
                    # The record goes first: a crash after it leaves no operation that waits on a deleted key.
                    record.update(status="rejected", error=error.code)
                    self.save(record)
                    if not moving:
                        self.store.delete("native-session.json")
                    self.discard(candidate.fingerprint)
                raise
            if moving:
                if result.get("status") != "active" or result.get("session_id") != identity:
                    raise ConnectorError("move_unverified", "Cloud has not verified this move.")
                self.store.write("native-session.json", session)
                return result
            self.forget_stale(session, result.get("stale_keys", []))
            return result
        if action in {"cancel-login", "sign-out"}:
            session = self.store.read("native-session.json")
            record["session"] = dict(session)
            self.admit(record)
            if action == "sign-out":
                # Takes this computer off Code before Cloud, even if Cloud is offline. Device ownership and
                # other sessions are retained.
                disable(self.store)
            session["signed_out_locally"] = True
            self.store.write("native-session.json", session)
            try:
                result = await self.native("cancel" if action == "cancel-login" else "sign-out")
            except ConnectorError as error:
                if action != "cancel-login" or error.code != "native_login_active":
                    raise
                # Browser approval won the race. The user's cancellation still
                # ends this exact session; it never removes the device record.
                record["cancel_active"] = True
                self.save(record)
                disable(self.store)
                result = await self.native("sign-out")
            if result.get("status") not in {"cancelled", "signed_out", "denied", "expired"}:
                raise ConnectorError("sign_out_unverified", "Read the original account sign-out before signing in again.")
            return result
        if action == "unpair":
            record["device_id"] = self.config.get("device", {}).get("device_id")
            if record["device_id"]:
                hardware_id(self.store)  # refused before admission, so nothing changes
            self.admit(record)
            return await unpair(self.store, self.cloud)
        raise ConnectorError("invalid_action", "Select a supported account action.")
