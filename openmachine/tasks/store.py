"""Transactional current Task state and indexed original evidence."""

from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from openmachine.foundation.sanitize import Fault, redact
from openmachine.foundation.state import lease
from . import database as db, effects, records
from .rows import (append_event, decode_operation, insert_record, operation_key, pack_current,
                   put_legacy, put_operation, read_current, read_decision, read_witnesses,
                   record_core, task_row, validate_operation)
from .index import (INDEX_PAGE, MAX_CHECKPOINT, metadata as validate_metadata,
                    private_path, read_checkpoint, read_index, read_structured, task_id)


def _observation_support() -> dict:
    return {"support": {"structured_records": True, "supervised_admission": True,
                        "runtime_recovery": False, "events_delivery": False},
            "observations": [{"kind": "caller", "state": "unknown", "source": "not_observed",
                "observed_at_ms": None, "last_progress_at_ms": None, "next_attempt_at_ms": None,
                "blocker": "Caller continuation is not observed; native-account claims are not ChatGPT presence or automatic wake."}]}


def _selected(connection, identity: str, current: dict, operation_ids=()) -> list[dict]:
    identities = set(operation_ids)
    if len(identities) > 256 or any(not isinstance(key, str) or len(key) != 32 for key in identities):
        raise Fault("invalid_range", "Use a bounded set of original operation IDs.")
    rows = {row["id"]: row for row in connection.execute(
        "SELECT * FROM operations WHERE task_id=? AND unresolved=1 ORDER BY ordinal", (identity,))}
    if len(rows) > records.MAX_ACTIVE_OPERATIONS:
        raise Fault("corrupt_tasks", "Unresolved task work exceeds its retained active-work budget.")
    for stage in current["plan"]["stages"]:
        row = connection.execute("SELECT * FROM operations WHERE task_id=? AND stage_id=? ORDER BY ordinal DESC LIMIT 1",
                                  (identity, stage["id"])).fetchone()
        if row is not None:
            rows[row["id"]] = row
    for key in identities - rows.keys():
        row = connection.execute("SELECT * FROM operations WHERE task_id=? AND id=?", (identity, key)).fetchone()
        if row is not None:
            rows[key] = row
    return [decode_operation(row) for row in sorted(rows.values(), key=lambda row: row["ordinal"])]


def _facts(connection, row, current: dict, operations: list[dict]) -> dict:
    identity = row["task_id"]
    unresolved = [item for item in operations if effects.unresolved(item)]
    pending = [item for item in unresolved if records.pending(item)]
    admitted = connection.execute("SELECT admitted_at_ms FROM operations WHERE task_id=? ORDER BY admitted_at_ms DESC LIMIT 1", (identity,)).fetchone()
    observed = connection.execute("SELECT observed_at_ms FROM operations WHERE task_id=? ORDER BY observed_at_ms DESC LIMIT 1", (identity,)).fetchone()
    progress = max(current["created_at_ms"], admitted[0] if admitted else 0,
                   (current["terminal"] or {}).get("finished_at_ms", 0))
    for stage in current["plan"]["stages"]:
        if stage["state"] == "accepted":
            event = connection.execute("SELECT at_ms FROM task_events WHERE task_id=? AND kind='decision' AND stage_id=? ORDER BY at_ms DESC LIMIT 1",
                                        (identity, stage["id"])).fetchone()
            progress = max(progress, event[0] if event else 0)
    return {"operation_count": row["operation_count"], "history_count": row["history_count"],
            "scope_count": row["scope_count"], "stage_operation_counts": json.loads(row["stage_counts"]),
            "unresolved_count": len(unresolved), "pending_count": len(pending),
            "running_count": sum((op["result"] or {}).get("status") == "running" for op in pending),
            "unknown_count": sum(op["state"] == "unknown" or (op["result"] or {}).get("status") == "unknown" for op in unresolved),
            "partial_count": sum((op["result"] or {}).get("status") == "partial" for op in unresolved),
            "last_admitted_at_ms": admitted[0] if admitted else None,
            "last_observed_at_ms": observed[0] if observed else None, "progress_ms": progress}


def _view(connection, identity: str, operation_ids=()) -> dict:
    row = task_row(connection, identity)
    current = db.decode(row["current"], row["sha256"])
    if row["kind"] == "legacy":
        body = current["body"]
        return {"task_id": identity, **current, "body": redact(body), "content_redacted": redact(body) != body,
                "generation": db.metadata(connection)["generation"], "resume_authorized": False}
    current, versions = read_current(connection, row)
    if current["task_id"] != identity or current["revision"] != row["revision"]:
        raise Fault("corrupt_tasks", "Task identity or revision differs from its stored authority.")
    operations = _selected(connection, identity, current, operation_ids)
    facts = _facts(connection, row, current, operations)
    current.update(operations=operations, history=[], effect_scopes=read_witnesses(connection, identity, operations), _facts=facts,
                   _decisions=versions)
    return {"task_id": identity, "record": current, "facts": facts, "read_schema": 2, "record_complete": False,
            "observed_at_ms": time.time_ns() // 1000000, "index_status": "current",
            "generation": db.metadata(connection)["generation"], "sequence": row["change_seq"],
            **_observation_support()}


@contextmanager
def _legacy_read(state: Path):
    if not os.path.lexists(state / "tasks"):
        raise Fault("tasks_uninitialized", "This private Task store has not been initialized.")
    private_path(state, directory=True)
    private_path(state / "tasks", directory=True)
    private_path(state / "locks", directory=True)
    private_path(state / "locks/tasks.lock")
    with lease(state / "locks/tasks.lock", shared=True, create=False):
        if os.path.lexists(state / "tasks.db"):
            # A prepared database has no authority while the original directory exists.
            connection = db._open(state, write=False)
            try:
                if db.metadata(connection)["active"]:
                    raise Fault("corrupt_tasks", "Two Task stores claim authority.")
            finally:
                connection.close()
        yield state / "tasks"


