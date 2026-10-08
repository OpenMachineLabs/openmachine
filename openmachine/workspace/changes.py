"""Scoped change application and readback through the original guarded publisher."""

import hashlib
import os
from pathlib import Path
import time
import uuid
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from .files import MAX_SOURCE, digest, publish_file, publish_path, root_for, snapshot
from .git import context, lock_root
from .worktrees import resolve_root
from . import change_records as records
from . import preparation


def _bound_source(source, effects):
    if not source.get("missing_parents"):
        return source
    created = {effect["path"]: effect["result_identity"] for effect in effects
               if effect.get("kind") == "mkdir" and effect["stage"] == "applied"}
    if any(path not in created for path in source["missing_parents"]):
        raise Fault("parent_unverified", "Every new ancestor needs its original verified creation effect.")
    return source | {"ancestors": source["ancestors"] + [tuple(created[path]) for path in source["missing_parents"]]}


def _scope(config, state: Path, machine: str, record: dict) -> str:
    config.machine(machine)
    if records._owner(state) != record["owner_sha256"]:
        raise Fault("scope_unavailable", "This receipt belongs to its original target state owner.")
    for name, item in config.workspaces.items():
        if item.machine != machine:
            continue
        try:
            root = root_for(config, machine, name)
            if str(root) != record["context"]["root"]:
                root = resolve_root(config, machine, name, record["context"]["root"])
            info = root.stat()
            if str(root) == record["context"]["root"] and [info.st_dev, info.st_ino] == record["context"]["root_id"]:
                return name
        except (OSError, Fault):
            continue
    raise Fault("scope_unavailable", "Current configuration no longer grants the original canonical workspace.")


def read_change(config, state: Path, machine: str, change_id: str, observe_current: bool = False) -> dict:
    config.machine(machine)
    if not records._hex(change_id, 32) or type(observe_current) is not bool:
        raise Fault("invalid_change_id", "Use the original 32-lowercase-hex change ID and a boolean observation flag.")
    store = records._store(state)
    if store is None or not os.path.lexists(store / change_id):
        raise Fault("change_not_found", "No retained record was found; absence is not proof of non-admission.")
    directory = store / change_id
    try:
        record = records._observed(directory)
    except FileNotFoundError as error:
        raise Fault("change_history_unavailable", "The original allocation is incomplete; never reuse this ID.") from error
    name = _scope(config, state, machine, record)
    result = record | {"machine": machine, "workspace": name}
    if observe_current and record["format"] == 2:
        current, total = [], 0
        for item in record["expected"]["preconditions"]:
            try:
                source = snapshot(Path(record["context"]["root"]), item["path"],
                                  max_bytes=min(MAX_SOURCE, records.MAX_PREPARED - total), allow_missing=True)
                total += len(source["raw"] or b"")
                current.append({"path": item["path"], "exists": source["raw"] is not None,
                                "sha256": digest(source["raw"]), "context": preparation._context(source)})
            except (OSError, Fault) as error:
                current.append({"path": item["path"], "error": error.code if isinstance(error, Fault) else type(error).__name__})
        result["current"] = {"files": current, "observed_at": time.time(), "complete": all("error" not in item for item in current)}
    elif observe_current:
        try:
            source = snapshot(Path(record["context"]["root"]), record["expected"]["path"])
            result["current"] = {"exists": source["raw"] is not None, "sha256": digest(source["raw"]),
                                 "observed_at": time.time(), "context": preparation._context(source)}
        except (OSError, Fault) as error:
            result["current"] = {"error": error.code if isinstance(error, Fault) else type(error).__name__}
    return result


