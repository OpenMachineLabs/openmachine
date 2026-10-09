"""Pure task state transitions and active-work guards; no IO or dispatch."""

from openmachine.foundation.sanitize import Fault
from . import effects
from .records import MAX_ACTIVE_OPERATIONS, MAX_INTEGER, Operation, _unique, digest, pending


def stage_for(record: dict, identity: str | None = None) -> dict:
    wanted = record["control"]["current_stage_id"] if identity is None else identity
    for stage in record["plan"]["stages"]:
        if stage["id"] == wanted:
            return stage
    raise Fault("task_stage_conflict", "The selected stage does not exist in the current plan.")


def guards(record: dict, revision: int, plan_revision: int, epoch: int, *, allow_observations: bool = False) -> None:
    current = revision == record["revision"]
    if allow_observations and record["format"] >= 4:
        current = record["control_revision"] <= revision <= record["revision"]
    if not current or (plan_revision, epoch) != (record["plan"]["revision"], record["control"]["epoch"]):
        raise Fault("task_conflict", "Task, plan or continuation ownership changed; read the original task before a new action.",
                    {"task_id": record["task_id"], "task_revision": record["revision"],
                     "plan_revision": record["plan"]["revision"], "continuation_epoch": record["control"]["epoch"]})


def require_owner(record: dict, principal: str, now_ms: int) -> None:
    control = record["control"]
    if control["stop_intent"] != "none" or record["terminal"] is not None:
        raise Fault("task_stopped", "The original task is paused, cancelling or terminal; no new continuation is allowed.")
    if now_ms >= record["contract"]["expires_at_ms"]:
        raise Fault("task_authority_expired", "Task authority expired; read and explicitly revise its accepted scope.")
    owner = control["owner"]
    if owner is None or owner["principal_ref"] != principal or owner["expires_at_ms"] <= now_ms:
        raise Fault("task_owner_conflict", "A current explicit continuation claim is required; expiry does not prove original work stopped.")


def material(record: dict, now_ms: int, kind: str, summary: str, stage_id: str | None = None,
             operation_id: str | None = None) -> None:
    record["history"].append({"at_ms": now_ms, "kind": kind, "stage_id": stage_id, "operation_id": operation_id,
                              "summary": summary.encode("utf-8")[:256].decode("utf-8", errors="ignore")})


def handoff(record: dict, identity: str, now_ms: int, reason: str) -> None:
    control = record["control"]
    if (control["stop_intent"] != "none" or record["terminal"] is not None
            or control["current_stage_id"] is None or now_ms >= record["contract"]["expires_at_ms"]):
        record["handoff"] = None
        return
    stage = stage_for(record)
    record["handoff"] = {"id": digest([record["task_id"], identity, record["plan"]["revision"], reason])[:32],
        "plan_revision": record["plan"]["revision"], "source_sha256": stage["source_sha256"],
        "stage_id": stage["id"], "reason": reason, "created_at_ms": now_ms,
        "expires_at_ms": record["contract"]["expires_at_ms"]}


def reserve(record: dict, *, operation_limit: int = MAX_ACTIVE_OPERATIONS - 2,
            new_operation: dict) -> None:
    """Bound concurrent unresolved work and each admitted operation's future result."""
    # The working set contains every unresolved operation, including this new
    # one. Retained terminal rows and accumulated events consume no active slots.
    if sum(effects.unresolved(item) for item in record["operations"]) > operation_limit:
        raise Fault("task_capacity", "Active work capacity is reserved for original results and stopping; reconcile unresolved work before admitting more.")
    future = dict(new_operation, observed_at_ms=MAX_INTEGER, state="observed",
        result={"status": "cancelled", "receipt_sha256": "f" * 64, "summary": "x" * 256, "log_offset": MAX_INTEGER})
    if record["format"] >= 4:
        future["result"].update(failure_code="x" * 64, effect="effect_verified")
    if future["locator"]["kind"] == "github":
        future["locator"] = dict(future["locator"], number=MAX_INTEGER,
            parent_number=None if future["locator"]["object_type"] in {"review_thread", "merge"} else MAX_INTEGER)
    try:
        Operation.model_validate(future)
    except ValueError as error:
        raise Fault("task_capacity", "This new operation cannot retain its bounded future result; no work was dispatched.") from error


