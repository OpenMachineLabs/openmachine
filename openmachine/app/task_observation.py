"""Bounded original-result observation policy and read-only views; no operation dispatch."""

import errno
import random
import re
import time
from openmachine.foundation.sanitize import Fault
from openmachine.tasks import records, transitions

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
            transitions.material(record, now, "recovery", f"Original receipt observation failed: {code}; original work retained.")
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


def caller(events, record: dict, snapshot: dict, now: int) -> list[dict]:
    observation = {"kind": "caller", "state": "unsubscribed", "source": "task_events",
        "observed_at_ms": now, "last_progress_at_ms": None, "next_attempt_at_ms": None,
        "blocker": None}
    try:
        facts = events.facts(record["task_id"])
    except Fault as error:
        return [dict(observation, state="blocked", blocker=error.code)]
    if not facts:
        return [dict(observation, state="retired" if record["terminal"] is not None else "unsubscribed")]
    result = []
    for fact in facts:
        item = dict(observation, subscription_id=fact["subscription_id"], state="pending")
        delivery = fact["delivery"]
        if delivery is None and fact["connection"] is not None:
            connection = fact["connection"]
            ended = connection["attempts"] >= 60 or now >= connection["deadline_ms"]
            item.update(state="blocked" if ended else "pending",
                blocker="delivery_budget_exhausted" if ended else None,
                next_attempt_at_ms=None if ended else connection["next_attempt_ms"])
        if delivery is not None:
            budget = delivery["budget"]
            item.update(event_id=delivery["id"], handoff_id=delivery["payload"]["handoff_id"],
                accepted_at_ms=delivery["accepted_at_ms"], read_at_ms=delivery["read_at_ms"],
                attempts=budget["attempts"], deadline_ms=budget["deadline_ms"])
            current_handoff = (record["handoff"] or {}).get("id")
            item["pending_handoff_id"] = current_handoff if current_handoff != delivery["payload"]["handoff_id"] else None
            admitted = [op for op in record["operations"] if op["epoch"] > delivery["epoch"]
                and op["plan_revision"] == delivery["payload"]["plan_revision"]
                and op["admitted_at_ms"] >= delivery["occurred_at_ms"]]
            owner = record["control"]["owner"]
            claimed = (owner is not None and record["control"]["epoch"] > delivery["epoch"]
                       and current_handoff == delivery["payload"]["handoff_id"]
                       and record["plan"]["revision"] == delivery["payload"]["plan_revision"])
            if admitted:
                item.update(state="next_intent_admitted", operation_id=admitted[0]["id"],
                            last_progress_at_ms=admitted[0]["admitted_at_ms"])
            elif claimed:
                item.update(state="claimed", last_progress_at_ms=record["updated_at_ms"])
                if owner["expires_at_ms"] <= now:
                    item.update(state="blocked", blocker="continuation_claim_expired")
            elif (record["plan"]["revision"] != delivery["payload"]["plan_revision"]
                  or current_handoff != delivery["payload"]["handoff_id"]):
                item.update(state="superseded")
            elif delivery["accepted_at_ms"] is not None:
                item.update(state="read_observed" if delivery["read_at_ms"] else "delivered",
                    last_progress_at_ms=delivery["read_at_ms"] or delivery["accepted_at_ms"])
                if now >= min(fact["expires_at_ms"], delivery["accepted_at_ms"] + 900000):
                    item.update(state="blocked", blocker="claim_wait_expired")
            else:
                connection = fact["connection"]
                ended = now >= budget["deadline_ms"] or budget["attempts"] >= 60
                connection_ended = connection is not None and (now >= connection["deadline_ms"] or connection["attempts"] >= 60)
                blocker = delivery["blocker"] or ("delivery_budget_exhausted" if ended or connection_ended else None)
                item.update(state="blocked" if blocker else "retrying", blocker=blocker,
                    next_attempt_at_ms=None if blocker else max(budget["next_attempt_ms"], (connection or {}).get("next_attempt_ms", 0)))
        blocker = snapshot.get("event_errors", {}).get(record["task_id"])
        started = snapshot.get("deliveries", {}).get(fact["subscription_id"])
        if started is not None and now >= started + 10000:
            blocker = "callback_still_outstanding"
        if record["terminal"] is not None or record["control"]["stop_intent"] != "none":
            item.update(state="retired" if record["terminal"] else "paused", next_attempt_at_ms=None, blocker=None)
        elif now >= min(fact["expires_at_ms"], record["contract"]["expires_at_ms"]):
            item.update(state="blocked", next_attempt_at_ms=None, blocker="subscription_expired")
        elif blocker:
            item.update(state="blocked", next_attempt_at_ms=None, blocker=blocker)
        if (snapshot["state"] != "running" or snapshot["observed_at_ms"] is None
                or now - snapshot["observed_at_ms"] > 2 * INTERVAL_MS):
            item["next_attempt_at_ms"] = None
        result.append(item)
    return result


def project(view: dict, snapshot: dict, events, ssh_supported: bool) -> dict:
    if "record" not in view:
        return view
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
    observations = caller(events, record, snapshot, now) if events is not None else list(view["observations"])
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
        "events_delivery": events is not None,
        "ssh_result_observation": ssh_supported}, "observations": observations}
