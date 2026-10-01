"""Service-owned local receipt observation, never command dispatch or AI work."""

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
# All other failures stop automatic observation. No permission/identity/corruption
# failure becomes a transport retry, and no retry calls a domain mutation.
TRANSIENT = {"resource_busy", "observation_busy", "reader_busy", "source_unavailable",
             "execution_not_found", "change_not_found", "execution_unavailable",
             "observation_timeout", "operation_outcome_unknown"}


def now_ms() -> int:
    return time.time_ns() // 1000000


def local_pending(record: dict) -> list[dict]:
    # An originally local locator remains local history if configuration changes.
    # Check the CURRENT route before reading it; never silently follow SSH instead.
    return [item for item in record["operations"] if records.pending(item)
            and item["locator"]["kind"] in {"execution", "change"}
            and item["locator"]["target_identity"] is None]


def blocked(recovery: dict, now: int) -> bool:
    return (recovery["reason_code"].startswith("blocked_")
            or recovery["attempts"] >= MAX_ATTEMPTS or now >= recovery["deadline_ms"])


def delay(attempt: int) -> int:
    base = 1000 if attempt == 1 else 2000 if attempt == 2 else COOLDOWN_MS
    return int(base * random.uniform(0.8, 1.2))


def error_code(error: Exception) -> str:
    if isinstance(error, Fault):
        return error.code if re.fullmatch(r"[a-z0-9_]{1,64}", error.code) else "observation_failed"
    if isinstance(error, OSError):
        return "read_permission_denied" if error.errno in {errno.EACCES, errno.EPERM} else "source_unavailable"
    return "observer_internal_error"


def fail_observation(record: dict, started: int, now: int, code: str) -> None:
    """Record one failed read/retry in the existing bounded recovery episode."""
    if record["terminal"] is not None or not local_pending(record):
        return
    recovery = record["recovery"]
    if recovery is not None and recovery["category"] != "local_observation":
        raise Fault("task_recovery_owner", "Local observation cannot replace another recovery owner.")
    if recovery is None:
        started = max(record["created_at_ms"], min(started, now))
        deadline = max(started, min(started + EPISODE_MS, record["contract"]["expires_at_ms"]))
        recovery = {"episode_id": records.digest([record["task_id"], started, "local_observation"])[:32],
            "category": "local_observation", "started_at_ms": started, "deadline_ms": deadline,
            "attempts": 1, "next_attempt_ms": deadline, "reason_code": code, "connection_ref": None}
        remaining = sum(stage["state"] != "accepted" for stage in record["plan"]["stages"])
        if len(record["history"]) + remaining + 3 < 64:
            records.material(record, now, "recovery", f"Local receipt observation failed: {code}; original work retained.")
        else:
            code = "task_capacity"
        record["recovery"] = recovery
    recovery["reason_code"] = code if code in TRANSIENT else "blocked_" + code
    recovery["next_attempt_ms"] = min(now + delay(recovery["attempts"]), recovery["deadline_ms"])
    if recovery["reason_code"].startswith("blocked_"):
        recovery["next_attempt_ms"] = recovery["deadline_ms"]
    if record["control"]["stop_intent"] == "none":
        record["control"]["state"] = "recovering"


def clear_observation(record: dict) -> None:
    recovery = record["recovery"]
    if recovery is not None and recovery["category"] == "local_observation":
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


