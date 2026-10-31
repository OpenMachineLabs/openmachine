"""Original Task receipt reads and interpretation; no admission or effect dispatch."""

import os
import re
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from openmachine.tasks import records


def receipt_result(status: str, facts: dict | None = None, summary: str = "", *, failure_code=None, effect=None) -> dict:
    result = {"status": status, "receipt_sha256": records.digest(facts) if facts is not None else None,
            "summary": summary or f"Original domain outcome: {status}.", "log_offset": None}
    if failure_code is not None:
        result["failure_code"] = failure_code
    if effect is not None:
        result["effect"] = effect
    return result


def observation(operation: dict, observed: dict) -> tuple[dict, dict]:
    locator = operation["locator"]
    status = observed.get("status")
    if status == "retained_task_evidence":
        return operation["result"], locator
    if locator["kind"] == "github":
        if operation.get("result") is not None and not records.pending(operation):
            return operation["result"], locator
        if status not in {"verified", "merged_verified"} and operation.get("result") is not None:
            # An unproven mutable destination snapshot cannot rewrite the
            # original uncertainty or discard its retained failure phase.
            return operation["result"], locator
        receipt = observed.get("receipt") or {}
        if locator["object_type"] == "merge":
            receipt = {key: (receipt or observed).get(key) for key in ("number", "head_sha", "merge_commit_sha")}
        number = receipt.get("number" if locator["object_type"] in {"issue", "pull_request", "merge"} else "id")
        enriched = dict(locator, number=number) if type(number) is int and number > 0 else locator
        if status in {"verified", "merged_verified"}:
            return receipt_result("succeeded", receipt), enriched
        if status == "observed" and operation["result"] is not None and not records.pending(operation):
            return operation["result"], enriched
        if status == "merge_blocked":
            return receipt_result("failed", summary="Original merge was blocked before dispatch."), enriched
        result = receipt_result("unknown", receipt or None, "Original provider outcome is unresolved; no request was repeated.")
        if operation.get("effect_scope") is not None:
            result["effect"] = "effect_unknown"
            code = observed.get("error_code")
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", code):
                result["failure_code"] = code
        return result, enriched
    if locator["kind"] == "change":
        outcome = {"applied": "succeeded", "unchanged": "succeeded", "rejected": "failed", "partial": "partial"}.get(status, "unknown")
        facts = {key: observed.get(key) for key in ("change_id", "request_sha256", "revision", "status", "plan_sha256", "effects", "error")}
        return receipt_result(outcome, facts), locator
    if locator["control_seq"] is not None:
        receipt = observed["control_result"]
        outcome = {"applied": "succeeded", "rejected": "failed", "partial": "partial"}.get(receipt["status"], "unknown")
        return receipt_result(outcome, receipt), locator
    outcome = {"accepted": "running", "running": "running", "finishing": "running", "succeeded": "succeeded",
               "failed": "failed", "timed_out": "failed", "cancelled": "cancelled"}.get(status, "unknown")
    if outcome in {"succeeded", "failed", "cancelled"} and (observed.get("cleanup", {}).get("status") != "verified"
            or outcome == "succeeded" and observed.get("exit_code") != 0):
        outcome = "unknown"
    result = receipt_result(outcome)
    result["receipt_sha256"] = records.execution_receipt_digest(observed)
    result["log_offset"] = (operation["result"] or {}).get("log_offset") or 0
    return result, locator


class TaskReceipts:
    def __init__(self, state, config, machines, github_call):
        self.state = state
        self.config = config
        self.machines = machines
        self.github_call = github_call

    def read(self, operation: dict, arguments: dict | None = None, *, verify: bool = False, cancelled=None) -> dict:
        locator = operation["locator"]
        if locator["kind"] == "github":
            if operation.get("result") is not None and not records.pending(operation):
                # A terminal provider receipt proves historical effect. Fresh
                # merge/state gates belong to the subsequent owner operation.
                return {"status": "retained_task_evidence", "locator": locator,
                        "result": operation["result"], "current_state_observed": False}
            proof = ({"acknowledgement_sha256": (operation.get("result") or {}).get("receipt_sha256")}
                     if locator["object_type"] == "review_thread" else {})
            observed = self.github_call("reconcile", dict(locator=locator, request=arguments, **proof),
                                        binding=locator.get("connection"), historical_unbound="connection" not in locator)
            return observed
        target = self.config.machine(locator["machine"])
        if (target.ssh_identity or None) != locator["target_identity"]:
            raise Fault("task_target_changed", "The original target identity no longer matches; do not follow another host.")
        if locator["kind"] == "change":
            observed = self.machines.read_workspace_change(locator["machine"], locator["change_id"], observe_current=verify, cancelled=cancelled)
            if verify:
                current, plan = observed.get("current") or {}, observed.get("plan") or {}
                if observed.get("format") == 2:
                    expected = {item["path"]: item["after_sha256"] for item in plan.get("files", [])}
                    actual = {item["path"]: item.get("sha256") for item in current.get("files", [])}
                    matches = bool(expected) and current.get("complete") is True and expected == actual
                else:
                    matches = current.get("sha256") == plan.get("after_sha256") and "error" not in current and bool(plan)
                if not matches:
                    raise Fault("task_evidence_changed", "Current files differ from original accepted change evidence.")
            return observed
        observed = self.machines.read_execution(locator["machine"], locator["execution_id"], max_bytes=1, cancelled=cancelled)
        if locator["control_seq"] is not None:
            last = (observed.get("control") or {}).get("last")
            if last is None or last["seq"] != locator["control_seq"]:
                if operation["result"] is not None and not records.pending(operation):
                    return {"status": "retained_task_evidence", "execution_id": locator["execution_id"], "control_result": None}
                raise Fault("task_evidence_unavailable", "The original control receipt is no longer retained; never resend its input.")
            return {**observed, "control_result": last}
        return observed

    def unstarted_local(self, operation: dict) -> bool:
        # A missing receipt alone never proves non-execution. Only the recorded
        # local start rejection and its original free launch lock can do so.
        locator = operation["locator"]
        if (locator["kind"] != "execution" or locator["control_seq"] is not None
                or locator["target_identity"] is not None or operation["id"] != locator["execution_id"]
                or self.config.machine(locator["machine"]).transport != "local"):
            return False
        identity = locator["execution_id"]
        try:
            with lease(self.state / "locks" / f"{identity}.lock", create=False):
                return not os.path.lexists(self.state / "executions" / identity)
        except (Fault, OSError):
            return False
