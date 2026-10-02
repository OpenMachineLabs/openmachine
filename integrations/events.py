"""Bounded private webhook subscriptions and delivery facts, never task progress."""

import base64
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

EVENT = "task.handoff_ready"
MAX_BYTES = 524288
TTL_MS = 3600000
VERIFY_MS = 300000
MAX_ATTEMPTS = 60
MAX_SUBSCRIPTIONS = 64
Integer = Annotated[int, Field(ge=0, le=2**53 - 1)]
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
TaskID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


def now_ms() -> int:
    return time.time_ns() // 1000000


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def instant(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()


def key(secret: str) -> bytes:
    try:
        if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 94:
            raise ValueError("invalid key")
        raw = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(raw) <= 64:
            raise ValueError("invalid key length")
        return raw
    except (ValueError, TypeError) as error:
        raise Fault("invalid_event_secret", "Use a whsec_ signing key containing 24-64 base64-decoded bytes.") from error


def destination(url: str):
    try:
        if not isinstance(url, str) or len(url) > 2048 or not url.isascii() or any(ord(c) <= 32 for c in url):
            raise ValueError("invalid URL")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "\\" in url
                or "%" in parsed.hostname or parsed.port == 0):
            raise ValueError("invalid destination")
        return parsed
    except (ValueError, TypeError) as error:
        raise Fault("callback_address", "Callbacks require a bounded HTTPS URL without credentials or fragments.") from error


def _pairs(items):
    value = {}
    for name, item in items:
        if name in value:
            raise ValueError("duplicate key")
        value[name] = item
    return value


def _resolve(host: str, port: int, seconds: float):
    # libc resolution cannot be cancelled in a thread. Reap an owned child
    # before returning so DNS cannot retain a delivery or verification slot.
    query = ("import json, socket, sys\n"
             "addresses = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), type=socket.SOCK_STREAM)\n"
             "if len(addresses) > 64: sys.exit(1)\n"
             "print(json.dumps(addresses))\n")
    try:
        result = subprocess.run([sys.executable, "-I", "-c", query, host, str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=seconds, check=True)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("callback DNS deadline") from error
    except subprocess.CalledProcessError as error:
        raise OSError("callback DNS unavailable") from error
    return [(family, kind, protocol, name, tuple(address))
            for family, kind, protocol, name, address in json.loads(result.stdout)]


def post(url: str, body: bytes, identifier: str, subscription: dict, seconds: float) -> tuple[int, bytes, str | None]:
    """Pin a public address; bound the whole exchange, including slow responses."""
    parsed = destination(url)
    deadline = time.monotonic() + seconds
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=seconds)
    sock = None

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("callback deadline")
        return value

    def expire():
        # HTTPResponse retains a socket reference even after connection.close().
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    try:
        addresses = _resolve(parsed.hostname, parsed.port or 443, remaining())
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global
                                or ipaddress.ip_address(item[4][0]).is_multicast for item in addresses):
            raise Fault("callback_address", "The callback must resolve only to public unicast addresses.")
        family, kind, protocol, _, address = next((item for item in addresses if item[0] == socket.AF_INET), addresses[0])
        sock = socket.socket(family, kind, protocol)
        sock.settimeout(remaining())
        sock.connect(address)
        sock.settimeout(remaining())
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        connection.sock = sock
        timestamp = str(now_ms() // 1000)
        signed = identifier.encode() + b"." + timestamp.encode() + b"." + body
        keys = [subscription["secret"]]
        if subscription.get("previous_secret") and now_ms() < subscription["rotation_until_ms"]:
            keys.append(subscription["previous_secret"])
        signatures = " ".join("v1," + base64.b64encode(hmac.new(key(value), signed, hashlib.sha256).digest()).decode()
                              for value in keys)
        sock.settimeout(remaining())
        connection.request("POST", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), body=body,
            headers={"Content-Type": "application/json", "webhook-id": identifier, "webhook-timestamp": timestamp,
                     "webhook-signature": signatures, "X-MCP-Subscription-Id": subscription["id"]})
        sock.settimeout(remaining())
        response = connection.getresponse()
        raw = response.read(4097)
        remaining()
        if len(raw) > 4096:
            raise Fault("callback_response_limit", "The callback response exceeded 4 KiB.")
        # http.client never follows a redirect or sends an ambient cookie/proxy credential.
        return response.status, raw, response.getheader("Retry-After")
    except ssl.SSLError as error:
        raise Fault("callback_tls", "Callback TLS verification failed.") from error
    except (OSError, http.client.HTTPException) as error:
        raise Fault("callback_timeout" if time.monotonic() >= deadline or isinstance(error, TimeoutError)
                    else "callback_unavailable", "The bounded callback exchange did not verify receipt.") from error
    finally:
        timer.cancel()
        connection.close()
        if sock is not None:
            sock.close()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Payload(Model):
    task_id: TaskID
    handoff_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    task_revision: Annotated[int, Field(ge=1, le=2**53 - 1)]
    plan_revision: Annotated[int, Field(ge=1, le=2**53 - 1)]
    reason: Literal["plan_ready", "result_ready", "decision_required"]


