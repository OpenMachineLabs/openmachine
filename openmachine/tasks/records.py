"""Bounded task contracts and canonical records; no execution or state mutation."""

import hashlib
import json
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_serializer, model_validator

from openmachine.foundation.sanitize import Fault, redact

MAX_RECORD = 65536
MAX_ACCEPTANCE = 49152
MAX_SCOPE_REQUEST = 131072
MAX_ACTIVE_OPERATIONS = 64
MAX_OPERATION = MAX_OPERATION_BYTES = 65536
MAX_EVENT_BYTES = 65536
MAX_PAGE_BYTES = 65536
MAX_PAGE_ITEMS = 50
MAX_INTEGER = 2**53 - 1
READ_FORMATS = (1, 2, 3, 4)


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
    worktree: Text1024 = ""

    @model_validator(mode="after")
    def selected_worktree(self):
        if self.worktree:
            path = PurePosixPath(self.worktree)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("worktree needs an absolute path without parent traversal")
            _line(self.worktree)
        return self

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        if not self.worktree:
            value.pop("worktree", None)
        return value


class Source(Model):
    locator: Annotated[Text512, Field(min_length=1)]
    sha256: SHA


class GitHubBinding(Model):
    connection_id: ID
    account_id: Revision
    authorization_revision: Revision


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
    github_connections: dict[Repository, GitHubBinding] = Field(default_factory=dict, max_length=8)

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        if not self.github_connections:
            value.pop("github_connections", None)
        return value

    @model_validator(mode="after")
    def bindings(self):
        if not all(value.strip() for value in (self.objective, self.acceptance, self.stop_condition)):
            raise ValueError("objective, acceptance and stop condition must be nonempty")
        for values in (self.authority_refs, self.repositories, self.tools,
                       [(t.machine, t.workspace, t.worktree) for t in self.targets], [s.locator for s in self.sources]):
            _unique(values)
        if any(repo.split("/")[1] in {".", ".."} for repo in self.repositories):
            raise ValueError("invalid repository")
        if not set(self.github_connections) <= set(self.repositories):
            raise ValueError("connection binding outside the repository contract")
        return self


class RetainedContract(Contract):
    """Read retired recovery policy from original Task authority, never new input."""

    recovery_policy: Literal["task_chat", "disabled"] | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        if not self.github_connections:
            value.pop("github_connections", None)
        if self.recovery_policy is None:
            value.pop("recovery_policy", None)
        return value


class StageDefinition(Model):
    id: TaskID
    depends_on: Annotated[list[TaskID], Field(max_length=16)]
    result: Annotated[Text512, Field(min_length=1)]
    verification: Annotated[Text512, Field(min_length=1)]
    source_refs: Annotated[list[Text512], Field(max_length=16)]
    source_sha256: SHA


class Decision(Model):
    state: Literal["pending", "running", "blocked", "accepted", "invalidated"]
    evidence: Annotated[list[ID], Field(max_length=64)]


class Stage(StageDefinition, Decision):
    pass


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
    state: Literal["planned", "running", "recovering", "waiting_for_ai", "waiting_for_user", "paused", "completed", "failed", "cancelled", "abandoned"]
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


class GitHubMergeBinding(Model):
    head_sha: Head
    base_ref: Annotated[Text256, Field(min_length=1)]
    commit_title_sha256: SHA | None
    commit_message_sha256: SHA | None


class GitHubLocator(Model):
    kind: Literal["github"]
    repository: Repository
    object_type: Literal["issue", "pull_request", "comment", "review", "review_thread", "merge"]
    number: Revision | None
    parent_number: Revision | None
    anchor: Annotated[Text128, Field(min_length=1)]
    thread_id: Annotated[str, Field(pattern=r"^PRRT_[A-Za-z0-9_-]{1,128}$")] | None = None
    expected_head_sha: Head | None = None
    connection: GitHubBinding | None = None
    merge: GitHubMergeBinding | None = None

    @model_validator(mode="after")
    def effect_binding(self):
        if self.merge is not None and (self.object_type != "merge" or self.number is None or self.parent_number is not None):
            raise ValueError("merge evidence requires an original pull request")
        if self.object_type == "review_thread":
            if self.thread_id is None or self.expected_head_sha is None or self.number is None or self.parent_number is not None:
                raise ValueError("review thread locators require a PR number, thread ID and exact HEAD")
        elif self.thread_id is not None or self.expected_head_sha is not None:
            raise ValueError("thread-only fields cannot decorate other GitHub locators")
        return self

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        for key in ("connection", "thread_id", "expected_head_sha", "merge"):
            if value[key] is None:
                value.pop(key)
        return value