def _apply(config, state, machine, workspace, request, change_id, dry_run, validate_new, operations=None):
    root = root_for(config, machine, workspace)
    if dry_run:
        plan, sources, desired = preparation._prepared(config, machine, workspace, root, request, operations)
        # Use the actual record shape for pure preview sizing without allocating
        # a state owner, change identity, lease, temporary file or parent.
        sizing = records._initial_record(request, "0" * 32, plan["context"], "0" * 64, "0" * 64)
        sizing.update(plan=plan, plan_sha256=records._hash(plan), effects=records._effects(plan, "0" * 32), status="prepared")
        records._record_budget(sizing)
        target = config.machine(machine)
        grant = target.workspace_write_enabled
        effective = target.execution_enabled if grant is None else grant
        return {"machine": machine, "workspace": workspace, "status": "preview", "plan": plan,
                "plan_sha256": records._hash(plan), "workspace_write_enabled": effective, "diff_kind": "proposed",
                **preparation._display(plan, sources, desired)}
    fingerprint = records._hash(request | {"root": str(root)})
    # Reconcile retained identities before mutable source, grants or new leases.
    store = records._store(state)
    if store is not None and os.path.lexists(store / change_id):
        original = read_change(config, state, machine, change_id)
        if original["request_sha256"] != fingerprint:
            raise Fault("change_id_conflict", "This change ID already belongs to a different immutable request.")
        return original
    config.workspace_machine(machine)
    observed = context(root)
    if observed["workspace_identity"] is None:
        raise Fault("context_unavailable", "Workspace identity is unavailable; no change was admitted.")
    record = records._initial_record(request, change_id, preparation._context({"root": root, "context": observed}),
                             "0" * 64, fingerprint)
    # Reject oversized admission/rejection evidence before reserving an identity.
    # The actual owner has the same encoded width; prepared plans are sized later.
    records._record_budget(record)
    patching = operations is not None
    with records._own(state, change_id) as (directory, existing):
        if existing:
            original = read_change(config, state, machine, change_id)
            if original["request_sha256"] != fingerprint:
                raise Fault("change_id_conflict", "This change ID belongs to a different request.")
            return original
        record["owner_sha256"] = records._owner(state)
        records._save(directory, record)
        try:
            if validate_new is not None:
                validate_new(root)
            key = hashlib.sha256(lock_root(config, machine, root, observed).encode()).hexdigest()
            with lease(state / "locks" / f"workspace-{key}.lock"):
                plan, sources, desired = preparation._prepared(config, machine, workspace, root, request, operations)
                if plan["context"] != record["context"]:
                    raise Fault("context_mismatch", "Workspace context changed after request admission.")
                prepared = record | {"plan": plan, "plan_sha256": records._hash(plan), "effects": records._effects(plan, change_id), "status": "prepared"}
                records._record_budget(prepared)
                record.update(prepared)
                records._save(directory, record)
                display = preparation._display(plan, sources, desired)
                modes = {item["path"]: item["after_mode"] for item in plan["files"]} if patching else {}
                for effect in record["effects"]:
                    kind, path = effect.get("kind", "publish"), effect["path"]
                    source = _bound_source(sources[path], record["effects"])
                    if kind == "publish" and source["raw"] == desired[path]:
                        effect.update(stage="unchanged", result_sha256=digest(desired[path]), readback_verified=True)
                    else:
                        record["status"] = "applying"
                        effect["stage"] = "attempting"
                        records._save(directory, record)
                        if kind == "publish":
                            arguments = {"mode": modes[path]} if patching else {}
                            observation = publish_file(config, machine, workspace, source, desired[path], effect["temporary"], **arguments)
                        else:
                            observation = publish_path(config, machine, workspace, source, kind)
                        effect.update(observation)
                        if effect["stage"] != "applied" or effect["cleanup"] not in {"removed", "not_created"}:
                            record["error"] = effect["error"]
                            break
                    if patching:
                        record["status"] = "applying"
                        records._save(directory, record)
                effects = record["effects"]
                if any(effect["stage"] in {"unknown", "attempting"} or effect["cleanup"] == "unknown" for effect in effects):
                    outcome = "outcome_unknown"
                elif all(effect["stage"] in {"applied", "unchanged"} for effect in effects):
                    outcome = "applied" if any(effect["stage"] == "applied" for effect in effects) else "unchanged"
                else:
                    outcome = "partial" if any(effect["stage"] == "applied" for effect in effects) else "rejected"
                record.update(status=outcome, finished_at=time.time())
                records._save(directory, record)
                result = records._observed(directory, record, owned=True) | {"machine": machine, "workspace": workspace}
                if outcome in {"applied", "unchanged"}:
                    result.update(display, diff_kind="applied")
                else:
                    result.update(diff_omitted=True)
                return result
        except (OSError, Fault) as error:
            code = error.code if isinstance(error, Fault) else type(error).__name__
            uncertain = code == "change_record_unverified" or any(e["stage"] in {"attempting", "unknown"} or
                        e["cleanup"] == "unknown" for e in record["effects"])
            applied = any(e["stage"] == "applied" for e in record["effects"])
            record.update(status="outcome_unknown" if uncertain else "partial" if applied else "rejected",
                          error=code, finished_at=time.time())
            if not uncertain:
                records._save(directory, record)
                return records._observed(directory, record, owned=True) | {"machine": machine, "workspace": workspace}
            raise Fault("change_outcome_unknown", "Read this original change and current files; never replay uncertain effects.",
                        record | {"record_persistence": "unverified", "machine": machine, "workspace": workspace}) from error


def apply_patch(config, state: Path, machine: str, workspace: str, patch_text: str,
                expected_workspace_identity: str, expected_head_sha: str, preconditions: list[dict],
                create_parents: bool = False, change_id: str = "", dry_run: bool = False,
                expected_plan_sha256: str = "", *, validate_new=None) -> dict:
    try:
        if type(dry_run) is not bool or (change_id != "" if dry_run else not records._hex(change_id, 32)):
            raise Fault("invalid_change_id", "Preview requires no ID; apply requires a new 32-lowercase-hex ID.")
        request, operations = preparation._patch_request(patch_text, preconditions, expected_workspace_identity,
                                             expected_head_sha, create_parents, expected_plan_sha256)
        return _apply(config, state, machine, workspace, request, change_id, dry_run, validate_new, operations)
    except Fault as error:
        error.evidence = {"machine": machine, "workspace": workspace, "change_id": change_id, **(error.evidence or {})}
        raise


