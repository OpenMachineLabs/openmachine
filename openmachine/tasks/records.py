"""Bounded task contracts and canonical records; no execution or state mutation."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from openmachine.foundation.sanitize import Fault, redact

MAX_RECORD = 65536
MAX_ACCEPTANCE = 49152
MAX_INTEGER = 2**53 - 1
READ_FORMATS = (1,)


def _text(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) > limit or "\0" in value or redact(value) != value:
        raise ValueError("text exceeds its UTF-8 bound or contains credentials")
    return value


def _line(value: str, *, title: bool = False) -> str:
    if not value or title and not value.strip() or any(ord(char) < 32 for char in value) or redact(value) != value:
        raise ValueError("expected nonempty single-line metadata without credentials")
    return value


Text128 = Annotated[str, Field(max_length=128), AfterValidator(lambda v: _text(v, 128))]
Text256 = Annotated[str, Field(max_length=256), AfterValidator(lambda v: _text(v, 256))]
Text512 = Annotated[str, Field(max_length=512), AfterValidator(lambda v: _text(v, 512))]
Text1024 = Annotated[str, Field(max_length=1024), AfterValidator(lambda v: _text(v, 1024))]
Text2048 = Annotated[str, Field(max_length=2048), AfterValidator(lambda v: _text(v, 2048))]
Title = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(lambda value: _line(value, title=True))]
Anchor = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_line)]
TaskID = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
Name = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
ID = Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
SHA = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
Head = Annotated[str, Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")]
Integer = Annotated[int, Field(strict=True, ge=0, le=MAX_INTEGER)]
Revision = Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]
Repository = Annotated[str, Field(max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")]


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _unique(values: list) -> None:
    if len(values) != len(set(values)):
        raise ValueError("duplicate reference")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Origin(Model):
    request_ref: Text1024
    request_sha256: SHA
    objective: Text1024

    @model_validator(mode="after")
    def nonempty(self):
        if not self.request_ref.strip() or not self.objective.strip():
            raise ValueError("origin needs the original request and objective")
        return self


class Target(Model):
    machine: Name
    workspace: Name
    workspace_identity: SHA
    head_sha: Head | None


class Source(Model):
    locator: Annotated[Text512, Field(min_length=1)]
    sha256: SHA


class Contract(Model):
    objective: Text1024
    scope: Text1024
    exclusions: Text1024
    acceptance: Text1024
    stop_condition: Text1024
    authority_refs: Annotated[list[Annotated[Text512, Field(min_length=1)]], Field(min_length=1, max_length=8)]
    targets: Annotated[list[Target], Field(max_length=4)]
    repositories: Annotated[list[Repository], Field(max_length=8)]
    tools: Annotated[list[Name], Field(max_length=16)]
    sources: Annotated[list[Source], Field(max_length=16)]
    expires_at_ms: Integer

    @model_validator(mode="after")
    def bindings(self):
        if not all(value.strip() for value in (self.objective, self.acceptance, self.stop_condition)):
            raise ValueError("objective, acceptance and stop condition must be nonempty")
        for values in (self.authority_refs, self.repositories, self.tools,
                       [(t.machine, t.workspace) for t in self.targets], [s.locator for s in self.sources]):
            _unique(values)
        if any(repo.split("/")[1] in {".", ".."} for repo in self.repositories):
            raise ValueError("invalid repository")
        return self


class StageDefinition(Model):
    id: TaskID
    depends_on: Annotated[list[TaskID], Field(max_length=16)]
    result: Annotated[Text512, Field(min_length=1)]
    verification: Annotated[Text512, Field(min_length=1)]
    source_refs: Annotated[list[Text512], Field(max_length=16)]
    source_sha256: SHA


class Stage(StageDefinition):
    state: Literal["pending", "running", "blocked", "accepted", "invalidated"]
    evidence: Annotated[list[ID], Field(max_length=64)]


def _stages(contract: Contract, stages: list[StageDefinition]) -> None:
    sources = {source.locator: source.model_dump() for source in contract.sources}
    seen = set()
    for stage in stages:
        _unique(stage.depends_on)
        _unique(stage.source_refs)
        if stage.id in seen or not set(stage.depends_on) <= seen or not set(stage.source_refs) <= sources.keys():
            raise ValueError("stages need unique IDs, backward dependencies and existing sources")
        selected = [sources[key] for key in sorted(stage.source_refs)]
        if stage.source_sha256 != digest(selected):
            raise ValueError("stage source hash does not match its selected canonical sources")
        seen.add(stage.id)


class CreateRequest(Model):
    task_id: TaskID
    update_id: ID
    title: Title
    anchors: Annotated[list[Anchor], Field(max_length=16)]
    origin: Origin
    contract: Contract
    stages: Annotated[list[StageDefinition], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def plan(self):
        self.anchors = sorted(set(self.anchors))
        _stages(self.contract, self.stages)
        return self


class Plan(Model):
    revision: Revision
    stages: Annotated[list[Stage], Field(min_length=1, max_length=16)]


class Owner(Model):
    principal_ref: Annotated[Text256, Field(min_length=1)]
    claim_id: ID
    expires_at_ms: Integer


class Control(Model):
    state: Literal["planned", "running", "recovering", "waiting_for_ai", "waiting_for_user", "paused", "completed", "failed", "cancelled"]
    current_stage_id: TaskID | None
    stop_intent: Literal["none", "pause", "cancel"]
    epoch: Integer
    owner: Owner | None
    pending_decision: Text512 | None


class ExecutionLocator(Model):
    kind: Literal["execution"]
    machine: Name
    target_identity: SHA | None
    execution_id: ID
    control_seq: Integer | None


class ChangeLocator(Model):
    kind: Literal["change"]
    machine: Name
    target_identity: SHA | None
    change_id: ID


class GitHubLocator(Model):
    kind: Literal["github"]
    repository: Repository
    object_type: Literal["issue", "pull_request", "comment", "review", "merge"]
    number: Revision | None
    parent_number: Revision | None
    anchor: Annotated[Text128, Field(min_length=1)]


class Result(Model):
    status: Literal["running", "succeeded", "failed", "cancelled", "partial", "unknown"]
    receipt_sha256: SHA | None
    summary: Text256
    log_offset: Integer | None


class Operation(Model):
    id: ID
    stage_id: TaskID
    plan_revision: Revision
    epoch: Integer
    request_sha256: SHA
    source_sha256: SHA
    locator: Annotated[ExecutionLocator | ChangeLocator | GitHubLocator, Field(discriminator="kind")]
    admitted_at_ms: Integer
    state: Literal["admitted", "observed", "unknown"]
    observed_at_ms: Integer | None
    result: Result | None

    @model_validator(mode="after")
    def bounds(self):
        if len(canonical(self.model_dump())) > 1536 or len(canonical(self.locator.model_dump())) > 768:
            raise ValueError("operation or locator exceeds its encoded bound")
        if self.state == "observed" and (self.observed_at_ms is None or self.result is None):
            raise ValueError("observed operation needs original result evidence")
        if self.observed_at_ms is not None and self.observed_at_ms < self.admitted_at_ms:
            raise ValueError("observation precedes admission")
        return self


class History(Model):
    at_ms: Integer
    kind: Literal["accepted", "plan_changed", "decision", "recovery", "stopped", "terminal"]
    stage_id: TaskID | None
    operation_id: ID | None
    summary: Text256


class Handoff(Model):
    id: ID
    plan_revision: Revision
    source_sha256: SHA
    stage_id: TaskID
    reason: Literal["plan_ready", "result_ready", "decision_required"]
    created_at_ms: Integer
    expires_at_ms: Integer


class Recovery(Model):
    episode_id: ID
    category: Literal["local_observation", "ssh_observation", "runtime_readiness", "delivery", "claim"]
    started_at_ms: Integer
    deadline_ms: Integer
    attempts: Integer
    next_attempt_ms: Integer
    reason_code: Text128
    connection_ref: Text256 | None


class Acceptance(Model):
    stage_id: TaskID
    operation_id: ID | None


class Terminal(Model):
    outcome: Literal["completed", "failed", "cancelled"]
    finished_at_ms: Integer
    summary: Text2048
    acceptance: Annotated[list[Acceptance], Field(max_length=64)]
    limitations: Text2048


class LastUpdate(Model):
    id: ID
    request_sha256: SHA


class Record(Model):
    format: Annotated[int, Field(strict=True, ge=1, le=1)]
    task_id: TaskID
    revision: Revision
    title: Title
    anchors: Annotated[list[Anchor], Field(max_length=16)]
    created_at_ms: Integer
    updated_at_ms: Integer
    origin: Origin
    contract: Contract
    plan: Plan
    control: Control
    operations: Annotated[list[Operation], Field(max_length=64)]
    history: Annotated[list[History], Field(max_length=64)]
    handoff: Handoff | None
    recovery: Recovery | None
    terminal: Terminal | None
    last_update: LastUpdate

    @model_validator(mode="after")
    def references(self):
        _stages(self.contract, self.plan.stages)
        if self.anchors != sorted(set(self.anchors)) or self.created_at_ms > self.updated_at_ms or self.plan.revision > self.revision:
            raise ValueError("invalid record ordering")
        stages = {stage.id: stage for stage in self.plan.stages}
        operations = {operation.id: operation for operation in self.operations}
        if len(operations) != len(self.operations) or self.control.current_stage_id is not None and self.control.current_stage_id not in stages:
            raise ValueError("invalid current stage or duplicate operation")
        for operation in self.operations:
            if (operation.stage_id not in stages or operation.plan_revision > self.plan.revision
                    or operation.epoch > self.control.epoch or not self.created_at_ms <= operation.admitted_at_ms <= self.updated_at_ms
                    or operation.observed_at_ms is not None and operation.observed_at_ms > self.updated_at_ms):
                raise ValueError("invalid operation binding")
            if operation.plan_revision == self.plan.revision and operation.source_sha256 != stages[operation.stage_id].source_sha256:
                raise ValueError("operation source binding mismatch")
        for stage in self.plan.stages:
            _unique(stage.evidence)
            if any(key not in operations or operations[key].stage_id != stage.id for key in stage.evidence):
                raise ValueError("stage evidence must reference its original operations")
        previous = self.created_at_ms
        for event in self.history:
            if (not previous <= event.at_ms <= self.updated_at_ms or event.stage_id is not None and event.stage_id not in stages
                    or event.operation_id is not None and event.operation_id not in operations):
                raise ValueError("invalid material history reference")
            previous = event.at_ms
        if self.control.owner is not None and self.control.owner.expires_at_ms > self.contract.expires_at_ms:
            raise ValueError("ownership outlives task authority")
        if self.handoff is not None:
            handoff = self.handoff
            if (handoff.stage_id not in stages or handoff.plan_revision != self.plan.revision
                    or handoff.source_sha256 != stages[handoff.stage_id].source_sha256
                    or not self.created_at_ms <= handoff.created_at_ms <= self.updated_at_ms
                    or not handoff.created_at_ms < handoff.expires_at_ms <= self.contract.expires_at_ms):
                raise ValueError("invalid pending handoff binding")
        if self.recovery is not None:
            recovery = self.recovery
            if not self.created_at_ms <= recovery.started_at_ms <= recovery.next_attempt_ms <= recovery.deadline_ms:
                raise ValueError("invalid recovery deadline")
        is_terminal = self.control.state in {"completed", "failed", "cancelled"}
        if is_terminal != (self.terminal is not None):
            raise ValueError("terminal control and final record must agree")
        if self.terminal is not None:
            terminal = self.terminal
            if (terminal.outcome != self.control.state or self.handoff is not None or self.control.owner is not None
                    or not self.created_at_ms <= terminal.finished_at_ms <= self.updated_at_ms
                    or len(canonical(terminal.model_dump())) > 4096):
                raise ValueError("invalid terminal record")
            for evidence in terminal.acceptance:
                if (evidence.stage_id not in stages or evidence.operation_id is not None and
                        (evidence.operation_id not in operations or operations[evidence.operation_id].stage_id != evidence.stage_id)):
                    raise ValueError("invalid terminal acceptance reference")
        return self


def normalize_create(value: dict) -> dict:
    try:
        if len(canonical(value)) > MAX_ACCEPTANCE:
            raise ValueError("creation budget exceeded")
        result = CreateRequest.model_validate(value).model_dump()
        if len(canonical(result)) > MAX_ACCEPTANCE:
            raise ValueError("creation budget exceeded")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise Fault("invalid_task_contract", "Supply the bounded S0 task contract, exact stage/source bindings and explicit authority references; no runtime facts.") from error


def new_record(request: dict, now_ms: int) -> dict:
    contract = request["contract"]
    if not now_ms < contract["expires_at_ms"]:
        raise Fault("task_authority_expired", "Task creation requires a future explicit authority expiry.")
    stages = [dict(stage, state="pending", evidence=[]) for stage in request["stages"]]
    first = stages[0]
    value = {"format": 1, "task_id": request["task_id"], "revision": 1, "title": request["title"],
             "anchors": request["anchors"], "created_at_ms": now_ms, "updated_at_ms": now_ms,
             "origin": request["origin"], "contract": contract, "plan": {"revision": 1, "stages": stages},
             "control": {"state": "waiting_for_ai", "current_stage_id": first["id"], "stop_intent": "none",
                         "epoch": 0, "owner": None, "pending_decision": None},
             "operations": [], "history": [{"at_ms": now_ms, "kind": "accepted", "stage_id": None,
                 "operation_id": None, "summary": "Accepted the declared task contract; explicit ownership and intent admission are required."}],
             "handoff": {"id": digest([request["task_id"], request["update_id"]])[:32], "plan_revision": 1,
                         "source_sha256": first["source_sha256"], "stage_id": first["id"], "reason": "plan_ready",
                         "created_at_ms": now_ms, "expires_at_ms": contract["expires_at_ms"]},
             "recovery": None, "terminal": None,
             "last_update": {"id": request["update_id"], "request_sha256": digest({"method": "create_task", **request})}}
    try:
        result = Record.model_validate(value).model_dump()
        if len(canonical(result)) > MAX_ACCEPTANCE:
            raise ValueError("initial record leaves no result reserve")
        return result
    except (ValueError, TypeError, UnicodeError) as error:
        raise Fault("invalid_task_contract", "The initial canonical record must fit 48 KiB with 16 KiB retained for later evidence.") from error


class TaskContext(Model):
    task_id: TaskID
    task_revision: Revision
    plan_revision: Revision
    continuation_epoch: Integer
    operation_id: ID


class Claim(Model):
    action: Literal["claim"]
    handoff_id: ID


class ReasonedControl(Model):
    action: Literal["release", "pause", "resume", "cancel"]
    reason: Annotated[Text512, Field(min_length=1)]


class RevisePlan(Model):
    action: Literal["revise_plan"]
    contract: Contract
    stages: Annotated[list[StageDefinition], Field(min_length=1, max_length=16)]
    invalidated_stage_ids: Annotated[list[TaskID], Field(max_length=16)]
    reason: Annotated[Text512, Field(min_length=1)]

    @model_validator(mode="after")
    def references(self):
        _stages(self.contract, self.stages)
        _unique(self.invalidated_stage_ids)
        return self


class RecordDecision(Model):
    action: Literal["record_decision"]
    stage_id: TaskID
    outcome: Literal["accepted", "blocked"]
    evidence: Annotated[list[ID], Field(max_length=64)]
    reason: Annotated[Text512, Field(min_length=1)]

    @model_validator(mode="after")
    def references(self):
        _unique(self.evidence)
        return self


class Finish(Model):
    action: Literal["finish"]
    outcome: Literal["completed", "failed"]
    acceptance: Annotated[list[Acceptance], Field(max_length=64)]
    limitations: Text2048


Transition = Annotated[Claim | ReasonedControl | RevisePlan | RecordDecision | Finish, Field(discriminator="action")]


class ControlRequest(Model):
    task_id: TaskID
    expected_revision: Revision
    expected_plan_revision: Revision
    expected_epoch: Integer
    update_id: ID
    transition: Transition


def normalize_control(value: dict) -> dict:
    try:
        if len(canonical(value)) > MAX_ACCEPTANCE:
            raise ValueError("control request exceeds its bound")
        return ControlRequest.model_validate(value).model_dump()
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise Fault("invalid_task_control", "Supply the exact bounded control transition and observed revisions; no owner, timestamps or runtime results.") from error


def normalize_context(value: dict) -> dict:
    try:
        return TaskContext.model_validate(value).model_dump()
    except (ValueError, TypeError, UnicodeError) as error:
        raise Fault("invalid_task_context", "Supply the original task, task/plan revisions, continuation epoch and operation ID.") from error


def pending(operation: dict) -> bool:
    result = operation["result"]
    return operation["state"] != "observed" or result is None or result["status"] in {"running", "unknown"}


def stage_for(record: dict, identity: str | None = None) -> dict:
    wanted = record["control"]["current_stage_id"] if identity is None else identity
    for stage in record["plan"]["stages"]:
        if stage["id"] == wanted:
            return stage
    raise Fault("task_stage_conflict", "The selected stage does not exist in the current plan.")


def guards(record: dict, revision: int, plan_revision: int, epoch: int) -> None:
    if (revision, plan_revision, epoch) != (record["revision"], record["plan"]["revision"], record["control"]["epoch"]):
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


def reserve(record: dict, *, operation_limit: int = 62, new_work: bool = True) -> None:
    """Bound new work while retaining room for original results, decisions and stop."""
    remaining = sum(stage["state"] != "accepted" for stage in record["plan"]["stages"])
    if (new_work and (len(canonical(record)) > MAX_ACCEPTANCE or len(record["operations"]) > operation_limit)
            or len(record["history"]) + remaining + 3 > 64):
        raise Fault("task_capacity", "Task capacity is reserved for original results and stopping; no new work was admitted.")
    # Worst-case enrichment of unresolved operations, not synthetic persisted facts.
    extra = 0
    for operation in record["operations"]:
        if pending(operation):
            future = dict(operation, observed_at_ms=MAX_INTEGER, state="observed",
                result={"status": "cancelled", "receipt_sha256": "f" * 64, "summary": "x" * 256, "log_offset": MAX_INTEGER})
            if future["locator"]["kind"] == "github":
                future["locator"] = dict(future["locator"], number=MAX_INTEGER, parent_number=MAX_INTEGER)
            extra += max(0, len(canonical(future)) - len(canonical(operation)))
    # Final record, pending decision, handoff, remaining material decisions and evidence references.
    if len(canonical(record)) + extra + 4096 + (3072 if new_work else 512) + 768 + (remaining + 3) * 384 + 2240 > MAX_RECORD:
        raise Fault("task_capacity", "The admitted work must leave bounded final-evidence capacity; preserve current history.")


def finish_record(record: dict, outcome: str, acceptance: list[dict], limitations: str, now_ms: int) -> None:
    if any(pending(operation) for operation in record["operations"]):
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
        identity = item["operation_id"]
        if identity is None:
            if stage["evidence"]:
                raise Fault("task_evidence_invalid", "A non-command decision cannot replace existing operation evidence.")
        elif (identity not in stage["evidence"] or identity not in operations
                or pending(operations[identity]) or operations[identity]["result"]["status"] != "succeeded"
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
        if desired == "cancel" and not any(pending(item) for item in record["operations"]):
            finish_record(record, "cancelled", [], "Explicit cancellation does not undo prior effects.", now_ms)
        return
    if now_ms >= record["contract"]["expires_at_ms"]:
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
    else:
        require_owner(record, principal, now_ms)
        if action == "release":
            control.update(owner=None, epoch=control["epoch"] + 1, state="waiting_for_ai")
            if record["handoff"] is None:
                handoff(record, identity, now_ms, "decision_required")
        elif action == "revise_plan":
            if any(pending(item) for item in record["operations"]):
                raise Fault("task_effect_unresolved", "Reconcile outstanding original effects before replacing their plan.")
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
            if any(record["contract"][key] != change["contract"][key] for key in record["contract"] if key not in stable):
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
            record["plan"] = {"revision": record["plan"]["revision"] + 1, "stages": stages}
            control.update(current_stage_id=next((s["id"] for s in stages if s["state"] != "accepted"), stages[-1]["id"]),
                           owner=None, epoch=control["epoch"] + 1, state="waiting_for_ai", pending_decision=None)
            handoff(record, identity, now_ms, "plan_ready")
        elif action == "record_decision":
            stage = stage_for(record, change["stage_id"])
            if stage["id"] != control["current_stage_id"] or stage["state"] == "accepted":
                raise Fault("task_stage_conflict", "Decide the current remaining stage; accepted work is not silently reopened.")
            _unique(change["evidence"])
            operations = {item["id"]: item for item in record["operations"]}
            for key in change["evidence"]:
                operation = operations.get(key)
                if (operation is None or operation["stage_id"] != stage["id"]
                        or operation["source_sha256"] != stage["source_sha256"] or pending(operation)
                        or change["outcome"] == "accepted" and operation["result"]["status"] != "succeeded"):
                    raise Fault("task_evidence_invalid", "Decision evidence must identify applicable, verified original results.")
            if change["outcome"] == "accepted":
                if any(pending(item) for item in record["operations"]):
                    raise Fault("task_effect_unresolved", "Acceptance cannot hide live or uncertain original effects.")
                if (not change["evidence"] and any(item["stage_id"] == stage["id"] for item in record["operations"])):
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
    reserve(record, new_work=action not in {"claim", "record_decision"})


def _pairs(items: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def decode(raw: bytes) -> dict:
    try:
        if len(raw) > MAX_RECORD:
            raise ValueError("record too large")
        value = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(value, dict) or type(value.get("format")) is not int or value["format"] not in READ_FORMATS:
            raise Fault("unsupported_task_format", "The actual reader does not support this structured task format.")
        record = Record.model_validate(value).model_dump()
        if canonical(record) != raw:
            raise ValueError("noncanonical task record")
        return record
    except Fault:
        raise
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise Fault("corrupt_tasks", "The canonical task is invalid or incomplete; preserve it without fallback or overwrite.") from error