class Budget(Model):
    started_at_ms: Integer
    deadline_ms: Integer
    attempts: Annotated[int, Field(ge=1, le=MAX_ATTEMPTS)]
    next_attempt_ms: Integer

    @model_validator(mode="after")
    def bounds(self):
        if not self.started_at_ms <= self.next_attempt_ms <= self.deadline_ms <= self.started_at_ms + TTL_MS:
            raise ValueError("invalid delivery episode")
        return self


class Delivery(Model):
    id: Identity
    payload: Payload
    occurred_at_ms: Integer
    epoch: Integer
    budget: Budget
    accepted_at_ms: Integer | None = None
    read_at_ms: Integer | None = None
    blocker: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")] | None = None


class Subscription(Model):
    id: Identity
    generation: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    principal: Annotated[str, Field(min_length=1, max_length=128)]
    task_id: TaskID
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    secret: Annotated[str, Field(max_length=94)]
    previous_secret: Annotated[str, Field(max_length=94)] | None = None
    rotation_until_ms: Integer = 0
    verified_at_ms: Integer
    expires_at_ms: Integer
    delivery: Delivery | None = None

    @model_validator(mode="after")
    def binding(self):
        key(self.secret)
        if self.previous_secret is not None:
            key(self.previous_secret)
        destination(self.url)
        if (self.id != subscription_id(self.principal, self.task_id, self.url)
                or not self.verified_at_ms < self.expires_at_ms <= self.verified_at_ms + TTL_MS
                or (self.previous_secret is None) != (self.rotation_until_ms == 0)
                or self.rotation_until_ms > self.expires_at_ms
                or self.delivery is not None and (self.delivery.payload.task_id != self.task_id
                    or self.delivery.id != digest([self.id, self.delivery.payload.handoff_id]))):
            raise ValueError("invalid subscription binding")
        return self


class Store(Model):
    format: Annotated[int, Field(ge=1, le=1)] = 1
    subscriptions: Annotated[dict[Identity, Subscription], Field(max_length=MAX_SUBSCRIPTIONS)] = Field(default_factory=dict)
    connections: Annotated[dict[str, Budget], Field(max_length=MAX_SUBSCRIPTIONS)] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identities(self):
        if any(name != value.id for name, value in self.subscriptions.items()):
            raise ValueError("invalid subscription identity")
        if self.connections.keys() - {origin(item.url) for item in self.subscriptions.values()}:
            raise ValueError("unbound connection budget")
        return self


def subscription_id(principal: str, task_id: str, url: str) -> str:
    return digest([principal, url, EVENT, {"task_id": task_id}])


def _private(path: Path, directory: bool = False):
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("unsafe Events state")
    return info