class GitHubEffectScope(Model):
    resource: Literal["object", "comment", "review"]
    number: Revision | None
    parent_number: Revision | None
    fields: Annotated[list[Literal["*", "title", "body", "state", "labels", "assignees", "draft", "merge", "threads", "create"]], Field(min_length=1, max_length=5)]
    creation_key: SHA | None = None

    @model_validator(mode="after")
    def canonical_fields(self):
        if self.fields != sorted(set(self.fields)) or "*" in self.fields and len(self.fields) != 1:
            raise ValueError("effect fields need a unique canonical set")
        if self.resource == "object" and self.parent_number is not None:
            raise ValueError("object scopes have no parent")
        if self.creation_key is not None and (self.resource != "object" or self.number is not None or self.fields != ["create"]):
            raise ValueError("creation identity belongs only to an unnumbered object creation")
        return self

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        if self.creation_key is None:
            value.pop("creation_key", None)
        return value


class Result(Model):
    status: Literal["running", "succeeded", "failed", "cancelled", "partial", "unknown"]
    receipt_sha256: SHA | None
    summary: Text256
    log_offset: Integer | None
    failure_code: Name | None = None
    effect: Literal["known_no_effect", "active", "effect_verified", "effect_partial", "effect_unknown"] | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        for key in ("failure_code", "effect"):
            if value[key] is None:
                value.pop(key)
        return value


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
    effect_scope: GitHubEffectScope | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        if self.effect_scope is None:
            value.pop("effect_scope", None)
        return value

    @model_validator(mode="after")
    def bounds(self):
        if len(canonical(self.model_dump())) > MAX_OPERATION:
            raise ValueError("operation or locator exceeds its encoded bound")
        if self.state == "observed" and (self.observed_at_ms is None or self.result is None):
            raise ValueError("observed operation needs original result evidence")
        if self.observed_at_ms is not None and self.observed_at_ms < self.admitted_at_ms:
            raise ValueError("observation precedes admission")
        if self.effect_scope is not None and not isinstance(self.locator, GitHubLocator):
            raise ValueError("only GitHub operations carry provider field scopes")
        return self


class EffectScopeWitness(Model):
    id: ID
    operation_id: ID
    request_sha256: SHA
    scope: GitHubEffectScope
    principal_ref: Annotated[Text256, Field(min_length=1)]
    at_ms: Integer


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
    outcome: Literal["completed", "failed", "cancelled", "abandoned"]
    finished_at_ms: Integer
    summary: Text2048
    acceptance: Annotated[list[Acceptance], Field(max_length=64)]
    limitations: Text2048


class LastUpdate(Model):
    id: ID
    request_sha256: SHA


def _retained_recovery_fields(version: int, contract: RetainedContract, has_registration: bool) -> None:
    """Keep original format-3/4 fields paired without granting retired recovery."""
    retired = contract.recovery_policy is not None or has_registration
    if retired and (version < 3 or contract.recovery_policy is None or not has_registration):
        raise ValueError("retired recovery metadata must retain its original format and pair")


class Current(Model):
    format: Annotated[int, Field(strict=True, ge=1, le=4)]
    task_id: TaskID
    revision: Revision
    title: Title
    anchors: Annotated[list[Anchor], Field(max_length=16)]
    created_at_ms: Integer
    updated_at_ms: Integer
    origin: Origin
    contract: RetainedContract
    plan: Plan
    control: Control
    handoff: Handoff | None
    recovery: Recovery | None
    terminal: Terminal | None
    last_update: LastUpdate
    chat_registration: ID | None = None
    control_revision: Revision | None = None

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        value = handler(self)
        if "chat_registration" not in self.model_fields_set:
            value.pop("chat_registration", None)
        if self.format < 4:
            value.pop("control_revision", None)
            value.pop("effect_scopes", None)
        return value

    @model_validator(mode="after")
    def references(self):
        _stages(self.contract, self.plan.stages)
        _retained_recovery_fields(self.format, self.contract, "chat_registration" in self.model_fields_set)
        if self.format < 4:
            if self.control_revision is not None:
                raise ValueError("scoped supervision requires format 4")
        elif self.control_revision is None or self.control_revision > self.revision:
            raise ValueError("format 4 needs its material control revision")
        if self.format == 1 and self.contract.github_connections:
            raise ValueError("legacy records cannot acquire a new account binding")
        if self.anchors != sorted(set(self.anchors)) or self.created_at_ms > self.updated_at_ms or self.plan.revision > self.revision:
            raise ValueError("invalid record ordering")
        stages = {stage.id: stage for stage in self.plan.stages}
        if self.control.current_stage_id is not None and self.control.current_stage_id not in stages:
            raise ValueError("invalid current stage")
        for stage in self.plan.stages:
            _unique(stage.evidence)
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
        is_terminal = self.control.state in {"completed", "failed", "cancelled", "abandoned"}
        if is_terminal != (self.terminal is not None):
            raise ValueError("terminal control and final record must agree")
        if self.terminal is not None:
            terminal = self.terminal
            if (terminal.outcome != self.control.state or self.handoff is not None or self.control.owner is not None
                    or not self.created_at_ms <= terminal.finished_at_ms <= self.updated_at_ms
                    or len(canonical(terminal.model_dump())) > 4096):
                raise ValueError("invalid terminal record")
            if terminal.outcome == "abandoned" and (self.control.stop_intent != "cancel" or terminal.acceptance):
                raise ValueError("abandonment must preserve stopped work without accepted effects")
            for evidence in terminal.acceptance:
                if evidence.stage_id not in stages:
                    raise ValueError("invalid terminal acceptance reference")
        bounded = self.model_dump()
        for stage in bounded["plan"]["stages"]:
            stage.pop("evidence")
            stage["decision_version"] = self.revision
        if len(canonical(bounded)) > MAX_RECORD:
            raise ValueError("current task state exceeds its bounded contract")
        return self