@contextmanager
def locked_read(state: Path, identity: str, *, operation_ids=()):
    task_id(identity)
    if os.path.lexists(state / "tasks"):
        with _legacy_read(state) as root:
            if os.path.lexists(root / identity / "task.json"):
                record, raw = read_structured(root, identity)
                status, generation = "stale", None
                try:
                    index = read_index(root)
                    generation = index["generation"]
                    if index["tasks"].get(identity, {}).get("sha256") == hashlib.sha256(raw).hexdigest():
                        status = "current"
                except Fault:
                    pass
                operations = record["operations"]
                accepted = {stage["id"] for stage in record["plan"]["stages"] if stage["state"] == "accepted"}
                progress = max([record["created_at_ms"], (record["terminal"] or {}).get("finished_at_ms", 0)]
                    + [op["admitted_at_ms"] for op in operations]
                    + [event["at_ms"] for event in record["history"] if event["kind"] == "decision" and event["stage_id"] in accepted])
                facts = {"operation_count": len(operations), "history_count": len(record["history"]),
                    "scope_count": len(record.get("effect_scopes", [])), "progress_ms": progress,
                    "last_observed_at_ms": max((op["observed_at_ms"] or 0 for op in operations), default=0) or None,
                    "last_admitted_at_ms": max((op["admitted_at_ms"] for op in operations), default=0) or None,
                    "unresolved_count": sum(effects.unresolved(op) for op in operations),
                    "pending_count": sum(records.pending(op) for op in operations),
                    "running_count": sum((op["result"] or {}).get("status") == "running" for op in operations),
                    "unknown_count": sum((op["result"] or {}).get("status") == "unknown" for op in operations),
                    "partial_count": sum((op["result"] or {}).get("status") == "partial" for op in operations),
                    "stage_operation_counts": {stage["id"]: sum(op["stage_id"] == stage["id"] for op in operations) for stage in record["plan"]["stages"]}}
                record["_facts"] = facts
                yield {"task_id": identity, "record": record, "facts": facts, "observed_at_ms": time.time_ns() // 1000000,
                       "index_status": status, "storage": "legacy", "generation": generation, **_observation_support()}
            else:
                index = read_index(root)
                item, raw = read_checkpoint(root, index, identity)
                body = raw.decode()
                yield {"task_id": identity, **item, "body": redact(body), "content_redacted": redact(body) != body,
                       "generation": index["generation"], "resume_authorized": False}
        return
    with db.connect(state, write=True) as connection:
        if connection is None:
            raise Fault("tasks_uninitialized", "This private Task store has not been initialized.")
        yield _view(connection, identity, operation_ids)


def read(state: Path, identity: str, *, operation_ids=()) -> dict:
    task_id(identity)
    if os.path.lexists(state / "tasks"):
        with locked_read(state, identity, operation_ids=operation_ids) as view:
            return view
    with db.connect(state) as connection:
        if connection is None:
            raise Fault("tasks_uninitialized", "This private Task store has not been initialized.")
        return _view(connection, identity, operation_ids)


def operation(state: Path, identity: str, operation_id: str) -> dict | None:
    with db.connect(state) as connection:
        if connection is None:
            return next((op for op in read(state, identity)["record"]["operations"] if op["id"] == operation_id), None)
        row = connection.execute("SELECT * FROM operations WHERE task_id=? AND id=?", (identity, operation_id)).fetchone()
        return decode_operation(row) if row else None


def control_update(state: Path, identity: str, update_id: str) -> dict | None:
    with db.connect(state) as connection:
        if connection is None:
            retained = read(state, identity)["record"]["last_update"]
            return retained if retained["id"] == update_id else None
        row = connection.execute("SELECT id,request_sha256 FROM task_updates WHERE task_id=? AND id=?", (identity, update_id)).fetchone()
        return {"id": row[0], "request_sha256": row[1]} if row else None