def edit_text_file(config, state: Path, machine: str, workspace: str, path: str, edits: list[dict],
                   expected_workspace_identity: str, expected_head_sha: str, expected_sha256: str,
                   change_id: str = "", dry_run: bool = False, expected_plan_sha256: str = "",
                   *, validate_new=None) -> dict:
    try:
        if type(dry_run) is not bool or (change_id != "" if dry_run else not records._hex(change_id, 32)):
            raise Fault("invalid_change_id", "Preview requires no ID; apply requires a new 32-lowercase-hex ID.")
        request = preparation._request("edit", path, edits, expected_workspace_identity, expected_head_sha, expected_sha256, expected_plan_sha256)
        return _apply(config, state, machine, workspace, request, change_id, dry_run, validate_new)
    except Fault as error:
        error.evidence = {"machine": machine, "workspace": workspace, "change_id": change_id,
                          **(error.evidence or {})}
        raise


def write_text_file(config, state: Path, machine: str, workspace: str, path: str, content: str,
                    expected_head_sha: str, expected_sha256: str, change_id: str = "",
                    expected_workspace_identity: str = "", *, validate_new=None) -> dict:
    if not records._hex(change_id, 32, empty=True) or not records._hex(expected_workspace_identity, 64, empty=True):
        raise Fault("invalid_precondition", "Use a valid change ID and workspace identity, or explicit empty legacy values.")
    if change_id:
        try:
            request = preparation._request("write", path, content, expected_workspace_identity, expected_head_sha, expected_sha256, "")
            return _apply(config, state, machine, workspace, request, change_id, False, validate_new)
        except Fault as error:
            error.evidence = {"machine": machine, "workspace": workspace, "change_id": change_id, **(error.evidence or {})}
            raise
    config.workspace_machine(machine)
    if not records._hex(expected_head_sha, 40, empty=True) or not records._hex(expected_sha256, 64, empty=True):
        raise Fault("invalid_precondition", "Supply the observed HEAD and whole-file hash; empty means absent.")
    desired = preparation._text(content)
    root = root_for(config, machine, workspace)
    observed = context(root)
    key = hashlib.sha256(lock_root(config, machine, root, observed).encode()).hexdigest()
    private_directory(state)
    private_directory(state / "locks")
    if validate_new is not None:
        validate_new(root)
    with lease(state / "locks" / f"workspace-{key}.lock"):
        source = snapshot(root, path)
        if (source["context"]["head_sha"] or "") != expected_head_sha:
            raise Fault("context_mismatch", "Git HEAD changed; inspect the original workspace.")
        result = {"machine": machine, "workspace": workspace, "path": path, "head_sha": source["context"]["head_sha"],
                  "sha256": digest(desired), "previous_sha256": digest(source["raw"]), "size_bytes": len(desired),
                  "durable_recovery": False}
        if source["raw"] == desired:
            return result | {"status": "already_current"}
        if digest(source["raw"]) != expected_sha256:
            raise Fault("content_mismatch", "The original source changed; do not overwrite it.")
        effect = publish_file(config, machine, workspace, source, desired, ".openmachine-edit-" + uuid.uuid4().hex)
        if effect["stage"] == "failed_no_effect" and effect["cleanup"] != "unknown":
            raise Fault(effect["error"] or "publication_failed", "No file replacement was performed.")
        return result | {"status": "written" if effect["stage"] == "applied" and effect["cleanup"] == "removed" else "write_outcome_unknown",
                         "effect": effect}


def bound_result(payload: dict, encoded_size, limit: int = records.MAX_WORKSPACE_RESULT) -> dict:
    """Bound presentation without skipping source bytes or captured entries."""
    result = dict(payload)
    while encoded_size(result) > limit and result.get("diff"):
        raw = result["diff"].encode()
        result["diff"] = raw[:len(raw) // 2].decode("utf-8", errors="ignore")
        result["truncated"] = True
    for field in ("matches", "entries"):
        while encoded_size(result) > limit and len(result.get(field, [])) > 1:
            result[field] = result[field][:len(result[field]) // 2]
            result.update(next_offset=result["offset"] + len(result[field]), has_more=True,
                          presentation_truncated=True)
    # Text pages are already bounded by their source owner. Further clipping here
    # would lose the raw cursor mapping after redaction; reject without advancing.
    if encoded_size(result) > limit:
        evidence = {"change_id": result["change_id"], "status": "outcome_unknown"} if result.get("change_id") else None
        raise Fault("workspace_response_limit", "Workspace metadata exceeds its response budget; narrow the observation.", evidence)
    return result