class Record(Current):
    """Exact formats 1-4 decoder, used only at the legacy import boundary."""

    operations: Annotated[list[Operation], Field(max_length=64)]
    history: Annotated[list[History], Field(max_length=64)]
    effect_scopes: Annotated[list[EffectScopeWitness], Field(max_length=64)] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        _stages(self.contract, self.plan.stages)
        _retained_recovery_fields(self.format, self.contract, "chat_registration" in self.model_fields_set)
        if self.format < 4:
            if self.control_revision is not None or self.effect_scopes or any(
                    op.effect_scope is not None or op.result is not None and (
                        op.result.failure_code is not None or op.result.effect is not None) for op in self.operations):
                raise ValueError("scoped supervision requires format 4")
        elif self.control_revision is None or self.control_revision > self.revision:
            raise ValueError("format 4 needs its material control revision")
        if self.format == 1 and (self.contract.github_connections or any(
                isinstance(op.locator, GitHubLocator) and op.locator.connection is not None for op in self.operations)):
            raise ValueError("legacy records cannot acquire a new account binding")
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
            if (self.format == 2 or self.format >= 3 and operation.plan_revision == self.plan.revision) and isinstance(operation.locator, GitHubLocator) and operation.locator.connection is None:
                raise ValueError("new GitHub operations require their account binding")
            if operation.plan_revision == self.plan.revision and operation.source_sha256 != stages[operation.stage_id].source_sha256:
                raise ValueError("operation source binding mismatch")
            if self.format >= 2 and operation.plan_revision == self.plan.revision and isinstance(operation.locator, GitHubLocator):
                binding = next((item for repo, item in self.contract.github_connections.items()
                                if repo.casefold() == operation.locator.repository.casefold()), None)
                if operation.locator.connection != binding:
                    raise ValueError("operation account differs from the accepted repository binding")
        _unique([item.id for item in self.effect_scopes])
        _unique([item.operation_id for item in self.effect_scopes])
        for witness in self.effect_scopes:
            operation = operations.get(witness.operation_id)
            if (operation is None or not isinstance(operation.locator, GitHubLocator)
                    or operation.effect_scope is not None or witness.request_sha256 != operation.request_sha256
                    or not operation.admitted_at_ms <= witness.at_ms <= self.updated_at_ms):
                raise ValueError("scope witness must bind an original unscoped GitHub request")
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
        is_terminal = self.control.state in {"completed", "failed", "cancelled", "abandoned"}
        if is_terminal != (self.terminal is not None):
            raise ValueError("terminal control and final record must agree")
        if self.terminal is not None:
            terminal = self.terminal
            if (terminal.outcome != self.control.state or self.handoff is not None or self.control.owner is not None
                    or not self.created_at_ms <= terminal.finished_at_ms <= self.updated_at_ms
                    or len(canonical(terminal.model_dump())) > 4096):
                raise ValueError("invalid terminal record")
            if terminal.outcome == "abandoned" and (self.control.stop_intent != "cancel" or terminal.acceptance):
                raise ValueError("abandonment must preserve stopped work without accepted effects")
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
    contract = dict(request["contract"])
    if not now_ms < contract["expires_at_ms"]:
        raise Fault("task_authority_expired", "Task creation requires a future explicit authority expiry.")
    stages = [dict(stage, state="pending", evidence=[]) for stage in request["stages"]]
    first = stages[0]
    value = {"format": 4, "control_revision": 1, "effect_scopes": [],
             "task_id": request["task_id"], "revision": 1, "title": request["title"],
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


class Renew(Model):
    action: Literal["renew"]
    claim_id: ID


class BindEffectScope(Model):
    action: Literal["bind_effect_scope"]
    operation_id: ID
    method: Literal["github_issue", "github_pull_request", "github_comment", "github_review", "github_review_thread", "github_merge"]
    arguments: dict[str, object]
    reason: Annotated[Text512, Field(min_length=1)]


class ReasonedControl(Model):
    action: Literal["release", "pause", "resume", "cancel", "abandon"]
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


Transition = Annotated[Claim | Renew | BindEffectScope | ReasonedControl | RevisePlan | RecordDecision | Finish, Field(discriminator="action")]


class ControlRequest(Model):
    task_id: TaskID
    expected_revision: Revision
    expected_plan_revision: Revision
    expected_epoch: Integer
    update_id: ID
    transition: Transition


def normalize_control(value: dict) -> dict:
    try:
        transition = value.get("transition") if isinstance(value, dict) else None
        bound = MAX_SCOPE_REQUEST if isinstance(transition, dict) and transition.get("action") == "bind_effect_scope" else MAX_ACCEPTANCE
        if len(canonical(value)) > bound:
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