def stage_has_operations(record: dict, stage_id: str) -> bool:
    """Include historical operations omitted from the bounded working set."""
    return bool(record.get("_facts", {}).get("stage_operation_counts", {}).get(stage_id)
                or any(item["stage_id"] == stage_id for item in record["operations"]))


def finish_record(record: dict, outcome: str, acceptance: list[dict], limitations: str, now_ms: int) -> None:
    if any(effects.unresolved(operation) for operation in record["operations"]):
        raise Fault("task_effect_unresolved", "Original live, partial or unknown effects must be reconciled before terminal retirement.")
    record["control"].update(state=outcome, current_stage_id=None, owner=None, pending_decision=None,
                              epoch=record["control"]["epoch"] + 1)
    record["handoff"] = record["recovery"] = None
    record["terminal"] = {"outcome": outcome, "finished_at_ms": now_ms,
        "summary": f"Task {outcome}: {record['contract']['objective']}", "acceptance": acceptance, "limitations": limitations}
    material(record, now_ms, "terminal", f"Task {outcome}; original evidence retained.")


def acceptance_refs(record: dict, evidence: list[dict], *, complete: bool) -> None:
    seen = set()
    operations = {item["id"]: item for item in record["operations"]}
    for item in evidence:
        stage = stage_for(record, item["stage_id"])
        key = (item["stage_id"], item["operation_id"])
        if key in seen or stage["state"] != "accepted":
            raise Fault("task_evidence_invalid", "Final acceptance requires unique references to accepted stages.")
        seen.add(key)
        # An unaffected, already accepted stage deliberately retains its
        # original evidence across a plan revision. New selection is stricter.
        identity = item["operation_id"]
        if identity is None:
            if stage["evidence"]:
                raise Fault("task_evidence_invalid", "A non-command decision cannot replace existing operation evidence.")
        elif (identity not in stage["evidence"] or identity not in operations
                or effects.unresolved(operations[identity]) or operations[identity]["result"]["status"] != "succeeded"
                or operations[identity]["source_sha256"] != stage["source_sha256"]):
            raise Fault("task_evidence_invalid", "Final evidence must retain a validated, applicable original result.")
    if complete and {item[0] for item in seen} != {stage["id"] for stage in record["plan"]["stages"]}:
        raise Fault("task_evidence_invalid", "Completion requires acceptance evidence for every stage, not an exit code alone.")


