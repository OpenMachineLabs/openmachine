"""Canonical task publication, bounded derived lookup, and unchanged legacy saves."""

from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.foundation.sanitize import Fault, redact

from . import effects, records
from .index import (MAX_CHECKPOINT, MAX_INDEX, MAX_TASKS, INDEX_PAGE, catalog_complete, directory_names,
                    load_index, metadata as validate_metadata, private_path, read_checkpoint, read_index,
                    read_structured, structured_metadata, task_id)


@contextmanager
def _reading(state: Path):
    if not state.is_absolute():
        raise Fault("invalid_state", "Use an absolute private state directory.")
    root = state / "tasks"
    try:
        state.lstat()
    except FileNotFoundError:
        yield None
        return
    private_path(state, directory=True)
    try:
        root.lstat()
    except FileNotFoundError:
        if os.path.lexists(state / "locks/tasks.lock"):
            raise Fault("corrupt_tasks", "The existing task store disappeared; preserve its original history.")
        yield None
        return
    try:
        private_path(root, directory=True)
        private_path(state / "locks", directory=True)
        private_path(state / "locks/tasks.lock")
        with lease(state / "locks/tasks.lock", shared=True, create=False):
            yield root
    except OSError as error:
        raise Fault("corrupt_tasks", "Existing task state or its lock is unavailable; preserve it.") from error


@contextmanager
def _writing(state: Path, *, initialize: bool):
    if not initialize:
        with _reading(state) as root:
            if root is None:
                raise Fault("tasks_uninitialized", "No task store exists to rebuild.")
    state = private_directory(state)
    root = state / "tasks"
    existing = os.path.lexists(root)
    if existing:
        try:
            private_path(state / "locks", directory=True)
            private_path(state / "locks/tasks.lock")
        except OSError as error:
            raise Fault("corrupt_tasks", "The existing task-store lock is missing; never recreate it over prior history.") from error
    locks = private_directory(state / "locks")
    previous_store = os.path.lexists(locks / "tasks.lock")
    with lease(locks / "tasks.lock", create=not existing):
        private_path(locks / "tasks.lock")
        try:
            root.lstat()
        except FileNotFoundError:
            if previous_store:
                raise Fault("corrupt_tasks", "A prior task store is missing; do not initialize new history.")
            private_directory(root)
            save_json(root / "index.json", {"format_version": 1, "generation": 0, "tasks": {}})
            _sync_directory(locks)
            _sync_directory(state)
        private_path(root, directory=True)
        yield root


def _view(root: Path, record: dict, raw: bytes, *, index_status: str | None = None) -> dict:
    if index_status is None:
        try:
            index = load_index(root)
            complete = catalog_complete(root, index)
            expected = structured_metadata(record, raw)
            index_status = "current" if complete and index["tasks"].get(record["task_id"]) == expected else "stale"
            if index.get("coverage") == "incomplete":
                index_status = "incomplete"
        except Fault:
            index_status = "stale"
    return {"task_id": record["task_id"], "record": record, "observed_at_ms": time.time_ns() // 1000000,
            "index_status": index_status,
            "support": {"structured_records": True, "supervised_admission": True,
                        "runtime_recovery": False, "events_delivery": False},
            "observations": [{"kind": "caller", "state": "unknown", "source": "not_observed",
                "observed_at_ms": None, "last_progress_at_ms": None, "next_attempt_at_ms": None,
                "blocker": "Caller continuation is not observed; native-account claims are not ChatGPT presence or automatic wake."}]}


@contextmanager
def locked_read(state: Path, identity: str):
    """Hold a canonical read stable during a short, non-network admission."""
    task_id(identity)
    with _reading(state) as root:
        if root is None:
            raise Fault("tasks_uninitialized", "This private task store has not been initialized.")
        if os.path.lexists(root / identity / "task.json"):
            record, raw = read_structured(root, identity)
            yield _view(root, record, raw)
            return
        index = read_index(root)
        if index["tasks"].get(identity, {}).get("owner") == "structured":
            raise Fault("corrupt_tasks", "The canonical record is missing; legacy fallback is forbidden.")
        item, raw = read_checkpoint(root, index, identity)
        body = raw.decode("utf-8")
        yield {"task_id": identity, **item, "body": redact(body), "content_redacted": redact(body) != body,
                "generation": index["generation"], "resume_authorized": False}