def create(state: Path, identity: str, update_id: str, title: str, anchors: list[str], origin: dict,
           contract: dict, stages: list[dict], *, validate_new: Callable[[dict], None] | None = None) -> dict:
    request = records.normalize_create({"task_id": identity, "update_id": update_id, "title": title,
        "anchors": anchors, "origin": origin, "contract": contract, "stages": stages})
    fingerprint = records.digest({"method": "create_task", **request})
    try:
        original = read(state, identity)
    except Fault as error:
        if error.code not in {"tasks_uninitialized", "task_not_found"}:
            raise
    else:
        retained = control_update(state, identity, update_id) if "record" in original else None
        if retained is None or retained["request_sha256"] != fingerprint:
            raise Fault("task_conflict", "The original Task ID belongs to different retained creation intent.")
        return {**original, "status": "unchanged"}
    records.new_record(request, time.time_ns() // 1000000)
    prepared = deepcopy(request)
    if validate_new is not None:
        validate_new(prepared)
    record = records.new_record(prepared, time.time_ns() // 1000000)
    record["last_update"]["request_sha256"] = fingerprint
    with db.connect(state, write=True, initialize=True) as connection:
        if connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (identity,)).fetchone():
            raise Fault("task_conflict", "An original Task appeared during creation; observe it before another action.")
        db.admission_space(state)
        insert_record(connection, record)
        view = _view(connection, identity)
    return {**view, "status": "created"}


def update(state: Path, identity: str, transform: Callable[[dict, int], None], *,
           expected_revision: int | None = None, last_update: dict | None = None,
           allow_observations: bool = False,
           observation: bool = False, upgrade: bool = False, operation_ids=(), reserve_disk: bool = True) -> dict:
    """Commit bounded current state, touched rows and audit together; callbacks do no IO."""
    task_id(identity)
    with db.connect(state, write=True) as connection:
        if connection is None:
            raise Fault("tasks_uninitialized", "This Task store has not been initialized.")
        view = _view(connection, identity, operation_ids)
        if "record" not in view:
            raise Fault("task_conflict", "A structured transition cannot replace a legacy checkpoint.")
        original = view["record"]
        if last_update is not None:
            retained = control_update(state, identity, last_update["id"])
            if retained is not None:
                if retained != last_update:
                    raise Fault("task_conflict", "The retained update ID belongs to different normalized intent.")
                return {**view, "status": "unchanged"}
        compatible = (expected_revision is not None and allow_observations and original["format"] >= 4
                      and original["control_revision"] <= expected_revision <= original["revision"])
        if expected_revision is not None and expected_revision != original["revision"] and not compatible:
            raise Fault("task_conflict", "Task authority advanced; read the current revision before another transition.",
                        {"task_id": identity, "task_revision": original["revision"], "plan_revision": original["plan"]["revision"],
                         "continuation_epoch": original["control"]["epoch"]})
        value = deepcopy(original)
        if upgrade and value["format"] == 3:
            value.update(format=4, control_revision=original["revision"], effect_scopes=[])
        now = max(time.time_ns() // 1000000, original["updated_at_ms"])
        transform(value, now)
        if last_update is not None:
            value["last_update"] = last_update
        if value == original:
            return {**view, "status": "unchanged"}
        if value["format"] != original["format"] and not (original["format"] == 3 and value["format"] == 4 and upgrade):
            raise Fault("task_identity_conflict", "Unsupported Task record format transition.")
        for key in ("task_id", "title", "anchors", "created_at_ms", "origin"):
            if value[key] != original[key]:
                raise Fault("task_identity_conflict", "Original task identity and initiating request are immutable.")
        value.update(revision=original["revision"] + 1, updated_at_ms=now)
        if value["format"] >= 4:
            material = not observation or any(value[key] != original[key] for key in ("contract", "plan", "terminal"))
            material |= any(value["control"][key] != original["control"][key] for key in ("owner", "epoch", "stop_intent", "current_stage_id"))
            value["control_revision"] = value["revision"] if material else original.get("control_revision", original["revision"])
        try:
            core = records.Current.model_validate(record_core(value)).model_dump()
            prior_row = task_row(connection, identity)
            prior = db.decode(prior_row["current"], prior_row["sha256"])
            if not connection.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='snapshot' LIMIT 1", (identity,)).fetchone():
                append_event(connection, identity, prior, snapshot_at=now)
            old_operations = {item["id"]: item for item in original["operations"]}
            selected = {item["id"]: item for item in value["operations"]}
            if len(selected) != len(value["operations"]) or not old_operations.keys() <= selected.keys():
                raise ValueError("original selected operations cannot be removed or duplicated")
            count = original["_facts"]["operation_count"]
            stages = {stage["id"]: original["_facts"]["stage_operation_counts"].get(stage["id"], 0)
                      for stage in core["plan"]["stages"]}
            for item in value["operations"]:
                old = old_operations.get(item["id"])
                if item == old:
                    continue
                item = validate_operation(item, core)
                if old is not None:
                    for key in ("id", "stage_id", "plan_revision", "epoch", "request_sha256", "source_sha256", "admitted_at_ms", "effect_scope"):
                        if old.get(key) != item.get(key):
                            raise Fault("task_identity_conflict", "Original operation intent and scope are immutable.")
                    changed_locator = {key for key in old["locator"].keys() | item["locator"].keys()
                                       if old["locator"].get(key) != item["locator"].get(key)}
                    enrichable = {key for key in ("number", "parent_number") if old["locator"].get(key) is None} if old["locator"]["kind"] == "github" else set()
                    if not changed_locator <= enrichable or operation_key(old["locator"]) != operation_key(item["locator"]):
                        raise Fault("task_identity_conflict", "Original operation locator identity is immutable.")
                    if old["locator"]["kind"] == "github" and old["result"] is not None and not records.pending(old):
                        raise Fault("task_identity_conflict", "Verified provider evidence is immutable.")
                    ordinal = connection.execute("SELECT ordinal FROM operations WHERE task_id=? AND id=?", (identity, item["id"])).fetchone()[0]
                else:
                    binding = operation_binding(state, operation_key(item["locator"]), item["id"])
                    if binding is not None:
                        if binding["task_id"] != identity or any(op["id"] == item["id"] for op in binding["operations"]):
                            raise Fault("task_operation_conflict", "Original operation identity is already bound.")
                    conflicts = effect_conflicts(state, value, item)
                    if conflicts:
                        raise Fault("task_effect_unresolved", "An overlapping original effect is unresolved.", {"conflicts": conflicts})
                    if reserve_disk:
                        db.admission_space(state)
                    count += 1
                    ordinal = count
                    stages[item["stage_id"]] = stages.get(item["stage_id"], 0) + 1
                put_operation(connection, identity, item, ordinal)
                append_event(connection, identity, {"kind": "operation_admitted" if old is None else "operation_observed", "at_ms": now,
                    "stage_id": item["stage_id"], "operation_id": item["id"],
                    "operation": item if old is None else {key: item[key] for key in ("id", "state", "observed_at_ms", "result", "locator")}})
            if connection.execute("SELECT count(*) FROM operations WHERE task_id=? AND unresolved=1", (identity,)).fetchone()[0] > records.MAX_ACTIVE_OPERATIONS:
                raise Fault("task_capacity", "Retain active-work capacity for original results and stopping.")
            old_witnesses = {item["operation_id"]: item for item in original.get("effect_scopes", [])}
            new_witnesses = {item["operation_id"]: item for item in value.get("effect_scopes", [])}
            if not old_witnesses.keys() <= new_witnesses.keys() or any(new_witnesses[key] != item for key, item in old_witnesses.items()):
                raise Fault("task_identity_conflict", "Original scope witnesses are immutable.")
            for key in new_witnesses.keys() - old_witnesses.keys():
                witness = records.EffectScopeWitness.model_validate(new_witnesses[key]).model_dump()
                op = operation(state, identity, key)
                if (op is None or op["locator"]["kind"] != "github" or op.get("effect_scope") is not None
                        or op["request_sha256"] != witness["request_sha256"] or not op["admitted_at_ms"] <= witness["at_ms"] <= now):
                    raise ValueError("scope witness does not bind original intent")
                payload, sha = db.encoded(witness)
                connection.execute("INSERT INTO operation_scopes VALUES (?,?,?,?,?)", (identity, key, db.encoded(witness["scope"])[0], payload, sha))
                append_event(connection, identity, {"kind": "scope_bound", "at_ms": now, "operation_id": key, "witness": witness})
            for stage in core["plan"]["stages"]:
                if stage not in original["plan"]["stages"]:
                    for key in stage["evidence"]:
                        op = operation(state, identity, key)
                        if op is None or op["stage_id"] != stage["id"]:
                            raise ValueError("stage evidence lost its original operation")
            for item in (core["terminal"] or {}).get("acceptance", []):
                if item["operation_id"] is not None:
                    op = operation(state, identity, item["operation_id"])
                    if op is None or op["stage_id"] != item["stage_id"]:
                        raise ValueError("terminal evidence differs")
            if (core["terminal"] is not None and core["terminal"]["outcome"] != "abandoned"
                    and connection.execute("SELECT 1 FROM operations WHERE task_id=? AND unresolved=1 LIMIT 1", (identity,)).fetchone()):
                raise Fault("task_effect_unresolved", "Original live, partial or unknown effects prevent retirement.")
            for event in value["history"]:
                records.History.model_validate(event)
                if not core["created_at_ms"] <= event["at_ms"] <= now:
                    raise ValueError("material event time differs")
                append_event(connection, identity, event)
            packed = pack_current(connection, core, original["_decisions"])
            changes = {key: item for key, item in packed.items() if key not in {"revision", "updated_at_ms", "control_revision", "last_update"} and item != prior.get(key)}
            if "plan" in changes and core["plan"]["revision"] == original["plan"]["revision"]:
                changes["plan"] = {"revision": core["plan"]["revision"], "decisions": [
                    {key: stage[key] for key in ("id", "state", "decision_version")}
                    for stage in packed["plan"]["stages"] if stage not in prior["plan"]["stages"]]}
            if changes or last_update is not None:
                append_event(connection, identity, {"kind": "control" if last_update else "state_changed", "at_ms": now, "changes": changes}, update=last_update)
            payload, sha = db.encoded(packed)
            connection.execute("UPDATE tasks SET revision=?,change_seq=?,current=?,sha256=?,operation_count=?,scope_count=scope_count+?,stage_counts=? WHERE task_id=?",
                (core["revision"], db.bump(connection), payload, sha, count, len(new_witnesses) - len(old_witnesses), json.dumps(stages), identity))
            result = _view(connection, identity, operation_ids)
        except (ValueError, TypeError, KeyError, sqlite3.IntegrityError) as error:
            raise Fault("invalid_task_transition", "The transaction violates current Task or original evidence constraints; no new effect was admitted.") from error
    return {**result, "status": "saved"}


def operation_binding(state: Path, key: tuple, operation_id: str = "") -> dict | None:
    with db.connect(state) as connection:
        if connection is None:
            if os.path.lexists(state / "tasks"):
                raise Fault("task_migration_required", "Migrate retained Task bindings before new effects.")
            return None
        encoded_key = records.canonical(key).decode()
        owners = connection.execute("SELECT DISTINCT task_id FROM operations WHERE effect_key=? OR id=? LIMIT 2",
                                    (encoded_key, operation_id)).fetchall()
        if len(owners) > 1:
            raise Fault("task_operation_conflict", "An original effect is ambiguously bound across Tasks.")
        if not owners:
            return None
        identity = owners[0][0]
        first = connection.execute("SELECT * FROM operations WHERE task_id=? AND effect_key=? ORDER BY ordinal LIMIT 1",
                                   (identity, encoded_key)).fetchone()
        exact = connection.execute("SELECT * FROM operations WHERE task_id=? AND id=?", (identity, operation_id)).fetchone()
        rows = {row["id"]: row for row in (first, exact) if row is not None}
        return {"task_id": identity, "operations": [decode_operation(row) for row in rows.values()]}


def effect_conflicts(state: Path, proposed_record: dict, proposed: dict) -> list[dict]:
    if proposed["locator"]["kind"] != "github":
        return []
    with db.connect(state) as connection:
        if connection is None:
            if os.path.lexists(state / "tasks"):
                raise Fault("task_migration_required", "Migrate retained conflict authority before new effects.")
            return []
        repository = proposed["locator"]["repository"].casefold()
        if proposed["locator"].get("object_type") == "project":
            # Organization Projects can be reached through different repositories.
            rows = connection.execute("SELECT * FROM operations WHERE repository LIKE ? AND unresolved=1",
                                      (repository.split("/", 1)[0] + "/%",))
        else:
            rows = connection.execute("SELECT * FROM operations WHERE repository=? AND unresolved=1", (repository,))
        for row in rows:
            if row["id"] == proposed.get("id"):
                continue
            item = decode_operation(row)
            scope = {"effect_scopes": read_witnesses(connection, row["task_id"], [item])}
            if effects.conflicts(scope, item, proposed_record, proposed):
                return [{"task_id": row["task_id"], "operations": [item]}]
        return []


def require_legacy(state: Path, identity: str) -> None:
    if "record" in read(state, identity):
        raise Fault("supervised_admission_unavailable", "Supply current task_context for structured work; a bare task_id is not authority.")


def _target_filter(machine: str, workspace: str, workspace_identity: str,
                   target_identity: str) -> tuple[str, list[str]]:
    if (any(not isinstance(value, str) for value in
            (machine, workspace, workspace_identity, target_identity))
            or any(value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value)
                   for value in (machine, workspace))
            or any(value and not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in (workspace_identity, target_identity))
            or workspace and not machine or workspace_identity and not workspace
            or target_identity and (not machine or workspace or workspace_identity)):
        raise Fault("invalid_task_filter", "Select a machine and optional workspace/identity, or a pinned operation target identity.")
    if not machine:
        return "", []
    if target_identity:
        return ("task_id IN (SELECT task_id FROM operations INDEXED BY operation_target "
                "WHERE machine=? AND target_identity=?)", [machine, target_identity])
    keys = ("machine", "workspace", "workspace_identity")[:1 + bool(workspace) + bool(workspace_identity)]
    sources = []
    values = []
    for slot in range(4):
        sources.append(f"SELECT task_id FROM tasks INDEXED BY task_target_{slot} WHERE "
                       + " AND ".join(f"json_extract(current,'$.contract.targets[{slot}].{key}')=?"
                                      for key in keys))
        values.extend((machine, workspace, workspace_identity)[:len(keys)])
    if not workspace:
        sources.append("SELECT task_id FROM operations INDEXED BY operation_target WHERE machine=?")
        values.append(machine)
    return "task_id IN (" + " UNION ".join(sources) + ")", values


def _list_row(connection, row, store_identity: str) -> dict:
    value = db.decode(row["current"], row["sha256"])
    if value.get("task_id", row["task_id"]) != row["task_id"] or value["revision"] != row["revision"]:
        raise Fault("corrupt_tasks", "Task list metadata differs from its current authority.")
    structured = row["kind"] == "structured"
    terminal = value.get("terminal") if structured else None
    operations = []
    if structured:
        observed = connection.execute("""SELECT kind,machine,target_identity FROM operations
            WHERE task_id=? AND kind IN ('execution','change') AND machine IS NOT NULL
            GROUP BY kind,machine,target_identity ORDER BY kind,machine,target_identity LIMIT 9""",
            (row["task_id"],)).fetchall()
        operations = [dict(item) for item in observed[:8]]
    return {"task_id": row["task_id"], "title": value["title"], "revision": row["revision"],
            "kind": row["kind"], "state": value["control"]["state"] if structured else None,
            "owner_store_identity": store_identity,
            "targets": value["contract"]["targets"] if structured else [],
            "operation_targets": operations, "operation_targets_complete": not structured or len(observed) <= 8,
            "created_at_ms": value["created_at_ms"] if structured else None,
            "updated_at_ms": value["updated_at_ms"] if structured else None,
            "finished_at_ms": terminal["finished_at_ms"] if terminal else None}


def find(state: Path, query: str = "", anchors: list[str] | None = None, identity: str = "", offset: int = 0,
         limit: int = 20, expected_generation: int = -1, *, machine: str = "",
         workspace: str = "", workspace_identity: str = "", target_identity: str = "") -> dict:
    scoped, scope_args = _target_filter(machine, workspace, workspace_identity, target_identity)
    if identity:
        task_id(identity)
    wanted = validate_metadata("lookup", anchors or [])["anchors"]
    if (not isinstance(query, str) or len(query) > 200 or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 50 or type(expected_generation) is not int or expected_generation < -1):
        raise Fault("invalid_range", "Use a bounded query, nonnegative offset and page size from 1 to 50.")
    with db.connect(state) as connection:
        if connection is None:
            if os.path.lexists(state / "tasks"):
                if scoped:
                    raise Fault("task_migration_required", "Target-scoped discovery needs the canonical Task store.")
                with _legacy_read(state) as root:
                    index = read_index(root)
                    matches = [{"task_id": key, "title": item["title"], "revision": item["revision"],
                                "kind": item.get("owner", "legacy"), "state": None, "targets": [],
                                "operation_targets": [], "operation_targets_complete": False,
                                "owner_store_identity": None, "created_at_ms": None,
                                "updated_at_ms": None, "finished_at_ms": None}
                        for key, item in sorted(index["tasks"].items()) if (not identity or key == identity)
                        and set(wanted) <= set(item["anchors"]) and query.casefold() in (key + " " + item["title"]).casefold()]
                    generation = index["generation"]
                    total, page = len(matches), matches[offset:offset+limit]
            else:
                if expected_generation != -1:
                    raise Fault("task_index_changed", "The original Task database is unavailable.")
                return {"status": "uninitialized", "tasks": [], "next_offset": None, "generation": None,
                        "coverage": "this_store_only", "resume_authorized": False}
        else:
            owner = db.metadata(connection)
            generation = owner["generation"]
            if expected_generation != -1 and generation != expected_generation:
                raise Fault("task_index_changed", "Task lookup changed between pages; use its current generation.")
            if scoped and not db.target_indexes_ready(connection):
                raise Fault("task_index_upgrade_required", "Activate the compatible Task target index before scoped discovery.")
            clauses, arguments = [], []
            if scoped:
                clauses.append(scoped)
                arguments.extend(scope_args)
            if identity:
                clauses.append("task_id=?")
                arguments.append(identity)
            if query:
                clauses.append("instr(title_key,?)>0")
                arguments.append(query.casefold())
            for anchor in wanted:
                clauses.append("EXISTS(SELECT 1 FROM json_each(tasks.anchors) WHERE value=?)")
                arguments.append(anchor)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            total = connection.execute("SELECT count(*) FROM tasks" + where, arguments).fetchone()[0]
            rows = connection.execute("SELECT task_id,kind,current,sha256,revision FROM tasks" + where
                                      + " ORDER BY task_id LIMIT ? OFFSET ?", [*arguments, limit, offset])
            page = [_list_row(connection, row, owner["identity"]) for row in rows]
        if expected_generation != -1 and generation != expected_generation:
            raise Fault("task_index_changed", "Task lookup changed between pages; use its current generation.")
        exact = bool(identity or wanted) and not query
        return {"status": "matched" if exact and total == 1 else "ambiguous" if exact and total > 1 else "candidates",
                "tasks": page, "total": total, "next_offset": offset + len(page) if offset + len(page) < total else None,
                "generation": generation, "coverage": "complete", "resume_authorized": False}


def observation_page(state: Path, cursor: int = 0, limit: int = INDEX_PAGE) -> dict:
    if type(cursor) is not int or cursor < 0 or not 1 <= limit <= INDEX_PAGE:
        raise Fault("invalid_range", "Use an observation sequence and a bounded page.")
    with db.connect(state) as connection:
        if connection is None:
            if os.path.lexists(state / "tasks"):
                raise Fault("task_migration_required", "Migrate legacy Task discovery before running the new observer.")
            return {"entries": [], "cursor": cursor, "generation": None, "has_more": False, "complete": True}
        rows = connection.execute("SELECT task_id,change_seq FROM tasks WHERE kind='structured' AND change_seq>? ORDER BY change_seq LIMIT ?", (cursor, limit + 1)).fetchall()
        entries = []
        for row in rows[:limit]:
            try:
                record, error = _view(connection, row["task_id"])["record"], None
            except Fault as fault:
                record, error = None, fault.code
            entries.append({"task_id": row["task_id"], "sequence": row["change_seq"], "change_seq": row["change_seq"], "record": record, "error": error})
        return {"entries": entries, "cursor": entries[-1]["sequence"] if entries else cursor,
                "generation": db.metadata(connection)["generation"], "has_more": len(rows) > limit, "complete": True}


def has_event(state: Path, identity: str, kind: str, since: int) -> bool:
    with db.connect(state) as connection:
        if connection is None:
            return any(event["kind"] == kind and event["at_ms"] >= since for event in read(state, identity)["record"]["history"])
        return connection.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=? AND at_ms>=? LIMIT 1", (identity, kind, since)).fetchone() is not None


def first_admission(state: Path, identity: str, epoch: int, since: int) -> int | None:
    with db.connect(state) as connection:
        if connection is None:
            return min((op["admitted_at_ms"] for op in read(state, identity)["record"]["operations"]
                        if op["epoch"] == epoch and op["admitted_at_ms"] >= since), default=None)
        row = connection.execute("SELECT admitted_at_ms FROM operations WHERE task_id=? AND epoch=? AND admitted_at_ms>=? ORDER BY admitted_at_ms LIMIT 1", (identity, epoch, since)).fetchone()
        return row[0] if row else None


def locator_matches(state: Path, identity: str, locator: dict) -> bool:
    with db.connect(state) as connection:
        if connection is None:
            return any(all(op["locator"].get(key) == value for key, value in locator.items())
                       for op in read(state, identity)["record"]["operations"])
        rows = connection.execute("SELECT payload,sha256 FROM operations WHERE task_id=? AND machine=? AND locator_id=?",
                                  (identity, locator.get("machine"), locator.get("execution_id", locator.get("change_id"))))
        return any(all(db.decode(row["payload"], row["sha256"])["locator"].get(key) == value for key, value in locator.items()) for row in rows)


def references_target(state: Path, machine: str, target_identity: str) -> bool:
    with db.connect(state) as connection:
        if connection is None:
            if not os.path.lexists(state / "tasks"):
                return False
            with _legacy_read(state) as root:
                for identity, item in read_index(root)["tasks"].items():
                    if item.get("owner") == "structured":
                        record, _ = read_structured(root, identity)
                        if any(target["machine"] == machine for target in record["contract"]["targets"]) or any(
                                op["locator"].get("machine") == machine or target_identity and op["locator"].get("target_identity") == target_identity for op in record["operations"]):
                            return True
                return False
        if connection.execute("SELECT 1 FROM operations WHERE machine=? OR target_identity=? LIMIT 1", (machine, target_identity or "")).fetchone():
            return True
        rows = connection.execute("SELECT current,sha256 FROM tasks WHERE kind='structured'")
        return any(any(item["machine"] == machine for item in db.decode(row["current"], row["sha256"])["contract"]["targets"]) for row in rows)


def references_workspace(state: Path, machine: str, workspace: str) -> bool:
    """Original Task targets are immutable; a saved name cannot be silently rebound."""
    with db.connect(state) as connection:
        if connection is None:
            if not os.path.lexists(state / "tasks"):
                return False
            with _legacy_read(state) as root:
                return any(any(target["machine"] == machine and target["workspace"] == workspace
                               for target in read_structured(root, identity)[0]["contract"]["targets"])
                           for identity, item in read_index(root)["tasks"].items()
                           if item.get("owner") == "structured")
        rows = connection.execute("SELECT current,sha256 FROM tasks WHERE kind='structured'")
        return any(any(target["machine"] == machine and target["workspace"] == workspace
                       for target in db.decode(row["current"], row["sha256"])["contract"]["targets"])
                   for row in rows)


def activation_blockers(state: Path):
    if os.path.lexists(state / "tasks"):
        with _legacy_read(state) as root:
            for identity, item in read_index(root)["tasks"].items():
                if item.get("owner") == "structured":
                    record, _ = read_structured(root, identity)
                    for op in record["operations"]:
                        if effects.unresolved(op):
                            yield op
        return
    with db.connect(state) as connection:
        if connection is None:
            return
        for row in connection.execute("SELECT * FROM operations WHERE unresolved=1"):
            yield decode_operation(row)


def save(state: Path, identity: str, expected_revision: int, title: str, body: str, anchors: list[str]) -> dict:
    task_id(identity)
    meta = validate_metadata(title, anchors)
    raw = body.encode()
    if type(expected_revision) is not int or expected_revision < 0:
        raise Fault("invalid_revision", "Supply the observed nonnegative checkpoint revision; zero means new.")
    if not raw.strip() or len(raw) > MAX_CHECKPOINT or b"\0" in raw or redact(body) != body:
        raise Fault("invalid_checkpoint", "Use a nonempty checkpoint of at most 32 KiB without credentials or NUL bytes.")
    with db.connect(state, write=True, initialize=expected_revision == 0) as connection:
        if connection is None:
            raise Fault("corrupt_tasks", "The original checkpoint store is missing.")
        row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (identity,)).fetchone()
        revision = 0
        if row is not None:
            if row["kind"] != "legacy":
                raise Fault("task_conflict", "Legacy save cannot convert structured authority.")
            old = db.decode(row["current"], row["sha256"])
            revision = old["revision"]
            meta = validate_metadata(meta["title"], sorted(set(old["anchors"]) | set(meta["anchors"])))
            if body == old["body"] and meta == {key: old[key] for key in ("title", "anchors")}:
                if expected_revision not in (revision, revision - 1):
                    raise Fault("task_conflict", "This retry differs from the retained checkpoint revision.")
                return {**_view(connection, identity), "status": "unchanged"}
        if expected_revision != revision:
            raise Fault("task_conflict", "Read the current checkpoint revision before replacing it.")
        if row is None:
            db.admission_space(state)
        value = {**meta, "revision": revision + 1, "sha256": hashlib.sha256(raw).hexdigest(),
                 "body": body, "content_redacted": False}
        put_legacy(connection, identity, value)
        result = _view(connection, identity)
    return {**result, "status": "saved"}


