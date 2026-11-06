"""Derived foreground task-stop policy; no mutation, cancellation, or delivery."""

import time

from openmachine.tasks import records

HANDOFF_GRACE_MS = 120000
MAX_WAIT_SECONDS = 30.0
WAIT_SLICE_SECONDS = 0.25
ATTENTION_REASONS = ("task_stopped", "task_authority_expired", "execution_unverified", "handoff_unconsumed")
_UNVERIFIED_RUNTIME = {"blocked", "stale", "stopped", "stopping"}


def now_ms() -> int:
    return time.time_ns() // 1000000


def _runtime(view: dict) -> dict | None:
    return next((item for item in reversed(view.get("observations", []))
                 if item.get("kind") == "runtime"), None)


def _resume_prompt(record: dict) -> str:
    identity = record["task_id"]
    return (f"Continue Open Machine task {identity}. Read the current task, foreground state, handoff, "
            "and original operation results first. Renew only your retained claim when eligible, or claim an "
            "eligible handoff under current guards before new effects. If neither is eligible, "
            "report the blocker. Do not replay completed, failed, partial, or unknown operations.")


def _operation(record: dict, pending: list[dict]) -> dict | None:
    if pending:
        return pending[-1]
    stage = record["control"]["current_stage_id"]
    return next((item for item in reversed(record["operations"]) if item["stage_id"] == stage), None)


def _foreground(view: dict, timestamp: int) -> dict:
    record = view["record"]
    control = record["control"]
    handoff = record["handoff"]
    owner = control["owner"]
    pending = [item for item in record["operations"] if records.pending(item)]
    local_pending = [item for item in pending if item["locator"]["kind"] in {"execution", "change"}]
    runtime = _runtime(view)
    runtime_state = None if runtime is None else runtime.get("state")
    operation = _operation(record, pending)
    common = {"task_id": record["task_id"], "stage_id": control["current_stage_id"],
              "operation_id": None if operation is None else operation["id"],
              "handoff_id": None if handoff is None else handoff["id"],
              "observed_at_ms": timestamp, "resume_prompt": _resume_prompt(record)}

    if record["terminal"] is not None:
        terminal = record["terminal"]
        return {**common, "status": "terminal", "reason": terminal["outcome"],
                "summary": terminal["summary"], "since_ms": terminal["finished_at_ms"],
                "age_ms": max(0, timestamp - terminal["finished_at_ms"]),
                "original_work_running": None if terminal["outcome"] == "abandoned" else False,
                "stop_foreground": True}

    if control["stop_intent"] != "none" or timestamp >= record["contract"]["expires_at_ms"]:
        stopped = control["stop_intent"] != "none"
        since = record["updated_at_ms"] if stopped else record["contract"]["expires_at_ms"]
        return {**common, "status": "attention_required",
                "reason": "task_stopped" if stopped else "task_authority_expired",
                "summary": "The task is paused or cancelling." if stopped else "The task authority expired.",
                "since_ms": since, "age_ms": max(0, timestamp - since),
                "original_work_running": None if pending else False, "stop_foreground": True}

    if local_pending and runtime_state in _UNVERIFIED_RUNTIME:
        since = (runtime or {}).get("observed_at_ms") or operation["admitted_at_ms"]
        blocker = (runtime or {}).get("blocker") or "original operation state is unverified"
        return {**common, "status": "attention_required", "reason": "execution_unverified",
                "summary": f"Open Machine cannot verify the original operation: {blocker}.",
                "since_ms": since, "age_ms": max(0, timestamp - since),
                "original_work_running": None, "stop_foreground": True}

    owner_expired = owner is not None and owner["expires_at_ms"] <= timestamp
    reliable_original = bool(local_pending) and runtime_state not in _UNVERIFIED_RUNTIME | {"idle", "retired"}
    if owner_expired:
        since = owner["expires_at_ms"]
        return {**common, "status": "renewal_required", "reason": "continuation_expired",
                "summary": "The continuation claim expired. Read original results and explicitly renew only the same "
                           "retained claim under current guards before new effects; this alone does not end the Chat turn.",
                "since_ms": since, "age_ms": max(0, timestamp - since),
                "original_work_running": True if reliable_original else None if pending else False,
                "stop_foreground": False}

    owner_live = owner is not None and owner["expires_at_ms"] > timestamp
    if handoff is not None and not owner_live and timestamp - handoff["created_at_ms"] >= HANDOFF_GRACE_MS:
        since = handoff["created_at_ms"] + HANDOFF_GRACE_MS
        return {**common, "status": "attention_required", "reason": "handoff_unconsumed",
                "summary": "A ready task handoff was not claimed within the foreground grace period. "
                           "End the interrupted Chat turn; a later user-authorized continuation may reclaim it.",
                "resume_prompt": f"Continue Open Machine task {record['task_id']}. On a subsequent explicit user request "
                    "after the interrupted turn has ended, read the current task, handoff and original operation results. "
                    "If handoff_unconsumed is the only remaining blocker, claim the current eligible handoff "
                    f"(observed handoff_id: {handoff['id']}) through control_task with exact task/plan revisions, "
                    "continuation epoch and a new update_id. The historical timeout alone does not require ending "
                    "that recovery turn before claiming. Do not claim automatically in the interrupted turn. "
                    "Keep all ownership, scope and authority guards; read back the claim before new effects. "
                    "If another blocker remains, report it. Do not replay completed, failed, partial, or unknown operations.",
                "since_ms": since, "age_ms": max(0, timestamp - since),
                "original_work_running": False, "stop_foreground": True}

    if handoff is not None:
        summaries = {"plan_ready": "The next task stage is ready to be claimed.",
                     "result_ready": "An original operation result is ready for review.",
                     "decision_required": "The task needs a decision before new work."}
        return {**common, "status": "ready", "reason": handoff["reason"],
                "summary": summaries[handoff["reason"]], "since_ms": handoff["created_at_ms"],
                "age_ms": max(0, timestamp - handoff["created_at_ms"]),
                "original_work_running": False, "stop_foreground": False}

    if pending or owner_live or control["state"] in {"planned", "running", "recovering"}:
        since = max([record["updated_at_ms"], *[item["admitted_at_ms"] for item in pending]])
        return {**common, "status": "running", "reason": "original_work_active",
                "summary": "Original task work is still active; a foreground wait does not extend its timeout.",
                "since_ms": since, "age_ms": max(0, timestamp - since),
                "original_work_running": bool(pending), "stop_foreground": False}

    return {**common, "status": "idle", "reason": "no_active_foreground_work",
            "summary": "No active original operation or actionable handoff is currently visible.",
            "since_ms": record["updated_at_ms"], "age_ms": max(0, timestamp - record["updated_at_ms"]),
            "original_work_running": False, "stop_foreground": False}


def project(view: dict, *, timestamp: int | None = None, wait_expired: bool = False) -> dict:
    if "record" not in view:
        return view
    timestamp = now_ms() if timestamp is None else timestamp
    foreground = _foreground(view, timestamp)
    if wait_expired and foreground["status"] in {"idle", "running"}:
        foreground = {**foreground, "status": "wait_expired", "reason": "foreground_wait_expired",
            "summary": "The bounded foreground wait ended. End this Chat turn; original work may continue independently.",
            "stop_foreground": True}
    return {**view, "foreground": foreground}
