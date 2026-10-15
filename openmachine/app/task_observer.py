"""Service-owned original receipt scheduling; threads and leases remain with one observer."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time
from openmachine import tasks
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from openmachine.tasks import records
from . import task_observation as observation, task_receipts


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
        self._condition = threading.Condition(self._lock)
        self._sequence = 0
        self._thread = None
        self._snapshot = {"state": "not_started", "observed_at_ms": None, "blocker": None,
                          "active": {}, "last": {}, "errors": {}}
        self._cursor = 0
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
            self._cursor = 0
            self._active.clear()
            self._last.clear()
            self._errors.clear()
            self._connections.clear()
            self._discovery_error = None
            self._admission_at = 0
            self._admission_error = None
            self._snapshot = dict(self._snapshot, state="starting", blocker=None)
            self._sequence += 1
            self._condition.notify_all()
            self._thread = threading.Thread(target=self._run, name="om-task-observer", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(observation.READ_SECONDS)
        # A blocked daemon reader cannot be killed safely. The coordinator retains
        # the owner lease until it exits, but never publishes after stopping.

    def notify(self) -> None:
        with self._condition:
            self._sequence += 1
            self._condition.notify_all()
        self._dirty.set()
        self._wake.set()

    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def wait(self, sequence: int, seconds: float, cancelled: threading.Event | None = None) -> int:
        deadline = time.monotonic() + max(0.0, seconds)
        with self._condition:
            while self._sequence == sequence and not (cancelled is not None and cancelled.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(remaining, 0.1 if cancelled is not None else remaining))
            return self._sequence

    def _status(self, state: str, blocker: str | None = None) -> None:
        with self._condition:
            value = {"state": state, "observed_at_ms": observation.now_ms(), "blocker": blocker or self._admission_error,
                "active": dict(self._active), "last": dict(self._last), "errors": dict(self._errors),
                "connections": dict(self._connections)}
            changed = any(value.get(key) != self._snapshot.get(key)
                          for key in ("state", "blocker", "active", "last", "errors", "connections"))
            self._snapshot = value
            if changed:
                self._sequence += 1
                self._condition.notify_all()

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
                                self._discovery_error = observation.error_code(error)
                                self._discover_at = observation.now_ms() + observation.COOLDOWN_MS
                            self._status("running", self._discovery_error)
                        self._wake.wait(0.05)
                        self._wake.clear()
                except Exception as error:
                    failure = observation.error_code(error)
                finally:
                    self._stop.set()
                    while any(read.thread is not None and read.thread.is_alive()
                              for read in self._reads.values()):
                        for read in self._reads.values():
                            read.stop.set()
                        self._status("blocked" if failure else "stopping", failure)
                        self._wake.wait(0.05)
                        self._wake.clear()
                    self._reads.clear()
        except Exception as error:
            failure = observation.error_code(error)
        # Even an unexpected coordinator fault drains readers INSIDE the owner
        # lease. No second observer can turn a timed-out read into a free slot.
        self._status("blocked" if failure else "stopped", failure)

    def _discover(self, now: int) -> None:
        page = tasks.observation_page(self.app.state, self._cursor)
        self._discovery_error = None
        for entry in page["entries"]:
            identity = entry["task_id"]
            if entry["error"] is not None:
                self._errors[identity] = entry["error"]
                self._discovery_error = entry["error"]
                break
            else:
                record = entry["record"]
                if record["terminal"] is not None or not observation.pending(record):
                    self._active.pop(identity, None)
                    self._last.pop(identity, None)
                    self._errors.pop(identity, None)
                elif identity in self._active or len(self._active) < observation.MAX_ACTIVE:
                    self._active[identity] = now
                    self._errors.pop(identity, None)
                else:
                    self._errors[identity] = "task_observer_capacity"
                    # Keep this change unconsumed until an original slot retires.
                    break
            self._cursor = entry["change_seq"]
        self._discover_at = now + (50 if page["has_more"] and len(self._active) < observation.MAX_ACTIVE else observation.INTERVAL_MS)

    def _tick(self) -> None:
        now = observation.now_ms()
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
            self._admission_error = observation.error_code(error)
            # One shared check, not 32 task retries. A live activation owns its
            # completion; this observer never repairs or restarts the service.
            self._admission_at = now + observation.INTERVAL_MS if self._admission_error in {"resource_busy", "activation_pending"} else records.MAX_INTEGER
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
                if record["terminal"] is not None or not observation.pending(record):
                    self._active.pop(identity, None)
                    self._last.pop(identity, None)
                    continue
                target = observation.active_target(record)
                ssh = target is not None
                interval = observation.SSH_INTERVAL_MS if ssh else observation.INTERVAL_MS
                if sum((item.target is not None) == ssh for item in self._reads.values()) >= (observation.SSH_MAX_READERS if ssh else observation.MAX_READERS):
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
                        if observation.blocked(dict(connection, category="ssh_observation"), now):
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
                    if observation.blocked(recovery, now):
                        self._errors[identity] = recovery["reason_code"] if recovery["reason_code"].startswith("blocked_") else "observation_budget_exhausted"
                        self._active.pop(identity, None)
                        continue
                    if now < recovery["next_attempt_ms"]:
                        self._active[identity] = recovery["next_attempt_ms"]
                        continue
                    def reserve(value, timestamp):
                        current = value["recovery"]
                        if current != recovery or observation.blocked(current, timestamp) or timestamp < current["next_attempt_ms"]:
                            raise Fault("task_conflict", "The original observation episode advanced.")
                        current["attempts"] += 1
                        # Durable crash fallback before this retry actually reads.
                        current["next_attempt_ms"] = min(timestamp + int((observation.SSH_READ_SECONDS if ssh else observation.READ_SECONDS) * 1000)
                            + observation.delay(current["attempts"], ssh), current["deadline_ms"])
                    with self.app._admit():
                        view = tasks.update(self.app.state, identity, reserve, expected_revision=record["revision"], observation=True)
                    record = view["record"]
                if ssh:
                    if connection is not None:
                        reserved = dict(connection, attempts=connection["attempts"] + 1)
                        reserved["next_attempt_ms"] = min(now + int(observation.SSH_READ_SECONDS * 1000)
                            + observation.delay(reserved["attempts"], True), reserved["deadline_ms"])
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
                code = observation.error_code(error)
                self._errors[identity] = code
                if code not in {"resource_busy", "task_store_busy", "task_conflict", "task_write_unverified"}:
                    self._active.pop(identity, None)
                else:
                    self._active[identity] = now + observation.INTERVAL_MS

    def _read(self, read: _Read) -> None:
        try:
            for operation in observation.pending(read.record):
                if operation["locator"]["target_identity"] != read.target:
                    continue
                if read.stop.is_set():
                    read.error = "ssh_observation_timeout" if read.target else "observation_timeout"
                    break
                target = self.app.config.machine(operation["locator"]["machine"])
                if (target.ssh_identity or None) != read.target or target.transport != ("ssh" if read.target else "local"):
                    raise Fault("task_target_changed", "Original evidence must not follow another route.")
                read.contacted = True
                observed = self.app.task_receipts.read(operation, cancelled=read.stop) if read.target else self.app.task_receipts.read(operation)
                result, locator = task_receipts.observation(operation, observed)
                read.observations.append((operation["id"], result, locator))
                read.observed_at_ms[operation["id"]] = observation.now_ms()
                if result["status"] == "unknown":
                    read.error = "operation_outcome_unknown"
        except Exception as error:
            read.error = observation.error_code(error)
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
            now = observation.now_ms()
            if code in observation.SSH_CONNECTION_ERRORS:
                connection = dict(read.connection) if read.connection else {
                    "episode_id": records.digest([read.target, read.started_at_ms])[:32],
                    "started_at_ms": read.started_at_ms, "deadline_ms": read.started_at_ms + observation.EPISODE_MS,
                    "attempts": 1}
                connection["reason_code"] = code if code in observation.SSH_TRANSIENT else "blocked_" + code
                connection["next_attempt_ms"] = min(now + observation.delay(connection["attempts"], True), connection["deadline_ms"]) if code in observation.SSH_TRANSIENT else connection["deadline_ms"]
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
                observation.apply_observations(record, timestamp, read.observations)
            if code:
                shared = dict(connection, connection_ref=read.target) if connection is not None else None
                observation.fail_observation(record, read.started_at_ms, timestamp, code, shared,
                                             ssh=read.target is not None)
            else:
                observation.clear_observation(record)
        with self.app._admit():
            if self._stop.is_set():
                return
            view = tasks.update(self.app.state, identity, apply, expected_revision=read.expected_revision,
                observation=True, operation_ids=tuple(item[0] for item in read.observations))
        # Updating this CAS guard only follows our own verified publication. A
        # pause/decision/foreground result produces a conflict instead of overwrite.
        read.expected_revision = view["record"]["revision"]
        recovery = view["record"]["recovery"]
        self._active[identity] = recovery["next_attempt_ms"] if recovery is not None else observation.now_ms() + (observation.SSH_INTERVAL_MS if read.target else observation.INTERVAL_MS)
        if not timeout and read.observed_at_ms:
            self._last[identity] = dict(read.observed_at_ms)
        self._errors.pop(identity, None)
        if view["record"]["terminal"] is not None or not observation.pending(view["record"]):
            self._active.pop(identity, None)
            self._last.pop(identity, None)

    def _harvest(self) -> None:
        for identity, read in list(self._reads.items()):
            finished = not read.thread.is_alive()
            timeout = not finished and not read.timed_out and time.monotonic() - read.started >= (observation.SSH_READ_SECONDS if read.target else observation.READ_SECONDS)
            if not finished and not timeout:
                continue
            if timeout:
                read.timed_out = True
                read.stop.set()
            try:
                if not self._stop.is_set():
                    self._commit(identity, read, timeout=timeout)
            except (Fault, OSError) as error:
                code = observation.error_code(error)
                self._errors[identity] = code
                if code in {"resource_busy", "task_store_busy", "activation_pending", "task_conflict", "ssh_recovery_conflict"}:
                    self._active[identity] = observation.now_ms() + observation.INTERVAL_MS
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
        identity = view["record"]["task_id"]
        if (identity not in snapshot["active"] and len(snapshot["active"]) >= observation.MAX_ACTIVE
                and observation.pending(view["record"])):
            snapshot = {**snapshot, "errors": {identity: "task_observer_capacity", **snapshot["errors"]}}
        return observation.project(view, snapshot, self.app.machines.ssh_recovery is not None)