class TaskObserver:
    """One coordinator per state root, with at most four actual local readers."""

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
                "active": dict(self._active), "last": dict(self._last), "errors": dict(self._errors)}

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
                if record["terminal"] is not None or not local_pending(record):
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
            if len(self._reads) >= MAX_READERS or self._stop.is_set():
                break
            if identity in self._reads or self._active[identity] > now:
                continue
            try:
                view = tasks.read(self.app.state, identity)
                record = view["record"]
                if record["terminal"] is not None or not local_pending(record):
                    self._active.pop(identity, None)
                    self._last.pop(identity, None)
                    continue
                recovery = record["recovery"]
                if recovery is not None:
                    if recovery["category"] != "local_observation":
                        self._errors[identity] = "task_recovery_owner"
                        self._active.pop(identity, None)
                        continue
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
                        current["next_attempt_ms"] = min(timestamp + int(READ_SECONDS * 1000) + delay(current["attempts"]), current["deadline_ms"])
                    with self.app._admit():
                        view = tasks.update(self.app.state, identity, reserve, expected_revision=record["revision"])
                    record = view["record"]
                read = _Read(record, now, expected_revision=record["revision"])
                read.thread = threading.Thread(target=self._read, args=(read,), name="om-task-read", daemon=True)
                self._reads[identity] = read
                self._active[identity] = now + INTERVAL_MS
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
            for operation in local_pending(read.record):
                if read.stop.is_set():
                    read.error = "observation_timeout"
                    break
                target = self.app.config.machine(operation["locator"]["machine"])
                if target.transport != "local" or target.ssh_identity:
                    raise Fault("task_target_changed", "Original local evidence must not follow another route.")
                observed = self.app.task_admission._read(operation)
                result, locator = self.app.task_admission._observation(operation, observed)
                read.observations.append((operation["id"], result, locator))
                read.observed_at_ms[operation["id"]] = now_ms()
                if result["status"] == "unknown":
                    read.error = "operation_outcome_unknown"
        except Exception as error:
            read.error = error_code(error)
        finally:
            self._wake.set()

    def _commit(self, identity: str, read: _Read, *, timeout: bool = False) -> None:
        def apply(record, timestamp):
            if record["terminal"] is not None:
                return
            if not timeout:
                self.app.task_admission.apply_observations(record, timestamp, read.observations)
            code = "observation_timeout" if timeout else read.error
            if code:
                fail_observation(record, read.started_at_ms, timestamp, code)
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
        self._active[identity] = recovery["next_attempt_ms"] if recovery is not None else now_ms() + INTERVAL_MS
        if not timeout and read.observed_at_ms:
            self._last[identity] = dict(read.observed_at_ms)
        self._errors.pop(identity, None)
        if view["record"]["terminal"] is not None or not local_pending(view["record"]):
            self._active.pop(identity, None)
            self._last.pop(identity, None)

    def _harvest(self) -> None:
        for identity, read in list(self._reads.items()):
            finished = not read.thread.is_alive()
            timeout = not finished and not read.timed_out and time.monotonic() - read.started >= READ_SECONDS
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
                if code in {"resource_busy", "activation_pending", "task_conflict"}:
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
        pending = {item["id"] for item in local_pending(record)}
        recovery = record["recovery"]
        blocker = snapshot["errors"].get(identity) or snapshot["blocker"]
        state = snapshot["state"]
        running = state == "running"
        next_attempt = snapshot["active"].get(identity)
        if record["terminal"] is not None:
            state, blocker = "retired", None
        elif not pending:
            state = "idle" if state == "running" else state
        elif recovery is not None and recovery["category"] == "local_observation":
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
        observations = list(view["observations"])
        observations.append({"kind": "runtime", "state": state, "source": "local_task_observer",
            "observed_at_ms": snapshot["observed_at_ms"],
            "last_progress_at_ms": max((item["observed_at_ms"] or 0 for item in record["operations"]), default=0) or None,
            "next_attempt_at_ms": next_attempt,
            "blocker": blocker})
        for operation in record["operations"]:
            locator = operation["locator"]
            if locator["kind"] != "execution":
                continue
            observed_at = snapshot["last"].get(identity, {}).get(operation["id"], operation["observed_at_ms"]) if operation["id"] in pending else operation["observed_at_ms"]
            stale = records.pending(operation) and (observed_at is None or now - observed_at > 2 * INTERVAL_MS)
            observations.append({"kind": "execution", "state": "stale" if stale else (operation["result"] or {}).get("status", "unknown"),
                "source": f"execution:{locator['machine']}:{locator['execution_id']}", "observed_at_ms": observed_at,
                "last_progress_at_ms": operation["observed_at_ms"], "next_attempt_at_ms": None,
                "blocker": "Original state is last-known; no current result is verified." if stale else None})
        return {**view, "support": {**view["support"], "local_result_observation": True}, "observations": observations}
