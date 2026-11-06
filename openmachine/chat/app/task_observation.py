"""Bounded original-result observation policy, pure transitions and read-only views."""

import errno
import random
import re
import time
from openmachine import tasks
from openmachine.foundation.sanitize import Fault
from openmachine.tasks import effects, records, transitions

INTERVAL_MS = 2000
READ_SECONDS = 2.0
MAX_READERS = 4
MAX_ACTIVE = 32
EPISODE_MS = 3600000
MAX_ATTEMPTS = 120
COOLDOWN_MS = 30000
TRANSIENT = {"resource_busy", "observation_busy", "reader_busy", "source_unavailable",
             "execution_not_found", "change_not_found", "execution_unavailable",
             "observation_timeout", "operation_outcome_unknown"}


def now_ms() -> int:
    return time.time_ns() // 1000000


def pending(record: dict) -> list[dict]:
    return [item for item in record["operations"] if records.pending(item)
            and item["locator"]["kind"] in {"execution", "change"}]








def blocked(recovery: dict, now: int) -> bool:
    return (recovery["reason_code"].startswith("blocked_")
            or recovery["attempts"] >= MAX_ATTEMPTS
            or now >= recovery["deadline_ms"])


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
    """Record one failed read in the original bounded recovery episode."""
    if record["terminal"] is not None or not pending(record):
        return
    recovery = record["recovery"]
    if recovery is not None and recovery["category"] != "local_observation":
        raise Fault("task_recovery_owner", "Receipt observation cannot replace another recovery owner.")
    if recovery is None:
        started = max(record["created_at_ms"], min(started, now))
        deadline = max(started, min(started + EPISODE_MS, record["contract"]["expires_at_ms"]))
        recovery = {"episode_id": records.digest([record["task_id"], started, "local_observation"])[:32],
            "category": "local_observation", "started_at_ms": started, "deadline_ms": deadline,
            "attempts": 1, "next_attempt_ms": deadline, "reason_code": code, "connection_ref": None}
        transitions.material(record, now, "recovery", f"Original receipt observation failed: {code}; original work retained.")
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


def apply_observations(record: dict, now_ms: int, observations: list[tuple[str, dict, dict]], *, expected=None) -> None:
    """Pure result/handoff transition shared with the revision-checked observer."""
    if record["terminal"] is not None:
        return
    changed = []
    for operation_id, result, locator in observations:
        operation = next((item for item in record["operations"] if item["id"] == operation_id), None)
        if operation is None or tasks.operation_key(operation["locator"]) != tasks.operation_key(locator):
            raise Fault("task_operation_conflict", "Original observation binding changed; preserve its domain receipt.")
        if expected is not None and expected.get(operation_id) != records.digest(operation):
            # The original receipt was read outside the transaction. A
            # newer observation owns the current row; this stale read is inert.
            continue
        if operation["result"] == result and operation["locator"] == locator:
            continue
        if effects.goal_witness(record, operation) is not None:
            continue  # The independent goal proof never reclassifies historical uncertainty.
        if operation["locator"]["kind"] == "github" and operation["result"] is not None and not records.pending(operation):
            continue
        # A transient unavailable read never erases a verified historical outcome.
        if operation["result"] is not None and not records.pending(operation) and result["status"] in {"unknown", "running"}:
            continue
        operation.update(result=result, locator=locator, state="unknown" if result["status"] == "unknown" else "observed",
                         observed_at_ms=now_ms)
        changed.append(operation)
    if not changed:
        return
    recovery = record["recovery"]
    if recovery is not None and recovery["category"] == "local_observation" and not pending(record):
        record["recovery"] = None
    control = record["control"]
    if control["stop_intent"] == "cancel" and not any(effects.unresolved(item) for item in record["operations"]):
        transitions.finish_record(record, "cancelled", [], "Explicit stop; original operation outcomes are retained separately.", now_ms)
    elif control["stop_intent"] == "none":
        stage = transitions.stage_for(record)
        decisive = next((item for item in reversed(changed) if item["result"]["status"] != "running"
            and item["stage_id"] == stage["id"] and item["plan_revision"] == record["plan"]["revision"]
            and item["source_sha256"] == stage["source_sha256"]), None)
        if decisive is not None:
            control["state"] = "waiting_for_ai"
            control["pending_decision"] = None if decisive["result"]["status"] == "succeeded" else "Review the original operation outcome before new work."
            transitions.handoff(record, decisive["id"], now_ms,
                            "result_ready" if decisive["result"]["status"] == "succeeded" else "decision_required")


def project(view: dict, snapshot: dict) -> dict:
    if "record" not in view:
        return view
    record, now = view["record"], now_ms()
    identity = record["task_id"]
    outstanding = {item["id"] for item in pending(record)}
    recovery = record["recovery"]
    blocker = snapshot["errors"].get(identity) or snapshot["blocker"]
    state = snapshot["state"]
    running = state == "running"
    next_attempt = snapshot["active"].get(identity)
    if record["terminal"] is not None:
        state, blocker = "retired", None
    elif not outstanding:
        state = "idle" if running else state
    elif recovery is not None and recovery["category"] == "local_observation":
        if running:
            state = "blocked" if blocked(recovery, now) or blocker else "recovering"
        blocker = blocker or (recovery["reason_code"] if recovery["reason_code"].startswith("blocked_") else "observation_budget_exhausted" if blocked(recovery, now) else recovery["reason_code"])
        next_attempt = None if blocked(recovery, now) else recovery["next_attempt_ms"]
    elif running:
        state = "blocked" if blocker else "observing" if identity in snapshot["active"] else "discovery_pending"
    if snapshot["observed_at_ms"] is not None and now - snapshot["observed_at_ms"] > 2 * INTERVAL_MS and state in {"observing", "running", "recovering", "discovery_pending"}:
        state, blocker = "stale", "observer_heartbeat_stale"
    if not running or state in {"retired", "idle", "blocked", "stale"}:
        next_attempt = None
    observations = list(view["observations"])
    observations.append({"kind": "runtime", "state": state, "source": "local_task_observer",
        "observed_at_ms": snapshot["observed_at_ms"],
        "last_progress_at_ms": record["_facts"]["last_observed_at_ms"] or None,
        "next_attempt_at_ms": next_attempt, "blocker": blocker})
    for operation in record["operations"]:
        locator = operation["locator"]
        if locator["kind"] != "execution":
            continue
        observed_at = snapshot["last"].get(identity, {}).get(operation["id"], operation["observed_at_ms"]) if operation["id"] in outstanding else operation["observed_at_ms"]
        stale = records.pending(operation) and (observed_at is None or now - observed_at > 2 * INTERVAL_MS)
        observations.append({"kind": "execution", "state": "stale" if stale else (operation["result"] or {}).get("status", "unknown"),
            "source": f"execution:{locator['machine']}:{locator['execution_id']}", "observed_at_ms": observed_at,
            "last_progress_at_ms": operation["observed_at_ms"], "next_attempt_at_ms": None,
            "blocker": "Original state is last-known; no current result is verified." if stale else None})
    return {**view, "support": {**view["support"], "local_result_observation": True}, "observations": observations}