def read(state: Path, identity: str) -> dict:
    with locked_read(state, identity) as view:
        return view


def update(state: Path, identity: str, transform: Callable[[dict, int], None], *,
           expected_revision: int | None = None, last_update: dict | None = None,
           require_current: bool = False, allow_observations: bool = False,
           observation: bool = False, upgrade: bool = False) -> dict:
    """Publish one guarded canonical revision. The callback must not perform IO."""
    task_id(identity)
    with _writing(state, initialize=False) as root:
        original, before = read_structured(root, identity)
        if last_update is not None and original["last_update"]["id"] == last_update["id"]:
            if original["last_update"] != last_update:
                raise Fault("task_conflict", "The retained update ID belongs to a different normalized request.")
            return {**_view(root, original, before), "status": "unchanged"}
        compatible_revision = (allow_observations and original["format"] >= 4
                               and original["control_revision"] <= expected_revision <= original["revision"]) if expected_revision is not None else False
        if expected_revision is not None and expected_revision != original["revision"] and not compatible_revision:
            raise Fault("task_conflict", "The original task advanced; read its current revision before another transition.",
                        {"task_id": identity, "task_revision": original["revision"],
                         "plan_revision": original["plan"]["revision"], "continuation_epoch": original["control"]["epoch"]})
        index = None
        try:
            index = read_index(root)
            if index["tasks"].get(identity) != structured_metadata(original, before):
                raise Fault("task_index_incomplete", "The canonical task and derived index differ; reconcile metadata before new work.")
        except (OSError, Fault):
            if require_current:
                raise
            index = None
        value = deepcopy(original)
        if upgrade and value["format"] == 3:
            value.update(format=4, control_revision=original["revision"], effect_scopes=[])
        now_ms = max(time.time_ns() // 1000000, original["updated_at_ms"])
        transform(value, now_ms)
        if last_update is not None:
            value["last_update"] = last_update
        if value == original:
            return {**_view(root, original, before), "status": "unchanged"}
        if upgrade and value["format"] == 3:
            value.update(format=4, control_revision=original["revision"])
            value.setdefault("effect_scopes", [])
        if value["format"] != original["format"] and not (
                original["format"] == 3 and value["format"] == 4 and upgrade
                or original["format"] < 3 and value["format"] in {3, 4}
                and value["contract"].get("recovery_policy") in {"task_chat", "disabled"}
                and value["plan"]["revision"] == original["plan"]["revision"] + 1):
            raise Fault("task_identity_conflict", "Only an explicit plan revision may enroll historical task recovery.")
        if original.get("chat_registration") is not None and value.get("chat_registration") != original["chat_registration"]:
            raise Fault("task_identity_conflict", "Retain the original recovery registration and its evidence.")
        for key in ("task_id", "title", "anchors", "created_at_ms", "origin"):
            if value[key] != original[key]:
                raise Fault("task_identity_conflict", "A transition cannot replace the original task identity or initiating request.")
        old_witnesses = original.get("effect_scopes", [])
        if value.get("effect_scopes", [])[:len(old_witnesses)] != old_witnesses:
            raise Fault("task_identity_conflict", "Original scope evidence is immutable and append-only.")
        old_operations = {item["id"]: item for item in original["operations"]}
        if not old_operations.keys() <= {item["id"] for item in value["operations"]}:
            raise Fault("task_identity_conflict", "Original operation history cannot be removed.")
        for operation in value["operations"]:
            old = old_operations.get(operation["id"])
            if old is None:
                continue
            for key in ("stage_id", "plan_revision", "epoch", "request_sha256", "source_sha256", "admitted_at_ms", "effect_scope"):
                if operation.get(key) != old.get(key):
                    raise Fault("task_identity_conflict", "Original operation intent and scope cannot be rewritten.")
            if (old["locator"]["kind"] == "github" and old["result"] is not None and not records.pending(old)
                    and operation != old):
                raise Fault("task_identity_conflict", "Verified historical provider evidence cannot follow mutable current state.")
        value.update(revision=original["revision"] + 1, updated_at_ms=now_ms)
        if value["format"] >= 4:
            material = not observation or any(value[key] != original[key] for key in ("contract", "plan", "terminal"))
            # Observation may publish a result/handoff, but never ownership or a
            # new authority/control intent under the evidence-only watermark.
            material |= any(value["control"][key] != original["control"][key]
                            for key in ("owner", "epoch", "stop_intent", "current_stage_id"))
            value["control_revision"] = value["revision"] if material else original.get("control_revision", original["revision"])
        try:
            value = records.Record.model_validate(value).model_dump()
            raw = records.canonical(value)
            if len(raw) > records.MAX_RECORD:
                raise ValueError("record capacity exceeded")
        except (ValueError, TypeError, UnicodeError) as error:
            raise Fault("invalid_task_transition", "The bounded canonical transition is invalid; original task bytes are unchanged.") from error
        try:
            _replace(root / identity / "task.json", raw)
            observed, content = read_structured(root, identity)
            if observed != value or content != raw:
                raise Fault("task_write_unverified", "Canonical update readback differs; inspect the same original task.")
        except OSError as error:
            raise Fault("task_write_unverified", "Canonical durability was not acknowledged; retain the original update/operation ID and never dispatch on uncertainty.") from error
        if index is not None:
            index["tasks"][identity] = structured_metadata(value, raw)
            index["generation"] += 1
            try:
                _write_index(root, index)
            except (OSError, Fault):
                index = None
        return {**_view(root, value, raw, index_status="current" if index is not None else "stale"), "status": "saved"}


def _matching_operations(state: Path, predicate) -> list[dict]:
    """Scan canonical owners with complete bounded index coverage."""
    generation, offset, found = None, 0, []
    while True:
        with _reading(state) as root:
            if root is None:
                return []
            index = read_index(root)
            if generation is not None and generation != index["generation"]:
                raise Fault("task_index_changed", "Task identity coverage changed; read original evidence before new work.")
            generation = index["generation"]
            identities = sorted(index["tasks"])
            checked, consumed = 0, 0
            for identity in identities[offset:offset + INDEX_PAGE]:
                item = index["tasks"][identity]
                path = root / identity / "task.json"
                if not os.path.lexists(path):
                    if item.get("owner") == "structured":
                        raise Fault("corrupt_tasks", "The original structured owner is missing; preserve its binding.")
                    checked += 1
                    continue
                size = private_path(path).st_size
                if size > records.MAX_RECORD:
                    raise Fault("corrupt_tasks", "A retained canonical task exceeds its record bound; preserve it without dispatch.")
                if consumed + size > MAX_INDEX:
                    break
                record, raw = read_structured(root, identity)
                consumed += len(raw)
                checked += 1
                if item != structured_metadata(record, raw):
                    raise Fault("task_index_incomplete", "Canonical identity coverage is stale; rebuild before a new effect.")
                matches = [operation for operation in record["operations"] if predicate(record, operation)]
                if matches:
                    found.append({"task_id": identity, "operations": matches})
            offset += checked
            if offset == len(identities):
                return found


def operation_binding(state: Path, key: tuple, operation_id: str = "") -> dict | None:
    """Original operation identity is independent from resource conflicts."""
    found = _matching_operations(state, lambda record, operation:
        operation_id and operation["id"] == operation_id or operation_key(operation["locator"]) == key)
    if len(found) > 1:
        raise Fault("task_operation_conflict", "An original operation is ambiguously bound; preserve both tasks without dispatch.")
    return found[0] if found else None


def effect_conflicts(state: Path, proposed_record: dict, proposed: dict) -> list[dict]:
    """Native GitHub routes share durable conflicts across task and account IDs."""
    if proposed["locator"]["kind"] != "github":
        return []
    return _matching_operations(state, lambda record, operation:
        operation["id"] != proposed.get("id") and effects.unresolved(operation)
        and effects.conflicts(record, operation, proposed_record, proposed))


def operation_key(locator: dict) -> tuple:
    kind = locator["kind"]
    if kind == "github":
        if not locator["anchor"]:
            object_type = "pull_request" if locator["object_type"] == "merge" else locator["object_type"]
            return "github_destination", locator["repository"].casefold(), object_type, locator["number"]
        return kind, locator["repository"].casefold(), locator["anchor"]
    return kind, locator["target_identity"] or "local", locator["execution_id" if kind == "execution" else "change_id"]


def require_legacy(state: Path, identity: str) -> None:
    """Only used after original execution-ID reconciliation, before a new launch."""
    if "record" in read(state, identity):
        raise Fault("supervised_admission_unavailable", "A bare task_id is not supervised authority; supply the current task_context before a new structured-task effect.")


def _metadata_page(root: Path, index: dict, identities: list[str], limit: int = INDEX_PAGE) -> list[tuple[str, dict]]:
    page, consumed = [], 0
    for identity in identities[:limit]:
        structured = os.path.lexists(root / identity / "task.json")
        path = root / identity / ("task.json" if structured else "task.md")
        size = private_path(path).st_size
        if size > (records.MAX_RECORD if structured else MAX_CHECKPOINT):
            raise Fault("corrupt_tasks", "A retained task exceeds its record bound.")
        if consumed + size > MAX_INDEX:
            break
        if structured:
            record, raw = read_structured(root, identity)
            item = structured_metadata(record, raw)
        else:
            item, raw = read_checkpoint(root, index, identity)
            item = {"owner": "legacy", **item}
        consumed += len(raw)
        page.append((identity, item))
    return page


def find(state: Path, query: str = "", anchors: list[str] | None = None, identity: str = "",
         offset: int = 0, limit: int = 20, expected_generation: int = -1) -> dict:
    if identity:
        task_id(identity)
    wanted = validate_metadata("lookup", anchors or [])["anchors"]
    if (not isinstance(query, str) or len(query) > 200 or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 50 or type(expected_generation) is not int or expected_generation < -1):
        raise Fault("invalid_range", "Use a bounded lookup query, nonnegative offset and page size from 1 to 50.")
    with _reading(state) as root:
        if root is None:
            if expected_generation != -1:
                raise Fault("task_index_changed", "The original task index is no longer available.")
            return {"status": "uninitialized", "tasks": [], "next_offset": None, "generation": None,
                    "coverage": "this_store_only", "resume_authorized": False}
        index = load_index(root)
        complete = catalog_complete(root, index)
        if expected_generation != -1 and expected_generation != index["generation"]:
            raise Fault("task_index_changed", "Task lookup metadata changed between pages; read the current index.")
        matches = [key for key, value in sorted(index["tasks"].items())
                   if (not identity or key == identity) and set(wanted) <= set(value["anchors"])
                   and query.casefold() in (key + " " + value["title"]).casefold()]
        page = _metadata_page(root, index, matches[offset:], limit)
        for key, item in page:
            stored = {"owner": "legacy", **index["tasks"][key]}
            complete = complete and item == stored
        total = len(matches)
        end = offset + len(page)
        exact = bool(identity or wanted) and not query
        status = "matched" if exact and total == 1 else "ambiguous" if exact and total > 1 else "candidates"
        return {"status": status, "tasks": [{"task_id": key, "title": item["title"], "revision": item["revision"]}
                                            for key, item in page], "total": total,
                "next_offset": end if end < total else None, "generation": index["generation"],
                "coverage": "registered_tasks_only" if complete and index["format_version"] == 1 else "complete" if complete else "incomplete",
                "resume_authorized": False}


def observation_page(state: Path, known: dict[str, str]) -> dict:
    """Read changed canonical candidates, not a second active-task ledger.

    The caller retains only advertised metadata hashes. Startup inspects each
    registered structured record once; unchanged history is never reread on a
    timer. A stale index is reported, not repaired or taken as complete absence.
    """
    if len(known) > MAX_TASKS:
        raise Fault("task_capacity", "Observation discovery exceeds the bounded task catalog.")
    with _reading(state) as root:
        if root is None:
            return {"entries": [], "identities": [], "generation": None, "complete": True, "has_more": False}
        index = load_index(root)
        complete = catalog_complete(root, index)
        identities = sorted(key for key, item in index["tasks"].items() if item.get("owner") == "structured")
        changed = [key for key in identities if known.get(key) != index["tasks"][key]["sha256"]]
        entries, consumed = [], 0
        for identity in changed[:INDEX_PAGE]:
            item = index["tasks"][identity]
            record, error = None, None
            try:
                size = private_path(root / identity / "task.json").st_size
                if size > records.MAX_RECORD:
                    raise Fault("corrupt_tasks", "Retained canonical evidence exceeds its bound.")
                if consumed + size > MAX_INDEX:
                    break
                consumed += size
                record, raw = read_structured(root, identity)
                complete = complete and item == structured_metadata(record, raw)
            except (Fault, OSError) as failure:
                error = failure.code if isinstance(failure, Fault) else "source_unavailable"
                complete = False
            entries.append({"task_id": identity, "metadata_sha256": item["sha256"], "record": record, "error": error})
        return {"entries": entries, "identities": identities, "generation": index["generation"],
                "complete": complete, "has_more": len(entries) < len(changed)}


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace(path: Path, raw: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _index_budget(index: dict) -> None:
    if (len(index["tasks"]) > MAX_TASKS or index["generation"] > records.MAX_INTEGER
            or len((json.dumps(index, sort_keys=True) + "\n").encode()) > MAX_INDEX):
        raise Fault("task_index_full", "The bounded task index is full; preserve existing records.")


def _version_two(index: dict) -> dict:
    return {"format_version": 2, "generation": index["generation"],
            "coverage": index.get("coverage", "complete"), "next_cursor": index.get("next_cursor"),
            "tasks": {key: {"owner": "legacy", **item} for key, item in index["tasks"].items()}}


def _write_index(root: Path, index: dict) -> None:
    _index_budget(index)
    save_json(root / "index.json", index)
    if load_index(root) != index:
        raise Fault("task_index_write_unverified", "Derived index readback differs; preserve canonical tasks and inspect metadata.")


def _original_create(root: Path | None, request: dict) -> dict | None:
    if root is None or not os.path.lexists(root / request["task_id"]):
        return None
    identity = request["task_id"]
    if not os.path.lexists(root / identity / "task.json"):
        read_checkpoint(root, read_index(root), identity)
        raise Fault("task_conflict", "This ID belongs to legacy history; conversion requires separate approval.")
    record, raw = read_structured(root, identity)
    update = {"id": request["update_id"], "request_sha256": records.digest({"method": "create_task", **request})}
    # Registration alone may publish revision 2 without replacing the original
    # creation receipt. Later business/control revisions still require read_task.
    creation_revision = 2 if record.get("chat_registration") is not None else 1
    if record["last_update"] != update or record["revision"] != creation_revision:
        raise Fault("task_conflict", "The original task/update ID has different intent or a later revision; read it without replay.")
    return {**_view(root, record, raw), "status": "unchanged"}


def create(state: Path, identity: str, update_id: str, title: str, anchors: list[str], origin: dict,
           contract: dict, stages: list[dict], *, validate_new: Callable[[dict], None] | None = None) -> dict:
    request = records.normalize_create({"task_id": identity, "update_id": update_id, "title": title,
        "anchors": anchors, "origin": origin, "contract": contract, "stages": stages})
    with _reading(state) as root:
        original = _original_create(root, request)
        if original is not None:
            return original
    # Preflight size/expiry before initialization; no task lock spans target reads.
    records.new_record(request, time.time_ns() // 1000000)
    prepared = deepcopy(request)
    if validate_new is not None:
        validate_new(prepared)
    record = records.new_record(prepared, time.time_ns() // 1000000)
    # Resolved authority is saved, but original-ID retries retain caller intent.
    record["last_update"]["request_sha256"] = records.digest({"method": "create_task", **request})
    raw = records.canonical(record)
    with _writing(state, initialize=True) as root:
        original = _original_create(root, request)
        if original is not None:
            return original
        index = _version_two(read_index(root))
        index["tasks"][identity] = structured_metadata(record, raw)
        index["generation"] += 1
        _index_budget(index)
        temporary = root / (".record-task-" + uuid.uuid4().hex)
        try:
            temporary.mkdir(mode=0o700)
            _replace(temporary / "task.json", raw)
            if os.path.lexists(root / identity):
                raise Fault("task_conflict", "The destination appeared during creation; preserve both records.")
            os.rename(temporary, root / identity)
            _sync_directory(root)
            observed, content = read_structured(root, identity)
            if observed != record or content != raw:
                raise Fault("task_write_unverified", "Canonical publication readback differs; inspect the original task.")
        except OSError as error:
            raise Fault("task_write_unverified", "Canonical publication has no verified durable acknowledgement; read the original ID, never replace it.") from error
        # Index failure cannot undo a verified canonical commit or cause another creation.
        try:
            _write_index(root, index)
        except (OSError, Fault):
            return {**_view(root, record, raw, index_status="stale"), "status": "created"}
        return {**_view(root, record, raw, index_status="current"), "status": "created"}


def rebuild_index(state: Path, expected_generation: int = -1) -> dict:
    """Explicit maintenance: at most 50 records/2 MiB, no canonical writes or GC."""
    if type(expected_generation) is not int or not -1 <= expected_generation <= records.MAX_INTEGER:
        raise Fault("invalid_range", "Supply the observed index generation, or -1 to begin a rebuild.")
    with _writing(state, initialize=False) as root:
        try:
            original = load_index(root)
        except Fault as error:
            if error.code != "corrupt_tasks" or expected_generation != -1:
                raise
            # A missing/corrupt derived file is recoverable only without legacy gaps.
            if os.path.lexists(root / "index.json"):
                private_path(root / "index.json")
            original = None
        index = _version_two(original) if original is not None else {
            "format_version": 2, "generation": time.time_ns() // 1000,
            "coverage": "complete", "next_cursor": None, "tasks": {}}
        if expected_generation != -1 and expected_generation != index["generation"]:
            raise Fault("task_index_changed", "Index generation changed; inspect the current rebuild cursor.")
        if index["coverage"] == "incomplete" and expected_generation == -1:
            raise Fault("task_index_changed", "An existing rebuild needs its observed generation; do not restart its episode.")
        names = directory_names(root)
        if original is None and not names:
            raise Fault("corrupt_tasks", "Missing metadata and no canonical records cannot prove empty history; do not initialize over it.")
        catalog_complete(root, index, names)
        cursor = index["next_cursor"] if index["coverage"] == "incomplete" else None
        if cursor is not None and (cursor not in names or any(key <= cursor and key not in index["tasks"] for key in names)):
            raise Fault("corrupt_tasks", "The published rebuild prefix changed; preserve it for reconciliation.")
        remaining = [key for key in names if cursor is None or key > cursor]
        page = _metadata_page(root, index, remaining)
        for key, item in page:
            index["tasks"][key] = item
        done = len(page) == len(remaining)
        index.update(generation=index["generation"] + 1, coverage="complete" if done else "incomplete",
                     next_cursor=None if done else page[-1][0])
        _index_budget(index)
        try:
            _write_index(root, index)
        except OSError as error:
            raise Fault("task_index_write_unverified", "Index publication was not acknowledged; read its original generation before continuing.") from error
        return {"status": "current" if done else "incomplete", "generation": index["generation"],
                "coverage": index["coverage"], "next_cursor": index["next_cursor"], "records_checked": len(page)}


def check_state(state: Path) -> dict:
    """Actual reader proof, including terminal-only stores; never repairs an index."""
    count, structured, generation, offset, version, present = 0, 0, None, 0, None, False
    formats_present = set()
    while True:
        with _reading(state) as root:
            if root is None:
                if present:
                    raise Fault("corrupt_tasks", "The original task store disappeared during compatibility validation.")
                break
            present = True
            index = read_index(root)
            if generation is not None and index["generation"] != generation:
                raise Fault("task_index_changed", "Task history changed during candidate validation.")
            generation, version = index["generation"], index["format_version"]
            identities = sorted(index["tasks"])
            page = _metadata_page(root, index, identities[offset:])
            for key, item in page:
                if item != {"owner": "legacy", **index["tasks"][key]}:
                    raise Fault("task_index_incomplete", "Canonical records and lookup metadata differ; rebuild before a service transition.")
                structured += item["owner"] == "structured"
                if item["owner"] == "structured":
                    record, _ = read_structured(root, key)
                    formats_present.add(record["format"])
            count += len(page)
            offset += len(page)
            if offset == len(identities):
                break
    return {"task_store_present": present, "task_records": count, "structured_task_records": structured,
            "structured_task_read_formats": list(records.READ_FORMATS),
            "structured_task_formats_present": sorted(formats_present), "task_index_format": version,
            "task_index_generation": generation, "task_index_coverage": "complete"}


def save(state: Path, identity: str, expected_revision: int, title: str, body: str, anchors: list[str]) -> dict:
    task_id(identity)
    metadata = validate_metadata(title, anchors)
    if type(expected_revision) is not int or expected_revision < 0:
        raise Fault("invalid_revision", "Supply the observed nonnegative checkpoint revision; zero means new.")
    raw = body.encode("utf-8")
    if not raw.strip() or len(raw) > MAX_CHECKPOINT or b"\0" in raw or redact(body) != body:
        raise Fault("invalid_checkpoint", "Supply a nonempty checkpoint up to 32 KiB, without credentials or NUL bytes.")
    if expected_revision and not (state / "tasks").exists():
        raise Fault("corrupt_tasks", "The original task store is missing; do not initialize new history.")
    with _writing(state, initialize=True) as root:
        index = read_index(root)
        revision, previous = 0, None
        if identity in index["tasks"]:
            item, previous = read_checkpoint(root, index, identity)
            metadata = validate_metadata(metadata["title"], sorted(set(item["anchors"]) | set(metadata["anchors"])))
            revision = item["revision"]
            if raw == previous and metadata == {key: item[key] for key in ("title", "anchors")}:
                if expected_revision not in (revision, revision - 1):
                    raise Fault("task_conflict", "This retry does not match the last checkpoint revision.")
                return {"task_id": identity, **item, "generation": index["generation"], "status": "unchanged"}
        if expected_revision != revision:
            raise Fault("task_conflict", "A newer checkpoint exists; read it before updating progress.")
        item = {**metadata, "revision": revision + 1, "sha256": hashlib.sha256(raw).hexdigest()}
        index["tasks"][identity] = ({"owner": "legacy", **item} if index["format_version"] == 2 else item)
        index["generation"] += 1
        _index_budget(index)
        directory = private_directory(root / identity)
        try:
            _replace(directory / "task.md", raw)
            save_json(root / "index.json", index)
            actual = read_index(root)
            observed, content = read_checkpoint(root, actual, identity)
            if observed != item or content != raw:
                raise Fault("task_write_unverified", "Checkpoint readback did not match; inspect the original task.")
        except OSError as error:
            raise Fault("task_write_unverified", "The save has no verified durable receipt; read the original task before retrying.") from error
        return {"task_id": identity, **item, "generation": index["generation"], "status": "saved"}