def apply_control(record: dict, request: dict, principal: str, now_ms: int) -> None:
    """Pure state transition. Application supplies identity and verified domain evidence."""
    guards(record, request["expected_revision"], request["expected_plan_revision"], request["expected_epoch"])
    if record["terminal"] is not None:
        raise Fault("task_terminal", "Terminal history cannot be resumed or reclassified.")
    change, control = request["transition"], record["control"]
    action, identity = change["action"], request["update_id"]
    reason = change.get("reason", f"Continuation {action}.")
    if action in {"pause", "cancel"}:
        desired = "pause" if action == "pause" else "cancel"
        if control["stop_intent"] == "cancel" and desired == "pause":
            raise Fault("task_stopped", "A cancellation cannot be downgraded to a pause.")
        if control["stop_intent"] != desired:
            control.update(state="paused", stop_intent=desired, owner=None, epoch=control["epoch"] + 1,
                           pending_decision="Await original operation outcomes." if desired == "cancel" else None)
            record["handoff"] = None
            # Pause/cancel suppress continuation, not evidence collection for
            # already-owned work. Neither action resets its observation budget.
            if record["recovery"] is not None and record["recovery"]["category"] not in {"local_observation", "ssh_observation"}:
                record["recovery"] = None
            material(record, now_ms, "stopped", reason)
        if desired == "cancel" and not any(effects.unresolved(item) for item in record["operations"]):
            finish_record(record, "cancelled", [], "Explicit cancellation does not undo prior effects.", now_ms)
        return
    expired = now_ms >= record["contract"]["expires_at_ms"]
    if expired and action != "revise_plan":
        raise Fault("task_authority_expired", "Expired task authority cannot be extended by a continuation request.")
    if action == "resume":
        if control["state"] != "paused" or control["stop_intent"] != "pause":
            raise Fault("task_stopped", "Resume applies only to an explicitly paused, non-cancelling task.")
        control.update(state="waiting_for_ai", stop_intent="none", owner=None, epoch=control["epoch"] + 1,
                       pending_decision=None)
        handoff(record, identity, now_ms, "decision_required")
    elif action == "claim":
        pending_handoff = record["handoff"]
        if (control["stop_intent"] != "none" or pending_handoff is None
                or pending_handoff["id"] != change["handoff_id"] or pending_handoff["expires_at_ms"] <= now_ms):
            raise Fault("task_handoff_conflict", "Read the current eligible handoff before claiming continuation.")
        if control["owner"] is not None and control["owner"]["expires_at_ms"] > now_ms:
            raise Fault("task_owner_conflict", "Another unexpired claim owns continuation; read or explicitly release it.")
        control.update(epoch=control["epoch"] + 1, owner={"principal_ref": principal, "claim_id": identity,
            "expires_at_ms": min(now_ms + 300000, pending_handoff["expires_at_ms"], record["contract"]["expires_at_ms"])})
        control["state"] = "running" if any((item["result"] or {}).get("status") == "running" for item in record["operations"]) else "planned"
        # Reading and claiming do not resolve the handoff; a later admitted intent does.
    elif action == "renew":
        owner = control["owner"]
        if control["stop_intent"] != "none":
            raise Fault("task_stopped", "Paused or cancelling work cannot renew continuation ownership.")
        if (owner is None or owner["principal_ref"] != principal
                or owner["claim_id"] != change["claim_id"]):
            raise Fault("task_owner_conflict", "Renew only the same retained claim; release or replacement ends its ownership.")
        # Explicit renewal may reattach the same expired claim while task authority
        # remains valid. It never changes the claim identity, epoch or original work.
        owner["expires_at_ms"] = min(now_ms + 300000, record["contract"]["expires_at_ms"])
    else:
        if action == "revise_plan" and expired:
            if control["stop_intent"] == "cancel":
                raise Fault("task_stopped", "Explicit reauthorization cannot undo cancellation.")
            owner = control["owner"]
            if owner is not None and owner["principal_ref"] != principal and owner["expires_at_ms"] > now_ms:
                raise Fault("task_owner_conflict", "Another live owner must release before explicit reauthorization.")
            contract = change["contract"]
            if (contract["expires_at_ms"] <= now_ms
                    or not set(contract["authority_refs"]) - set(record["contract"]["authority_refs"])):
                raise Fault("task_authority_expired", "Explicit reauthorization needs a future expiry and a new supplied authority reference.")
        else:
            require_owner(record, principal, now_ms)
        if action == "release":
            control.update(owner=None, epoch=control["epoch"] + 1, state="waiting_for_ai")
            if record["handoff"] is None:
                handoff(record, identity, now_ms, "decision_required")
        elif action == "revise_plan":
            contract = change["contract"]
            if contract["expires_at_ms"] <= now_ms:
                raise Fault("task_authority_expired", "A revised contract requires a future explicit authority expiry.")
            outstanding = [item for item in record["operations"] if effects.unresolved(item)]
            if any(item["locator"]["kind"] != "github" for item in outstanding):
                raise Fault("task_effect_unresolved", "Reconcile outstanding local effects before replacing their plan.")
            if outstanding:
                repositories = {repo.casefold() for repo in contract["repositories"]}
                bindings = contract.get("github_connections", {})
                for item in outstanding:
                    locator = item["locator"]
                    repository = locator["repository"].casefold()
                    binding = locator.get("connection")
                    # Admission may select the first alias; none may substitute another account.
                    matching = [value for repo, value in bindings.items() if repo.casefold() == repository]
                    if (repository not in repositories or binding is None or not matching
                            or any(value != binding for value in matching)):
                        raise Fault("task_effect_unresolved", "An unresolved GitHub effect must retain its original repository and native account binding.")
            old = {stage["id"]: stage for stage in record["plan"]["stages"]}
            definitions = change["stages"]
            retained_order = [stage["id"] for stage in definitions if stage["id"] in old]
            if retained_order != list(old):
                raise Fault("task_plan_conflict", "Retain original stage IDs and their order; never remove or reassign historical stages.")
            affected = set(change["invalidated_stage_ids"])
            if not affected <= old.keys():
                raise Fault("task_plan_conflict", "Explicit invalidation must identify retained stages.")
            required = {stage["id"] for stage in definitions if stage["id"] in old
                        and stage != {key: old[stage["id"]][key] for key in stage}}
            stable = {"sources", "expires_at_ms", "authority_refs"}
            if any(record["contract"].get(key) != change["contract"].get(key)
                   for key in record["contract"].keys() | change["contract"].keys() if key not in stable):
                required.update(old)
            while True:
                prior = set(required)
                for stage in [*old.values(), *definitions]:
                    if set(stage["depends_on"]) & (required | affected) and stage["id"] in old:
                        required.add(stage["id"])
                if prior == required:
                    break
            if not required <= affected:
                raise Fault("task_plan_conflict", "Invalidate every changed stage and affected dependent, preserving unaffected evidence.")
            stages = [dict(stage, state="invalidated" if stage["id"] in affected else old[stage["id"]]["state"]
                           if stage["id"] in old else "pending", evidence=[] if stage["id"] in affected or stage["id"] not in old
                           else old[stage["id"]]["evidence"]) for stage in definitions]
            record["contract"] = change["contract"]
            # A newly accepted plan must not retain the retired registration alone.
            if "recovery_policy" not in contract:
                record.pop("chat_registration", None)
            record["plan"] = {"revision": record["plan"]["revision"] + 1, "stages": stages}
            paused = control["stop_intent"] == "pause"
            decision = control["pending_decision"] if paused or outstanding else None
            if outstanding and decision is None:
                decision = "Reconcile the original external outcome before related work."
            control.update(current_stage_id=next((s["id"] for s in stages if s["state"] != "accepted"), stages[-1]["id"]),
                           owner=None, epoch=control["epoch"] + 1, state="paused" if paused else "waiting_for_ai",
                           pending_decision=decision)
            handoff(record, identity, now_ms, "decision_required" if outstanding else "plan_ready")
        elif action == "record_decision":
            stage = stage_for(record, change["stage_id"])
            if stage["id"] != control["current_stage_id"] or stage["state"] == "accepted":
                raise Fault("task_stage_conflict", "Decide the current remaining stage; accepted work is not silently reopened.")
            _unique(change["evidence"])
            operations = {item["id"]: item for item in record["operations"]}
            for key in change["evidence"]:
                operation = operations.get(key)
                if (operation is None or operation["stage_id"] != stage["id"]
                        or operation["plan_revision"] != record["plan"]["revision"]
                        or operation["source_sha256"] != stage["source_sha256"] or pending(operation)
                        or change["outcome"] == "accepted" and operation["result"]["status"] != "succeeded"):
                    raise Fault("task_evidence_invalid", "Decision evidence must identify applicable, verified original results.")
            if change["outcome"] == "accepted":
                outstanding = [item for item in record["operations"] if effects.unresolved(item)]
                if outstanding and (not change["evidence"] or any(
                        effects.conflicts(record, item, record, operations[key])
                        for item in outstanding for key in change["evidence"])):
                    raise Fault("task_effect_unresolved", "Acceptance needs independent verified evidence that does not overlap an unresolved original effect.")
                if not change["evidence"] and stage_has_operations(record, stage["id"]):
                    raise Fault("task_evidence_invalid", "An operation-backed stage requires original evidence, not an empty decision.")
                if any(stage_for(record, key)["state"] != "accepted" for key in stage["depends_on"]):
                    raise Fault("task_stage_conflict", "Dependencies must be accepted before this stage.")
            stage.update(state=change["outcome"], evidence=change["evidence"])
            control["pending_decision"] = reason if change["outcome"] == "blocked" else None
            control["current_stage_id"] = next((s["id"] for s in record["plan"]["stages"] if s["state"] != "accepted"), stage["id"])
            control["state"] = "waiting_for_ai"
            handoff(record, identity, now_ms, "decision_required" if change["outcome"] == "blocked" else "plan_ready")
        elif action == "finish":
            acceptance_refs(record, change["acceptance"], complete=change["outcome"] == "completed")
            finish_record(record, change["outcome"], change["acceptance"], change["limitations"], now_ms)
            return
    material(record, now_ms, "plan_changed" if action == "revise_plan" else "decision", reason,
             change.get("stage_id"))
