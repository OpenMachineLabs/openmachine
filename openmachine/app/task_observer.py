"""Service-owned original receipt observation, never command dispatch or AI work."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import random
import re
import threading
import time

from openmachine import tasks
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from openmachine.tasks import records

INTERVAL_MS = 2000
READ_SECONDS = 2.0
MAX_READERS = 4
MAX_ACTIVE = 32
EPISODE_MS = 3600000
MAX_ATTEMPTS = 120
COOLDOWN_MS = 30000
SSH_INTERVAL_MS = 5000
SSH_READ_SECONDS = 30.0
SSH_MAX_READERS = 2
SSH_MAX_ATTEMPTS = 60
SSH_COOLDOWN_MS = 60000
SSH_TRANSIENT = {"ssh_transport_unavailable", "ssh_observation_timeout"}
SSH_CONNECTION_ERRORS = SSH_TRANSIENT | {"ssh_unavailable", "ssh_missing", "ssh_protocol_error",
    "remote_runtime_unavailable", "ssh_host_key_mismatch", "ssh_authentication_required",
    "identity_mismatch", "identity_unavailable", "worker_identity_mismatch", "protocol_mismatch",
    "response_limit"}
# All other failures stop automatic observation. No permission/identity/corruption
# failure becomes a transport retry, and no retry calls a domain mutation.
TRANSIENT = {"resource_busy", "observation_busy", "reader_busy", "source_unavailable",
             "execution_not_found", "change_not_found", "execution_unavailable",
             "observation_timeout", "operation_outcome_unknown"}


def now_ms() -> int:
    return time.time_ns() // 1000000


def pending(record: dict) -> list[dict]:
    return [item for item in record["operations"] if records.pending(item)
            and item["locator"]["kind"] in {"execution", "change"}]


def remote(record: dict) -> bool:
    return any(item["locator"]["target_identity"] is not None for item in pending(record))


def blocked(recovery: dict, now: int) -> bool:
    return (recovery["reason_code"].startswith("blocked_")
            or recovery["attempts"] >= (SSH_MAX_ATTEMPTS if recovery.get("category") == "ssh_observation" else MAX_ATTEMPTS)
            or now >= recovery["deadline_ms"])


def delay(attempt: int, ssh: bool = False) -> int:
    base = (2000 if attempt == 1 else 4000 if attempt == 2 else SSH_COOLDOWN_MS) if ssh else (
        1000 if attempt == 1 else 2000 if attempt == 2 else COOLDOWN_MS)
    return int(base * random.uniform(0.8, 1.2))


def error_code(error: Exception) -> str:
    if isinstance(error, Fault):
        return error.code if re.fullmatch(r"[a-z0-9_]{1,64}", error.code) else "observation_failed"
    if isinstance(error, OSError):
        return "read_permission_denied" if error.errno in {errno.EACCES, errno.EPERM} else "source_unavailable"
    return "observer_internal_error"


def fail_observation(record: dict, started: int, now: int, code: str, connection: dict | None = None) -> None:
    """Record one failed read/retry in the existing bounded recovery episode."""
    if record["terminal"] is not None or not pending(record):
        return
    ssh = remote(record)
    category = "ssh_observation" if ssh else "local_observation"
    recovery = record["recovery"]
    if recovery is not None and recovery["category"] != category:
        raise Fault("task_recovery_owner", "Receipt observation cannot replace another recovery owner.")
    if connection is not None and recovery is not None and recovery["connection_ref"] is None:
        # A connection outage must not erase an earlier receipt-specific budget.
        # Its current allowance remains at the connection owner and projection.
        return
    if recovery is not None and recovery["connection_ref"] is not None and connection is None:
        recovery = None  # A verified connection recovered; a receipt fault has its own budget.
    if recovery is None:
        started = max(record["created_at_ms"], min(started, now))
        deadline = max(started, min(started + EPISODE_MS, record["contract"]["expires_at_ms"]))
        recovery = {"episode_id": records.digest([record["task_id"], started, category])[:32],
            "category": category, "started_at_ms": started, "deadline_ms": deadline,
            "attempts": 1, "next_attempt_ms": deadline, "reason_code": code, "connection_ref": None}
        remaining = sum(stage["state"] != "accepted" for stage in record["plan"]["stages"])
        if len(record["history"]) + remaining + 3 < 64:
            records.material(record, now, "recovery", f"Original receipt observation failed: {code}; original work retained.")
        else:
            code = "task_capacity"
        record["recovery"] = recovery
    if connection is not None:
        recovery.update(connection)
        recovery["started_at_ms"] = max(record["created_at_ms"], recovery["started_at_ms"])
        recovery["deadline_ms"] = max(recovery["started_at_ms"], min(recovery["deadline_ms"], record["contract"]["expires_at_ms"]))
    retryable = code in SSH_TRANSIENT or code in TRANSIENT and (not ssh or code != "source_unavailable")
    recovery["reason_code"] = code if retryable else "blocked_" + code
    recovery["next_attempt_ms"] = min((connection["next_attempt_ms"] if connection else now + delay(recovery["attempts"], ssh)), recovery["deadline_ms"])
    if recovery["reason_code"].startswith("blocked_"):
        recovery["next_attempt_ms"] = recovery["deadline_ms"]
    if record["control"]["stop_intent"] == "none":
        record["control"]["state"] = "recovering"


def clear_observation(record: dict) -> None:
    recovery = record["recovery"]
    if recovery is not None and recovery["category"] in {"local_observation", "ssh_observation"}:
        record["recovery"] = None
        if record["control"]["state"] == "recovering":
            record["control"]["state"] = "running" if any(records.pending(item) for item in record["operations"]) else "waiting_for_ai"


@dataclass
class _Read:
    record: dict
    started_at_ms: int
    started: float = field(default_factory=time.monotonic)
    stop: threading.Event = field(default_factory=threading.Event)
    observations: list = field(default_factory=list)
    error: str | None = None
    observed_at_ms: dict[str, int] = field(default_factory=dict)
    thread: threading.Thread | None = None
    timed_out: bool = False
    expected_revision: int = 0
    target: str | None = None
    connection: dict | None = None
    contacted: bool = False


class TaskObserver:
    """One coordinator, four local and two SSH readers, one per pinned target."""

    def __init__(self, application):
        self.app = application
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._dirty = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._snapshot = {"state": "not_started", "observed_at_ms": None, "blocker": None,
                          "active": {}, "last": {}, "errors": {}}
        self._known: dict[str, str] = {}
        self._active: dict[str, int] = {}
        self._last: dict[str, dict[str, int]] = {}
        self._errors: dict[str, str] = {}
        self._reads: dict[str, _Read] = {}
        self._connections: dict[str, dict | None] = {}
        self._discover_at = 0
        self._discovery_error = None
        self._admission_at = 0
        self._admission_error = None

    @contextmanager
    def running(self):
        self.start()
        try:
            yield self
        finally:
            self.stop()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise Fault("task_observer_busy", "The original observer still owns its readers; do not replace it.")
            self._stop.clear()
            self._dirty.set()
            self._known.clear()
            self._active.clear()
            self._last.clear()
            self._errors.clear()
            self._connections.clear()
            self._discovery_error = None
            self._admission_at = 0
            self._admission_error = None
            self._snapshot = dict(self._snapshot, state="starting", blocker=None)
            self._thread = threading.Thread(target=self._run, name="om-task-observer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(READ_SECONDS)
        # A blocked daemon reader cannot be killed safely. The coordinator retains
        # the owner lease until it exits, but never publishes after stopping.

    def notify(self) -> None:
        self._dirty.set()
        self._wake.set()

    def _status(self, state: str, blocker: str | None = None) -> None:
        with self._lock:
            self._snapshot = {"state": state, "observed_at_ms": now_ms(), "blocker": blocker or self._admission_error,
                "active": dict(self._active), "last": dict(self._last), "errors": dict(self._errors),
                "connections": dict(self._connections)}

    def _run(self) -> None:
        failure = None
        try:
            # Like the listener lease, this is service bootstrap coordination,
            # not task mutation authority. Activation may still hold admission
            # while probing this server; every receipt dispatch and publication
            # below waits for the original Application admission to become ready.
            private_directory(self.app.state)
            locks = private_directory(self.app.state / "locks")
            with lease(locks / "task-observer.lock"):
                try:
                    while not self._stop.is_set() or self._reads:
                        self._harvest()
                        if self._stop.is_set():
                            for read in self._reads.values():
                                read.stop.set()
                            self._status("stopping")
                        else:
                            try:
                                self._tick()
                            except (Fault, OSError) as error:
                                self._discovery_error = error_code(error)
                                self._discover_at = now_ms() + COOLDOWN_MS
                            self._status("running", self._discovery_error)
                        self._wake.wait(0.05)
                        self._wake.clear()
                except Exception as error:
                    failure = error_code(error)
                finally:
                    self._stop.set()
                    while any(read.thread is not None and read.thread.is_alive() for read in self._reads.values()):
                        for read in self._reads.values():
                            read.stop.set()
                        self._status("blocked" if failure else "stopping", failure)
                        self._wake.wait(0.05)
                        self._wake.clear()
                    self._reads.clear()
        except Exception as error:
            failure = error_code(error)
        # Even an unexpected coordinator fault drains readers INSIDE the owner
        # lease. No second observer can turn a timed-out read into a free slot.
        self._status("blocked" if failure else "stopped", failure)

    def _discover(self, now: int) -> None:
        page = tasks.observation_page(self.app.state, self._known)
        present = set(page["identities"])
        self._known = {key: value for key, value in self._known.items() if key in present}
        self._errors = {key: value for key, value in self._errors.items() if key in present or key in self._active}
        for entry in page["entries"]:
            identity = entry["task_id"]
            if entry["error"] is not None:
                self._errors[identity] = entry["error"]
            else:
                record = entry["record"]
                if record["terminal"] is not None or not pending(record):
                    self._active.pop(identity, None)
                    self._last.pop(identity, None)
                    self._errors.pop(identity, None)
                elif identity in self._active or len(self._active) < MAX_ACTIVE:
                    self._active.setdefault(identity, now)
                    self._errors.pop(identity, None)
                else:
                    self._errors[identity] = "task_observer_capacity"
                    # Revisit after a slot retires; never silently accept coverage.
                    continue
            self._known[identity] = entry["metadata_sha256"]
        self._discovery_error = None if page["complete"] else "task_index_incomplete"
        self._discover_at = now + (50 if page["has_more"] and len(self._active) < MAX_ACTIVE else INTERVAL_MS)

    def _tick(self) -> None:
        now = now_ms()
        dirty = self._dirty.is_set()
        if dirty:
            self._dirty.clear()
        if dirty or now >= self._discover_at:
            self._discover(now)
        if now < self._admission_at or not any(key not in self._reads and due <= now for key, due in self._active.items()):
            return
        try:
            with self.app._admit():
                pass
        except (Fault, OSError) as error:
            self._admission_error = error_code(error)
            # One shared check, not 32 task retries. A live activation owns its
            # completion; this observer never repairs or restarts the service.
            self._admission_at = now + INTERVAL_MS if self._admission_error in {"resource_busy", "activation_pending"} else records.MAX_INTEGER
            return
        self._admission_error = None
        for identity in sorted(self._active, key=lambda key: (self._active[key], key)):
            if self._stop.is_set():
                break
            if identity in self._reads or self._active[identity] > now:
                continue
            try:
                view = tasks.read(self.app.state, identity)
                record = view["record"]
                if record["terminal"] is not None or not pending(record):
                    self._active.pop(identity, None)
                    self._last.pop(identity, None)
                    continue
                targets = {item["locator"]["target_identity"] for item in pending(record)}
                if len(targets) != 1:
                    raise Fault("task_observation_scope", "Outstanding receipts must retain one original target.")
                target = next(iter(targets))
                ssh = target is not None
                interval = SSH_INTERVAL_MS if ssh else INTERVAL_MS
                if sum((item.target is not None) == ssh for item in self._reads.values()) >= (SSH_MAX_READERS if ssh else MAX_READERS):
                    continue
                if ssh and any(item.target == target for item in self._reads.values()):
                    continue
                if ssh and now >= record["contract"]["expires_at_ms"]:
                    raise Fault("task_authority_expired", "Automatic SSH observation authority expired; original work is retained.")
                connection = None
                if ssh:
                    owner = self.app.machines.ssh_recovery
                    if owner is None:
                        raise Fault("ssh_recovery_unavailable", "The configured SSH connection has no recovery owner.")
                    self._connections = owner.read(self.app.state)["targets"]
                    if (record["recovery"] is not None and record["recovery"]["connection_ref"] is not None
                            and target not in self._connections):
                        raise Fault("ssh_recovery_corrupt", "The referenced connection history is missing; preserve original work.")
                    connection = self._connections.get(target)
                    if connection is not None:
                        if blocked(dict(connection, category="ssh_observation"), now):
                            self._errors[identity] = connection["reason_code"] if connection["reason_code"].startswith("blocked_") else "observation_budget_exhausted"
                            self._active.pop(identity, None)
                            continue
                        if now < connection["next_attempt_ms"]:
                            self._active[identity] = connection["next_attempt_ms"]
                            continue
                recovery = record["recovery"]
                if recovery is not None:
                    if recovery["category"] != ("ssh_observation" if ssh else "local_observation"):
                        self._errors[identity] = "task_recovery_owner"
                        self._active.pop(identity, None)
                        continue
                    if recovery["connection_ref"] is not None:
                        # The task retains an observation, never a second owner
                        # of the shared connection's current retry allowance.
                        recovery = None
                if recovery is not None:
                    if blocked(recovery, now):
                        self._errors[identity] = recovery["reason_code"] if recovery["reason_code"].startswith("blocked_") else "observation_budget_exhausted"
                        self._active.pop(identity, None)
                        continue
                    if now < recovery["next_attempt_ms"]:
                        self._active[identity] = recovery["next_attempt_ms"]
                        continue
                    def reserve(value, timestamp):
                        current = value["recovery"]
                        if current != recovery or blocked(current, timestamp) or timestamp < current["next_attempt_ms"]:
                            raise Fault("task_conflict", "The original observation episode advanced.")
                        current["attempts"] += 1
                        # Durable crash fallback before this retry actually reads.
                        current["next_attempt_ms"] = min(timestamp + int((SSH_READ_SECONDS if ssh else READ_SECONDS) * 1000)
                            + delay(current["attempts"], ssh), current["deadline_ms"])
                    with self.app._admit():
                        view = tasks.update(self.app.state, identity, reserve, expected_revision=record["revision"])
                    record = view["record"]
                if ssh:
                    if connection is not None:
                        reserved = dict(connection, attempts=connection["attempts"] + 1)
                        reserved["next_attempt_ms"] = min(now + int(SSH_READ_SECONDS * 1000)
                            + delay(reserved["attempts"], True), reserved["deadline_ms"])
                    else:
                        reserved = None
                    with self.app._admit():
                        owner.replace(self.app.state, target, connection, reserved)
                    connection = reserved
                    self._connections[target] = connection
                read = _Read(record, now, expected_revision=record["revision"], target=target, connection=connection)
                read.thread = threading.Thread(target=self._read, args=(read,), name="om-task-read", daemon=True)
                self._reads[identity] = read
                self._active[identity] = now + interval
                read.thread.start()
            except (Fault, OSError) as error:
                code = error_code(error)
                self._errors[identity] = code
                if code not in {"resource_busy", "task_conflict", "task_write_unverified"}:
                    self._active.pop(identity, None)
                else:
                    self._active[identity] = now + INTERVAL_MS

    def _read(self, read: _Read) -> None:
        try:
            for operation in pending(read.record):
                if read.stop.is_set():
                    read.error = "ssh_observation_timeout" if read.target else "observation_timeout"
                    break
                target = self.app.config.machine(operation["locator"]["machine"])
                if (target.ssh_identity or None) != read.target or target.transport != ("ssh" if read.target else "local"):
                    raise Fault("task_target_changed", "Original evidence must not follow another route.")
                read.contacted = True
                observed = self.app.task_admission._read(operation, cancelled=read.stop) if read.target else self.app.task_admission._read(operation)
                result, locator = self.app.task_admission._observation(operation, observed)
                read.observations.append((operation["id"], result, locator))
                read.observed_at_ms[operation["id"]] = now_ms()
                if result["status"] == "unknown":
                    read.error = "operation_outcome_unknown"
        except Exception as error:
            read.error = error_code(error)
            if isinstance(error, Fault) and error.code == "ssh_unavailable" and (error.evidence or {}).get("ssh_retryable") is True:
                read.error = "ssh_transport_unavailable"
            if read.target and read.error == "observation_cancelled":
                read.error = "ssh_observation_timeout"
        finally:
            self._wake.set()

    def _commit(self, identity: str, read: _Read, *, timeout: bool = False) -> None:
        connection = None
        code = ("ssh_observation_timeout" if read.target else "observation_timeout") if timeout else read.error
        if read.target and (read.contacted or timeout):
            now = now_ms()
            if code in SSH_CONNECTION_ERRORS:
                connection = dict(read.connection) if read.connection else {
                    "episode_id": records.digest([read.target, read.started_at_ms])[:32],
                    "started_at_ms": read.started_at_ms, "deadline_ms": read.started_at_ms + EPISODE_MS,
                    "attempts": 1}
                connection["reason_code"] = code if code in SSH_TRANSIENT else "blocked_" + code
                connection["next_attempt_ms"] = min(now + delay(connection["attempts"], True), connection["deadline_ms"]) if code in SSH_TRANSIENT else connection["deadline_ms"]
            with self.app._admit():
                if self._stop.is_set():
                    return
                self.app.machines.ssh_recovery.replace(self.app.state, read.target, read.connection, connection)
            read.connection = connection
            self._connections[read.target] = connection
        def apply(record, timestamp):
            if record["terminal"] is not None:
                return
            if not timeout:
                self.app.task_admission.apply_observations(record, timestamp, read.observations)
            if code:
                shared = dict(connection, connection_ref=read.target) if connection is not None else None
                fail_observation(record, read.started_at_ms, timestamp, code, shared)
            else:
                clear_observation(record)
        with self.app._admit():
            if self._stop.is_set():
                return
            view = tasks.update(self.app.state, identity, apply, expected_revision=read.expected_revision)
        # Updating this CAS guard only follows our own verified publication. A
        # pause/decision/foreground result produces a conflict instead of overwrite.
        read.expected_revision = view["record"]["revision"]
        recovery = view["record"]["recovery"]
        self._active[identity] = recovery["next_attempt_ms"] if recovery is not None else now_ms() + (SSH_INTERVAL_MS if read.target else INTERVAL_MS)
        if not timeout and read.observed_at_ms:
            self._last[identity] = dict(read.observed_at_ms)
        self._errors.pop(identity, None)
        if view["record"]["terminal"] is not None or not pending(view["record"]):
            self._active.pop(identity, None)
            self._last.pop(identity, None)

    def _harvest(self) -> None:
        for identity, read in list(self._reads.items()):
            finished = not read.thread.is_alive()
            timeout = not finished and not read.timed_out and time.monotonic() - read.started >= (SSH_READ_SECONDS if read.target else READ_SECONDS)
            if not finished and not timeout:
                continue
            if timeout:
                read.timed_out = True
                read.stop.set()
            try:
                if not self._stop.is_set():
                    self._commit(identity, read, timeout=timeout)
            except (Fault, OSError) as error:
                code = error_code(error)
                self._errors[identity] = code
                if code in {"resource_busy", "activation_pending", "task_conflict", "ssh_recovery_conflict"}:
                    self._active[identity] = now_ms() + INTERVAL_MS
                else:
                    # Unacknowledged/permanent publication cannot durably account
                    # for further retries. Preserve original evidence and stop,
                    # rather than treating disk/capacity faults as a read loop.
                    self._active.pop(identity, None)
            if finished:
                # A deadline or a cancelled await never frees this slot early.
                del self._reads[identity]

    def project(self, view: dict) -> dict:
        if "record" not in view:
            return view
        with self._lock:
            snapshot = self._snapshot
        record, now = view["record"], now_ms()
        identity = record["task_id"]
        outstanding = {item["id"] for item in pending(record)}
        ssh = remote(record)
        interval = SSH_INTERVAL_MS if ssh else INTERVAL_MS
        recovery = record["recovery"]
        target = next((item["locator"]["target_identity"] for item in pending(record) if item["locator"]["target_identity"]), None)
        connection = snapshot.get("connections", {}).get(target)
        if connection is not None:
            recovery = dict(connection, category="ssh_observation")
        elif recovery is not None and recovery["connection_ref"] is not None:
            recovery = None
        blocker = snapshot["errors"].get(identity) or snapshot["blocker"]
        state = snapshot["state"]
        running = state == "running"
        next_attempt = snapshot["active"].get(identity)
        if record["terminal"] is not None:
            state, blocker = "retired", None
        elif not outstanding:
            state = "idle" if state == "running" else state
        elif ssh and now >= record["contract"]["expires_at_ms"]:
            state, blocker, next_attempt = "blocked" if running else state, "task_authority_expired", None
        elif recovery is not None and recovery["category"] in {"local_observation", "ssh_observation"}:
            if running:
                state = "blocked" if blocked(recovery, now) or blocker else "recovering"
            blocker = blocker or (recovery["reason_code"] if recovery["reason_code"].startswith("blocked_") else "observation_budget_exhausted" if blocked(recovery, now) else recovery["reason_code"])
            next_attempt = None if blocked(recovery, now) else recovery["next_attempt_ms"]
        elif state == "running":
            state = "blocked" if blocker else "observing" if identity in snapshot["active"] else "discovery_pending"
        if snapshot["observed_at_ms"] is not None and now - snapshot["observed_at_ms"] > 2 * INTERVAL_MS and state in {"observing", "running", "recovering", "discovery_pending"}:
            state, blocker = "stale", "observer_heartbeat_stale"
        if not running or state in {"retired", "idle", "blocked", "stale"}:
            next_attempt = None
        if ssh and next_attempt is not None and next_attempt >= record["contract"]["expires_at_ms"]:
            next_attempt = None
        observations = list(view["observations"])
        observations.append({"kind": "runtime", "state": state, "source": "ssh_task_observer" if ssh else "local_task_observer",
            "observed_at_ms": snapshot["observed_at_ms"],
            "last_progress_at_ms": max((item["observed_at_ms"] or 0 for item in record["operations"]), default=0) or None,
            "next_attempt_at_ms": next_attempt,
            "blocker": blocker})
        if connection is not None:
            observations.append({"kind": "connection", "state": "blocked" if blocked(recovery, now) else "recovering",
                "source": f"ssh_recovery_budget:{target}", "observed_at_ms": connection["started_at_ms"],
                "last_progress_at_ms": None, "next_attempt_at_ms": next_attempt,
                "blocker": blocker or connection["reason_code"]})
        for operation in record["operations"]:
            locator = operation["locator"]
            if locator["kind"] != "execution":
                continue
            observed_at = snapshot["last"].get(identity, {}).get(operation["id"], operation["observed_at_ms"]) if operation["id"] in outstanding else operation["observed_at_ms"]
            stale = records.pending(operation) and (observed_at is None or now - observed_at > 2 * interval)
            observations.append({"kind": "execution", "state": "stale" if stale else (operation["result"] or {}).get("status", "unknown"),
                "source": f"execution:{locator['machine']}:{locator['execution_id']}", "observed_at_ms": observed_at,
                "last_progress_at_ms": operation["observed_at_ms"], "next_attempt_at_ms": None,
                "blocker": "Original state is last-known; no current result is verified." if stale else None})
        return {**view, "support": {**view["support"], "local_result_observation": True,
            "ssh_result_observation": self.app.machines.ssh_recovery is not None}, "observations": observations}