def present(view: dict) -> dict:
    """Public v2 current-state snapshot; historical rows are explicit separate pages."""
    if "record" not in view:
        return {**view, "read_schema": 2, "section": "summary", "complete": True, "has_more": False}
    record = view["record"]
    facts = view.get("facts", record.get("_facts", {}))
    counts = {name: facts.get(name, len(record.get(field, []))) for name, field in (
        ("operation_count", "operations"), ("history_count", "history"), ("scope_count", "effect_scopes"))}
    counts.update({name: facts.get(name, 0) for name in ("pending_count", "unresolved_count", "unknown_count", "partial_count")})
    result = {key: value for key, value in view.items() if key != "facts"}
    if view.get("storage") == "legacy":
        result.update(record={key: value for key, value in record.items() if key not in {"_facts", "_decisions"}},
                      read_schema=2, section="summary", counts=counts, current_record_complete=True,
                      record_complete=True, complete=True, has_more=False)
        return result
    current = deepcopy(record_core(record))
    for stage in current["plan"]["stages"]:
        if stage["id"] in record.get("_decisions", {}):
            stage.pop("evidence")
            stage["evidence_ref"] = {"task_id": record["task_id"], "section": "evidence", "stage_id": stage["id"],
                                     "decision_version": record["_decisions"][stage["id"]]}
    result.update(record=current, read_schema=2, section="summary", counts=counts,
                  current_record_complete=True, record_complete=False, complete=True, has_more=False,
                  history_ref={"task_id": record["task_id"], "section": "history"},
                  operations_ref={"task_id": record["task_id"], "section": "operations"},
                  unresolved_operation_ids=[op["id"] for op in record["operations"] if effects.unresolved(op)])
    return result