def _load(state: Path) -> dict:
    path = state / "events.json"
    before = _private(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or info.st_size > MAX_BYTES:
            raise ValueError("Events state identity or size changed")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Events state exceeds capacity")
    return Store.model_validate(json.loads(raw, object_pairs_hook=_pairs)).model_dump()


@contextmanager
def _locked(state: Path, write: bool = False):
    path, lock = state / "events.json", state / "locks/events.lock"
    existing = os.path.lexists(path) or os.path.lexists(lock)
    if not existing and not write:
        yield Store().model_dump()
        return
    try:
        if existing:
            _private(state, True)
            _private(lock.parent, True)
            _private(lock)
        else:
            private_directory(state)
            private_directory(lock.parent)
        with ExitStack() as held:
            # A webhook ACK and refresh may overlap one short fsync. Bound local
            # contention without replaying verification or the yielded mutation.
            for attempt in range(20):
                try:
                    held.enter_context(lease(lock, shared=not write, create=not existing))
                    break
                except Fault as error:
                    if error.code != "resource_busy" or attempt == 19:
                        raise
                    time.sleep(0.005)
            value = _load(state) if existing or os.path.lexists(path) else Store().model_dump()
            before = deepcopy(value)
            yield value
            if write and (not existing or value != before):
                value = Store.model_validate(value).model_dump()
                if len(json.dumps(value, sort_keys=True).encode()) + 1 > MAX_BYTES:
                    raise Fault("events_capacity", "The bounded Events store is full.")
                save_json(path, value)
                directory = os.open(lock.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                if _load(state) != value:
                    raise ValueError("Events readback mismatch")
    except (OSError, ValueError, TypeError, RecursionError, ValidationError) as error:
        raise Fault("events_unverified" if write else "events_corrupt",
                    "Events history is missing, unsafe or unverified; preserve it without resetting delivery.") from error


def read(state: Path) -> dict:
    with _locked(state) as value:
        return value


def check_state(state: Path) -> dict:
    value = read(state)
    present = os.path.lexists(state / "events.json")
    return {"events_present": present, "events_format": 1, "events_subscriptions": len(value["subscriptions"]),
            "events_sha256": digest(value) if present else None}


def exhausted(budget: dict, now: int) -> bool:
    return now >= budget["deadline_ms"] or budget["attempts"] >= MAX_ATTEMPTS


def delay(attempts: int) -> int:
    return int((1000 * 2 ** (attempts - 1) if attempts < 4 else 60000) * random.uniform(0.8, 1.2))


def origin(url: str) -> str:
    parsed = destination(url)
    return f"{parsed.hostname}:{parsed.port or 443}"


def parameters(params: dict, *, subscribing: bool) -> tuple[str, str]:
    allowed = {"name", "arguments", "delivery", "cursor", "ttlMs"} if subscribing else {"name", "arguments", "delivery"}
    arguments, delivery = params.get("arguments"), params.get("delivery")
    if (set(params) - allowed or params.get("name") != EVENT or not isinstance(arguments, dict)
            or set(arguments) != {"task_id"} or not isinstance(arguments["task_id"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", arguments["task_id"]) is None
            or not isinstance(delivery, dict) or set(delivery) != ({"mode", "url", "secret"} if subscribing else {"mode", "url"})
            or delivery.get("mode") != "webhook" or params.get("cursor") is not None
            or params.get("ttlMs") is not None and (type(params["ttlMs"]) is not int or params["ttlMs"] <= 0)):
        raise Fault("invalid_event_request", "Use task.handoff_ready, exactly one task_id, webhook delivery and no replay cursor.")
    destination(delivery["url"])
    if subscribing:
        key(delivery["secret"])
    return arguments["task_id"], delivery["url"]


def same_attempt(current: dict, job: dict) -> bool:
    delivery = current["delivery"]
    return (current["generation"] == job["generation"] and delivery is not None
            and delivery["id"] == job["delivery"]["id"]
            and delivery["budget"]["attempts"] == job["delivery"]["budget"]["attempts"])


class Events:
    def __init__(self, state: Path):
        self.state = state
        self.verifications = threading.BoundedSemaphore(2)
        self._read_lock = threading.Lock()
        self._reads: dict[tuple[str, str], int] = {}

    def subscribe(self, params: dict, principal: str, authorize, cancelled=None) -> dict:
        if not self.verifications.acquire(blocking=False):
            raise Fault("callback_busy", "Two subscription verifications are already outstanding.")
        try:
            return self._subscribe(params, principal, authorize, cancelled)
        finally:
            self.verifications.release()

    def _subscribe(self, params: dict, principal: str, authorize, cancelled) -> dict:
        task_id, url = parameters(params, subscribing=True)
        with authorize(task_id) as expiry:
            pass
        identity = subscription_id(principal, task_id, url)
        old = read(self.state)["subscriptions"].get(identity)
        secret = params["delivery"]["secret"]
        start = now_ms()
        cached = (old is not None and old["secret"] == secret and old["expires_at_ms"] > start
                  and old["verified_at_ms"] + VERIFY_MS > start)
        if cancelled is not None and cancelled.is_set():
            raise Fault("callback_timeout", "Subscription verification was cancelled.")
        if not cached:
            challenge = secrets.token_hex(32)
            status, raw, _ = post(url, canonical({"type": "verification", "challenge": challenge}),
                "verify_" + secrets.token_hex(16), {"id": identity, "secret": secret}, 5)
            try:
                received = json.loads(raw, object_pairs_hook=_pairs)
                echoed = received.get("challenge") if isinstance(received, dict) else None
            except (ValueError, UnicodeError):
                echoed = None
            if (not 200 <= status < 300 or not isinstance(echoed, str)
                    or not hmac.compare_digest(challenge.encode(), echoed.encode()) or now_ms() >= start + 5000):
                raise Fault("callback_challenge_failed", "The bounded signed callback challenge was not verified.")
        with authorize(task_id) as current_expiry:
            now = now_ms()
            expiry = min(expiry, current_expiry, now + min(params.get("ttlMs") or TTL_MS, TTL_MS))
            verified = old["verified_at_ms"] if cached else now
            expiry = min(expiry, verified + TTL_MS)
            if expiry <= now or cancelled is not None and cancelled.is_set():
                raise Fault("callback_timeout", "The subscription expired or verification was cancelled.")
            with _locked(self.state, True) as value:
                if cancelled is not None and cancelled.is_set():
                    raise Fault("callback_timeout", "Subscription verification was cancelled.")
                current = value["subscriptions"].get(identity)
                if (({k: v for k, v in current.items() if k != "delivery"} if current else None)
                        != ({k: v for k, v in old.items() if k != "delivery"} if old else None)):
                    raise Fault("events_conflict", "The subscription changed during verification; inspect its original identity.")
                # Reclaim only bounded owner facts whose grant has expired.
                value["subscriptions"] = {k: v for k, v in value["subscriptions"].items() if v["expires_at_ms"] > now or k == identity}
                if identity not in value["subscriptions"] and len(value["subscriptions"]) >= MAX_SUBSCRIPTIONS:
                    raise Fault("events_capacity", "At most 64 active subscriptions are supported.")
                item = Subscription(id=identity, generation=old["generation"] if old else secrets.token_hex(16),
                    principal=principal, task_id=task_id, url=url, secret=secret,
                    verified_at_ms=verified, expires_at_ms=expiry).model_dump()
                if old is not None:
                    item["delivery"] = current["delivery"]
                    if old["secret"] != secret and old["expires_at_ms"] > now:
                        item.update(previous_secret=old["secret"], rotation_until_ms=min(expiry, now + VERIFY_MS))
                    elif old["rotation_until_ms"] > now:
                        item.update(previous_secret=old["previous_secret"], rotation_until_ms=min(expiry, old["rotation_until_ms"]))
                value["subscriptions"][identity] = item
                if not cached:
                    value["connections"].pop(origin(url), None)  # Actual health, never an unobserved budget reset.
                live = {origin(item["url"]) for item in value["subscriptions"].values()}
                value["connections"] = {k: v for k, v in value["connections"].items() if k in live}
        return {"id": identity, "refreshBefore": instant(expiry), "cursor": None, "truncated": False}

    def unsubscribe(self, params: dict, principal: str) -> dict:
        task_id, url = parameters(params, subscribing=False)
        identity = subscription_id(principal, task_id, url)
        if identity not in read(self.state)["subscriptions"]:
            return {}
        with _locked(self.state, True) as value:
            value["subscriptions"].pop(identity, None)
            live = {origin(item["url"]) for item in value["subscriptions"].values()}
            value["connections"] = {k: v for k, v in value["connections"].items() if k in live}
        return {}

    def task_ids(self) -> set[str]:
        return {item["task_id"] for item in read(self.state)["subscriptions"].values() if item["expires_at_ms"] > now_ms()}

    def facts(self, task_id: str) -> list[dict]:
        value = read(self.state)
        return [{"subscription_id": item["id"], "expires_at_ms": item["expires_at_ms"],
                 "delivery": item["delivery"], "connection": value["connections"].get(origin(item["url"]))}
                for item in value["subscriptions"].values() if item["task_id"] == task_id]

    def retire(self, task_id: str) -> None:
        if not any(item["task_id"] == task_id for item in read(self.state)["subscriptions"].values()):
            return
        with _locked(self.state, True) as value:
            value["subscriptions"] = {k: v for k, v in value["subscriptions"].items() if v["task_id"] != task_id}
            live = {origin(item["url"]) for item in value["subscriptions"].values()}
            value["connections"] = {k: v for k, v in value["connections"].items() if k in live}

    def due(self, task_id: str, handoff_id: str, now: int) -> list[str]:
        value = read(self.state)
        ready = []
        for identity, item in value["subscriptions"].items():
            delivery = item["delivery"]
            budget = value["connections"].get(origin(item["url"]))
            if item["task_id"] != task_id or item["expires_at_ms"] <= now:
                continue
            if budget is not None and (exhausted(budget, now) or budget["next_attempt_ms"] > now):
                continue
            if delivery is None or delivery["payload"]["handoff_id"] != handoff_id or (
                    delivery["accepted_at_ms"] is None and delivery["blocker"] is None
                    and not exhausted(delivery["budget"], now) and delivery["budget"]["next_attempt_ms"] <= now):
                ready.append(identity)
        return ready

    def reserve(self, identity: str, principal: str, offer: dict) -> dict | None:
        """Called inside Application's short task/lifecycle guard, before any network IO."""
        now = now_ms()
        with _locked(self.state, True) as value:
            item = value["subscriptions"].get(identity)
            if (item is None or item["principal"] != principal or item["task_id"] != offer["payload"]["task_id"]
                    or now >= min(item["expires_at_ms"], offer["expires_at_ms"])):
                return None
            connection = value["connections"].get(origin(item["url"]))
            if connection is not None and (exhausted(connection, now) or connection["next_attempt_ms"] > now):
                return None
            current = item["delivery"]
            if current is None or current["payload"]["handoff_id"] != offer["payload"]["handoff_id"]:
                current = Delivery(id=digest([identity, offer["payload"]["handoff_id"]]), payload=offer["payload"],
                    occurred_at_ms=offer["occurred_at_ms"], epoch=offer["epoch"], budget=Budget(started_at_ms=now,
                    deadline_ms=min(now + TTL_MS, item["expires_at_ms"], offer["expires_at_ms"]), attempts=1,
                    next_attempt_ms=now)).model_dump()
                item["delivery"] = current
            else:
                if (current["accepted_at_ms"] is not None or current["blocker"] is not None
                        or exhausted(current["budget"], now) or current["budget"]["next_attempt_ms"] > now):
                    return None
                current["budget"]["attempts"] += 1
            budget = current["budget"]
            budget["next_attempt_ms"] = min(budget["deadline_ms"], now + 10000 + delay(budget["attempts"]))
            if connection is None:
                connection = {"started_at_ms": now, "deadline_ms": now + TTL_MS, "attempts": 1, "next_attempt_ms": now}
            else:
                connection["attempts"] += 1
            connection["next_attempt_ms"] = min(connection["deadline_ms"], now + 10000 + delay(connection["attempts"]))
            value["connections"][origin(item["url"])] = connection
            job = deepcopy(item)
        return job

    def deliver(self, job: dict) -> tuple[int, str | None]:
        current = read(self.state)["subscriptions"].get(job["id"])
        if (current is None or current["expires_at_ms"] <= now_ms() or not same_attempt(current, job)):
            raise Fault("events_superseded", "The original delivery is no longer current.")
        delivery = job["delivery"]
        if now_ms() >= delivery["budget"]["deadline_ms"]:
            raise Fault("events_expired", "The original delivery budget expired before the exchange.")
        body = canonical({"eventId": delivery["id"], "name": EVENT, "timestamp": instant(delivery["occurred_at_ms"]),
                          "data": delivery["payload"], "cursor": None})
        if len(body) > 4096:
            raise Fault("events_payload_limit", "The event envelope exceeds 4 KiB.")
        status, _, retry = post(current["url"], body, delivery["id"], current,
                               min(10, (min(current["expires_at_ms"], delivery["budget"]["deadline_ms"]) - now_ms()) / 1000))
        return status, retry

    def complete(self, job: dict, *, status: int | None = None, retry: str | None = None, error: str | None = None) -> None:
        now = now_ms()
        with _locked(self.state, True) as value:
            item = value["subscriptions"].get(job["id"])
            if item is None or not same_attempt(item, job):
                return
            delivery, host = item["delivery"], origin(item["url"])
            if status is not None and 200 <= status < 300:
                delivery["accepted_at_ms"] = now
                value["connections"].pop(host, None)
                return
            transient = error in {"callback_timeout", "callback_unavailable"} or status == 429 or status is not None and status >= 500
            if not transient:
                delivery["blocker"] = error or f"callback_http_{status}"
                value["connections"].pop(host, None)
                return
            connection = value["connections"].get(host)
            wait = max(delay(delivery["budget"]["attempts"]), delay(connection["attempts"]) if connection else 0)
            if retry:
                try:
                    from email.utils import parsedate_to_datetime
                    wait = max(wait, int(retry) * 1000 if retry.isdecimal() else int(parsedate_to_datetime(retry).timestamp() * 1000) - now)
                except (ValueError, TypeError, OverflowError):
                    pass
            for budget in (delivery["budget"], connection):
                if budget is not None:
                    budget["next_attempt_ms"] = min(budget["deadline_ms"], now + max(0, wait))

    def note_read(self, task_id: str, handoff_id: str) -> None:
        with self._read_lock:
            if len(self._reads) < 32 or (task_id, handoff_id) in self._reads:
                self._reads[(task_id, handoff_id)] = now_ms()

    def flush_reads(self) -> None:
        with self._read_lock:
            observed, self._reads = self._reads, {}
        if not observed or not read(self.state)["subscriptions"]:
            return
        with _locked(self.state, True) as value:
            for item in value["subscriptions"].values():
                delivery = item["delivery"]
                if delivery is not None:
                    timestamp = observed.get((item["task_id"], delivery["payload"]["handoff_id"]))
                    if timestamp is not None and timestamp >= delivery["budget"]["started_at_ms"]:
                        delivery["read_at_ms"] = max(timestamp, delivery["read_at_ms"] or 0)