def public_read(state: Path, identity: str, *, section: str = "summary", cursor: str = "", limit: int = 20,
                operation_id: str = "", expected_revision: int = -1, stage_id: str = "", decision_version: int = 0) -> dict:
    import base64
    if (section not in {"summary", "operations", "history", "evidence"} or type(limit) is not int
            or not 1 <= limit <= records.MAX_PAGE_ITEMS or not isinstance(cursor, str) or len(cursor) > 1024
            or operation_id and (len(operation_id) != 32 or any(c not in "0123456789abcdef" for c in operation_id))):
        raise Fault("invalid_task_read", "Use a bounded Task section, cursor and exact original operation ID.")
    if stage_id or decision_version:
        if (section != "evidence" or cursor or operation_id or not stage_id
                or type(decision_version) is not int or not 1 <= decision_version <= records.MAX_INTEGER):
            raise Fault("invalid_task_read", "An exact stage decision requires its stage and immutable version on the evidence section.")
        task_id(stage_id)
    if section == "summary":
        if cursor or operation_id:
            raise Fault("invalid_task_read", "Summary reads do not select historical cursors or operations.")
        return present(read(state, identity))
    if operation_id and (section != "operations" or cursor):
        raise Fault("invalid_task_read", "Exact original operation lookup uses the operations section without a cursor.")
    with db.connect(state) as connection:
        if connection is None:
            raise Fault("task_migration_required", "Legacy history remains readable through its original summary until managed migration.")
        row = task_row(connection, identity)
        if row["kind"] != "structured":
            raise Fault("task_legacy", "A Markdown checkpoint has no supervised operation ledger.")
        if expected_revision != -1 and expected_revision != row["revision"]:
            raise Fault("task_conflict", "Task state changed before its evidence page; refresh the current snapshot.")
        owner = db.metadata(connection)
        if stage_id:
            if not connection.execute("SELECT 1 FROM stage_decisions WHERE task_id=? AND stage_id=? AND version=?",
                                      (identity, stage_id, decision_version)).fetchone():
                raise Fault("task_evidence_not_found", "No retained decision exists for this stage and version.")
            decision = read_decision(connection, identity, stage_id, decision_version)
            return {"task_id": identity, "read_schema": 2, "section": "evidence", "task_revision": row["revision"],
                    "generation": owner["generation"], "complete": True, "has_more": False, "next_cursor": None,
                    "items": [{"kind": "stage_decision", "stage_id": stage_id, "decision_version": decision_version, **decision}]}
        ceiling = row["operation_count"] if section == "operations" else row["history_count"]
        after = 0
        if cursor:
            try:
                token = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if (set(token) != {"store", "task", "section", "after", "ceiling", "revision"}
                        or (token["store"], token["task"], token["section"]) != (owner["identity"], identity, section)
                        or type(token["after"]) is not int or type(token["ceiling"]) is not int
                        or not 0 <= token["after"] <= token["ceiling"] <= ceiling):
                    raise ValueError("cursor identity differs")
                if section == "operations" and token["revision"] != row["revision"]:
                    raise Fault("task_conflict", "Mutable operation evidence changed between pages; refresh its revision.")
                after, ceiling = token["after"], token["ceiling"]
            except (ValueError, TypeError, KeyError) as error:
                raise Fault("invalid_task_read", "The evidence cursor does not match this Task, section and watermark.") from error
        if section == "operations":
            clause, args = ("id=?", [operation_id]) if operation_id else ("ordinal>? AND ordinal<=?", [after, ceiling])
            rows = connection.execute("SELECT * FROM operations WHERE task_id=? AND " + clause + " ORDER BY ordinal LIMIT ?", [identity, *args, limit + 1]).fetchall()
            decode = lambda item: {"sequence": item["ordinal"], **decode_operation(item)}
            sequence = "ordinal"
        else:
            selected = " AND kind NOT IN ('operation_admitted','operation_observed')" if section == "evidence" else ""
            rows = connection.execute("SELECT * FROM task_events WHERE task_id=? AND seq>? AND seq<=?" + selected + " ORDER BY seq LIMIT ?", (identity, after, ceiling, limit + 1)).fetchall()
            decode = lambda item: {"sequence": item["seq"], "kind": item["kind"], "at_ms": item["at_ms"], **db.decode(item["payload"], item["sha256"])}
            sequence = "seq"
        items, size = [], 0
        for item in rows[:limit]:
            value = decode(item)
            length = len(item["payload"].encode())
            if size + length > records.MAX_PAGE_BYTES:
                if not items:
                    raise Fault("corrupt_tasks", "An evidence item exceeds its supported page bound; preserve its original bytes.")
                break
            items.append(value)
            size += length
            after = item[sequence]
        more = len(rows) > len(items)
        token = {"store": owner["identity"], "task": identity, "section": section, "after": after,
                 "ceiling": ceiling, "revision": row["revision"]}
        next_cursor = base64.urlsafe_b64encode(records.canonical(token)).decode().rstrip("=") if more else None
        return {"task_id": identity, "read_schema": 2, "section": section, "items": items,
                "task_revision": row["revision"], "generation": owner["generation"], "watermark": ceiling,
                "complete": not more, "has_more": more, "next_cursor": next_cursor}


def check_state(state: Path) -> dict:
    """Explicit compatibility proof scans retained rows; ordinary reads never do."""
    storage = db.storage_state(state)
    count, structured, formats, index_format = 0, 0, set(), None
    if storage["kind"] == "legacy":
        with _legacy_read(state) as root:
            index = read_index(root)
            index_format = index["format_version"]
            for identity, item in index["tasks"].items():
                if item.get("owner") == "structured":
                    record, raw = read_structured(root, identity)
                    if item["sha256"] != hashlib.sha256(raw).hexdigest():
                        raise Fault("task_index_incomplete", "Legacy metadata differs from canonical Task evidence.")
                    formats.add(record["format"])
                    structured += 1
                else:
                    read_checkpoint(root, index, identity)
                count += 1
    elif storage["kind"] == "sqlite":
        with db.connect(state, maintenance=True) as connection:
            storage["generation"] = db.metadata(connection)["generation"]
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
                raise Fault("corrupt_tasks", "The authoritative Task database failed its integrity check.")
            for row in connection.execute("SELECT * FROM tasks ORDER BY task_id"):
                count += 1
                value = db.decode(row["current"], row["sha256"])
                if row["kind"] == "legacy":
                    if hashlib.sha256(value["body"].encode()).hexdigest() != value["sha256"]:
                        raise Fault("corrupt_tasks", "A legacy checkpoint differs from its original body hash.")
                    continue
                core, _ = read_current(connection, row)
                formats.add(core["format"])
                structured += 1
                stage_counts = {stage["id"]: 0 for stage in core["plan"]["stages"]}
                operations = 0
                for item in connection.execute("SELECT * FROM operations WHERE task_id=? ORDER BY ordinal", (row["task_id"],)):
                    op = validate_operation(decode_operation(item), core)
                    operations += 1
                    if item["ordinal"] != operations:
                        raise Fault("corrupt_tasks", "An original operation ordinal is missing.")
                    stage_counts[op["stage_id"]] += 1
                    scope = connection.execute("SELECT * FROM operation_scopes WHERE task_id=? AND operation_id=?", (row["task_id"], op["id"])).fetchone()
                    if op.get("effect_scope") is not None:
                        if scope is None or scope["witness"] is not None or db.decode(scope["scope"], scope["sha256"]) != op["effect_scope"]:
                            raise Fault("corrupt_tasks", "An original declared scope differs from its conflict index.")
                    elif scope is not None:
                        read_witnesses(connection, row["task_id"], [op])
                events = 0
                for item in connection.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY seq", (row["task_id"],)):
                    event = db.decode(item["payload"], item["sha256"])
                    if item["kind"] != "snapshot" and any(event.get(key) != item[key] for key in ("kind", "at_ms", "stage_id", "operation_id")):
                        raise Fault("corrupt_tasks", "Audit lookup columns differ from their retained event.")
                    if item["kind"] == "snapshot" and event.get("task_id") != row["task_id"]:
                        raise Fault("corrupt_tasks", "The initial authority snapshot belongs to another Task.")
                    events += 1
                    if item["seq"] != events:
                        raise Fault("corrupt_tasks", "An original audit event is missing.")
                if operations != row["operation_count"] or events != row["history_count"] or stage_counts != json.loads(row["stage_counts"]):
                    raise Fault("corrupt_tasks", "Task counts do not cover the retained authority.")
                witnesses = connection.execute("SELECT count(*) FROM operation_scopes WHERE task_id=? AND witness IS NOT NULL", (row["task_id"],)).fetchone()[0]
                if witnesses != row["scope_count"] or connection.execute("SELECT count(*) FROM operations WHERE task_id=? AND unresolved=1", (row["task_id"],)).fetchone()[0] > records.MAX_ACTIVE_OPERATIONS:
                    raise Fault("corrupt_tasks", "Scope or active-work counts differ from retained authority.")
                for decision in connection.execute("SELECT stage_id,version FROM stage_decisions WHERE task_id=?", (row["task_id"],)):
                    if not 1 <= decision["version"] <= core["revision"]:
                        raise Fault("corrupt_tasks", "A decision version exceeds its Task revision.")
                    read_decision(connection, row["task_id"], decision["stage_id"], decision["version"])
                last = connection.execute("SELECT request_sha256 FROM task_updates WHERE task_id=? AND id=?", (row["task_id"], core["last_update"]["id"])).fetchone()
                if last is None or last[0] != core["last_update"]["request_sha256"]:
                    raise Fault("corrupt_tasks", "The last control lost its retained request fingerprint.")
    return {"task_store_present": storage["kind"] != "absent", "task_records": count,
            "structured_task_records": structured, "structured_task_read_formats": list(records.READ_FORMATS),
            "structured_task_formats_present": sorted(formats), "task_index_format": index_format,
            "task_index_generation": storage["generation"], "task_index_coverage": "complete", "task_storage": storage}


def rebuild_index(state: Path, expected_generation: int = -1) -> dict:
    """Retain the maintenance entry; SQLite indexes commit with their authority."""
    info = check_state(state)
    if expected_generation != -1 and info["task_index_generation"] != expected_generation:
        raise Fault("task_index_changed", "The original Task generation changed.")
    if info["task_storage"]["kind"] == "legacy":
        raise Fault("task_migration_required", "Use managed migration for a verified legacy store; do not create a second active index.")
    return {"status": "current", "generation": info["task_index_generation"], "coverage": "complete",
            "next_cursor": None, "records_checked": info["task_records"]}
