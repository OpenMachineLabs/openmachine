"""Versioned change receipts, validation and private ownership; no user-file publication."""

from contextlib import contextmanager, ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from openmachine import __version__
from openmachine.foundation.files import file_parts
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from .files import MAX_SOURCE
from . import patch

CHANGE_FORMAT = 2
READ_CHANGE_FORMATS = (1, 2)
MAX_PREPARED = 8 * 1024 * 1024
MAX_PARENTS = 16
MAX_PARENT_DEPTH = 8
MAX_RECORD = 65536
MAX_CHANGES = 10000
MAX_EDITS = 64
MAX_WORKSPACE_RESULT = 512 * 1024
TERMINAL = {"applied", "unchanged", "rejected", "partial", "outcome_unknown"}


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _hex(value, length: int, *, empty: bool = False) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{%d}" % length, value) or empty and value == "")


def _effect_specs(plan):
    if "operations" not in plan:
        return [("publish", plan["path"])]
    specs, created = [], set()
    for operation in plan["operations"]:
        kind, path = operation["kind"], operation["path"]
        destination = operation.get("destination", path)
        if kind in {"add", "move"}:
            for parent in plan["parents"]:
                name = parent["path"]
                if name not in created and destination.startswith(name + "/"):
                    specs.append(("mkdir", name))
                    created.add(name)
        if kind == "move":
            specs.extend((("publish", destination), ("remove", path)))
        else:
            specs.append(("remove" if kind == "delete" else "publish", path))
    return specs


def _effects(plan, change_id):
    effects = []
    patching = "operations" in plan
    for index, (kind, path) in enumerate(_effect_specs(plan)):
        temporary = ".openmachine-change-" + change_id + (f"-{index:02x}" if patching else "") if kind == "publish" else None
        effect = {"path": path, "temporary": temporary, "stage": "not_attempted", "result_sha256": None,
                  "synced": False, "readback_verified": False, "error": None, "cleanup": "not_created"}
        if patching:
            effect.update(kind=kind, result_identity=None, result_mode=None)
        effects.append(effect)
    return effects


def _record_budget(record):
    """Reserve bounded room for every possible effect/error before any target IO."""
    largest = json.loads(json.dumps(record))
    largest.update(revision=2**63 - 1, status="outcome_unknown", error="x" * 128, finished_at=1.2345678901234567e+100)
    for effect in largest["effects"]:
        effect.update(stage="failed_no_effect", result_sha256="f" * 64, error="x" * 128, cleanup="not_created")
        if "kind" in effect:
            effect.update(result_identity=[2**64 - 1, 2**64 - 1], result_mode=511)
    # A 64 KiB complete record also leaves ample room for both escaped MCP
    # representations, identity metadata and a separately bounded 32 KiB diff.
    if len(json.dumps(largest, sort_keys=True).encode()) + 1024 > MAX_RECORD:
        raise Fault("request_limit", "The complete plan and worst-case effect evidence exceed the record budget.")


def _validate_patch(record):
    expected = record["expected"]
    if (set(expected) != {"workspace_identity", "head_sha", "plan_sha256", "preconditions", "create_parents"}
        or not _hex(expected["workspace_identity"], 64) or not _hex(expected["head_sha"], 40, empty=True)
        or not _hex(expected["plan_sha256"], 64, empty=True) or type(expected["create_parents"]) is not bool
        or not isinstance(expected["preconditions"], list) or not 1 <= len(expected["preconditions"]) <= patch.MAX_PATHS):
        raise ValueError
    preconditions = expected["preconditions"]
    if any(set(item) != {"path", "expected_sha256"} or not _hex(item["expected_sha256"], 64, empty=True) for item in preconditions):
        raise ValueError
    names = [item["path"] for item in preconditions]
    patch.checked_paths(names)
    if names != sorted(names):
        raise ValueError
    if record["error"] is not None and (not isinstance(record["error"], str) or len(record["error"]) > 128):
        raise ValueError
    plan, effects, status = record["plan"], record["effects"], record["status"]
    if plan is None:
        if record["plan_sha256"] is not None or effects or status not in {"accepted", "rejected", "outcome_unknown"}:
            raise ValueError
        return
    if (set(plan) != {"context", "operations", "files", "parents"} or plan["context"] != record["context"]
        or _hash(plan) != record["plan_sha256"] or plan["context"]["workspace_identity"] != expected["workspace_identity"]
        or (plan["context"]["head_sha"] or "") != expected["head_sha"]
        or not isinstance(plan["files"], list) or len(plan["files"]) != len(names)
        or not isinstance(plan["parents"], list) or len(plan["parents"]) > MAX_PARENTS
        or not isinstance(plan["operations"], list) or not 1 <= len(plan["operations"]) <= patch.MAX_PATHS):
        raise ValueError
    def ancestry(item):
        parts = file_parts(item["path"])
        ancestors, missing = item["ancestors"], item["missing_parents"]
        if (len(item["path"].encode()) > 1024 or not isinstance(ancestors, list) or not ancestors
            or ancestors[0] != plan["context"]["root_id"]
            or any(not isinstance(pair, list) or len(pair) != 2 or any(type(v) is not int or v < 0 for v in pair) for pair in ancestors)
            or not isinstance(missing, list) or len(missing) > MAX_PARENT_DEPTH
            or len(ancestors) + len(missing) != len(parts)
            or missing != ["/".join(parts[:depth]) for depth in range(len(ancestors), len(parts))]):
            raise ValueError
    files = {}
    for item in plan["files"]:
        if set(item) != {"path", "ancestors", "missing_parents", "before_sha256", "after_sha256", "before_size", "after_size", "before_mode", "after_mode"}:
            raise ValueError
        ancestry(item)
        if item["path"] in files:
            raise ValueError
        files[item["path"]] = item
        for side in ("before", "after"):
            sha, mode, size = item[side + "_sha256"], item[side + "_mode"], item[side + "_size"]
            if (not _hex(sha, 64, empty=True) or (mode is None) != (sha == "")
                or mode is not None and (type(mode) is not int or not 0 <= mode <= 0o777)
                or type(size) is not int or not 0 <= size <= MAX_SOURCE or sha == "" and size != 0):
                raise ValueError
    if (set(files) != set(names) or any(files[item["path"]]["before_sha256"] != item["expected_sha256"] for item in preconditions)
        or sum(item["before_size"] + item["after_size"] for item in files.values()) > MAX_PREPARED):
        raise ValueError
    touched, needed = [], set()
    for operation in plan["operations"]:
        kind = operation["kind"]
        if (kind not in {"add", "update", "delete", "move"} or
            set(operation) != ({"kind", "path", "ranges", "destination"} if kind == "move" else {"kind", "path", "ranges"})):
            raise ValueError
        path, destination = operation["path"], operation.get("destination", operation["path"])
        before, after = files[path], files[destination]
        touched.append(path)
        if kind == "move":
            touched.append(destination)
        if (kind == "add" and (before["before_sha256"] != "" or after["after_mode"] != 0o644)
            or kind != "add" and not before["before_sha256"]
            or kind in {"delete", "move"} and before["after_sha256"] != ""
            or kind in {"add", "update", "move"} and not after["after_sha256"]
            or kind in {"update", "move"} and before["before_mode"] != after["after_mode"]
            or kind == "move" and after["before_sha256"] != ""):
            raise ValueError
        if kind in {"add", "move"}:
            needed.update(after["missing_parents"])
        elif before["missing_parents"]:
            raise ValueError
        ranges, old_end, new_end = operation["ranges"], 0, 0
        if not isinstance(ranges, list) or len(ranges) > patch.MAX_HUNKS + 1 or not ranges and kind != "move":
            raise ValueError
        for span in ranges:
            if (set(span) != {"old_start", "old_end", "new_start", "new_end"}
                or any(type(value) is not int for value in span.values())
                or not old_end <= span["old_start"] <= span["old_end"] <= before["before_size"]
                or not new_end <= span["new_start"] <= span["new_end"] <= after["after_size"]):
                raise ValueError
            old_end, new_end = span["old_end"], span["new_end"]
    if touched != list(files) or len(set(touched)) != len(touched):
        raise ValueError
    parents = set()
    for item in plan["parents"]:
        if set(item) != {"path", "ancestors", "missing_parents"} or item["path"] in parents:
            raise ValueError
        ancestry(item)
        if any(path not in parents for path in item["missing_parents"]):
            raise ValueError
        parents.add(item["path"])
    if parents != needed or parents and not expected["create_parents"]:
        raise ValueError
    templates = _effects(plan, record["change_id"])
    if not isinstance(effects, list) or len(effects) != len(templates):
        raise ValueError
    stopped = False
    for effect, template in zip(effects, templates):
        if set(effect) != set(template) or any(effect[key] != template[key] for key in ("path", "temporary", "kind")):
            raise ValueError
        stage, kind = effect["stage"], effect["kind"]
        if (stage not in {"not_attempted", "attempting", "applied", "unchanged", "failed_no_effect", "unknown"}
            or stopped and stage != "not_attempted"
            or type(effect["synced"]) is not bool or type(effect["readback_verified"]) is not bool
            or effect["cleanup"] not in {"not_created", "pending", "removed", "unknown"}
            or effect["error"] is not None and (not isinstance(effect["error"], str) or len(effect["error"]) > 128)
            or effect["result_sha256"] is not None and not _hex(effect["result_sha256"], 64, empty=True)):
            raise ValueError
        stopped |= stage not in {"applied", "unchanged"}
        identity, mode = effect["result_identity"], effect["result_mode"]
        if (identity is not None and (kind != "mkdir" or not isinstance(identity, list) or len(identity) != 2
                                     or any(type(value) is not int or value < 0 for value in identity))
            or mode is not None and (kind != "mkdir" or type(mode) is not int or not 0 <= mode <= 0o777)):
            raise ValueError
        if stage in {"not_attempted", "attempting"} and effect != template | {"stage": stage}:
            raise ValueError
        if stage in {"applied", "unchanged"}:
            if not effect["readback_verified"] or effect["error"] is not None:
                raise ValueError
            if stage == "applied" and (not effect["synced"] or effect["cleanup"] != ("removed" if kind == "publish" else "not_created")):
                raise ValueError
            if kind == "mkdir":
                if stage != "applied" or identity is None or mode is None or effect["result_sha256"] is not None:
                    raise ValueError
            elif effect["result_sha256"] != files[effect["path"]]["after_sha256"]:
                raise ValueError
            if stage == "unchanged" and (kind != "publish" or effect["cleanup"] != "not_created"
                or files[effect["path"]]["before_sha256"] != files[effect["path"]]["after_sha256"]):
                raise ValueError
    stages = [effect["stage"] for effect in effects]
    uncertain = any(effect["stage"] in {"unknown", "attempting"} or effect["cleanup"] == "unknown" for effect in effects)
    if (status == "accepted" or status == "prepared" and any(stage != "not_attempted" for stage in stages)
        or status == "applying" and any(stage in {"unknown", "failed_no_effect"} for stage in stages)
        or status == "applied" and (any(stage not in {"applied", "unchanged"} for stage in stages) or "applied" not in stages)
        or status == "unchanged" and any(stage != "unchanged" for stage in stages)
        or status == "rejected" and ("applied" in stages or uncertain)
        or status == "partial" and ("applied" not in stages or uncertain or all(stage in {"applied", "unchanged"} for stage in stages))
        or status in {"applied", "unchanged"} and record["error"] is not None):
        raise ValueError


def _private(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or
        info.st_uid != os.getuid() or info.st_mode & 0o077 or not directory and info.st_nlink != 1):
        raise Fault("change_store_corrupt", "Preserve unsafe or linked change state; it cannot be used.")
    return info


def _owner(state: Path) -> str:
    info = _private(state, directory=True)
    if state.resolve() != state:
        raise Fault("change_store_corrupt", "Use the original canonical target state directory.")
    return _hash({"state": str(state), "device": info.st_dev, "inode": info.st_ino, "uid": os.getuid()})


def _entries(store: Path) -> list[Path]:
    entries = []
    for item in store.iterdir():
        if not _hex(item.name, 32):
            raise Fault("change_store_corrupt", "An invalid change identity is retained in the store.")
        _private(item, directory=True)
        entries.append(item)
        if len(entries) > MAX_CHANGES:
            raise Fault("change_store_full", "Retain existing history; this store exceeds its identity budget.")
    return entries


def _store(state: Path) -> Path | None:
    store, lock = state / "workspace-changes", state / "locks/workspace-changes.lock"
    if not os.path.lexists(store):
        if os.path.lexists(lock):
            raise Fault("change_store_corrupt", "The established change store is missing; do not initialize replacement history.")
        return None
    _owner(state)
    _private(store, directory=True)
    _private(state / "locks", directory=True)
    _private(lock)
    return store


def _validate(record: dict, change_id: str) -> None:
    try:
        if type(record.get("format")) is not int or record["format"] not in READ_CHANGE_FORMATS:
            raise Fault("unsupported_change_format", "The retained change format requires a compatible reader.")
        if set(record) != {"format", "change_id", "writer_version", "owner_sha256", "request_sha256", "operation",
                           "created_at", "revision", "context", "expected", "plan", "plan_sha256", "effects",
                           "status", "finished_at", "error"}:
            raise ValueError
        if (record["change_id"] != change_id or not _hex(change_id, 32) or
            not _hex(record["owner_sha256"], 64) or not _hex(record["request_sha256"], 64) or
            record["operation"] not in {"edit", "write", "patch"} or
            (record["operation"] == "patch") != (record["format"] == 2) or
            not isinstance(record["writer_version"], str) or not 1 <= len(record["writer_version"]) <= 100 or
            type(record["revision"]) is not int or record["revision"] < 1 or
            record["status"] not in TERMINAL | {"accepted", "prepared", "applying"}):
            raise ValueError
        for value in (record["created_at"], record["finished_at"]):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError
        if record["created_at"] is None or (record["finished_at"] is not None) != (record["status"] in TERMINAL):
            raise ValueError
        ctx = record["context"]
        if (set(ctx) != {"root", "root_id", "worktree", "workspace_identity", "workspace_identity_version", "git_state", "head_sha"}
            or not Path(ctx["root"]).is_absolute() or not Path(ctx["worktree"]).is_absolute()
            or not isinstance(ctx["root_id"], list) or len(ctx["root_id"]) != 2
            or any(type(v) is not int or v < 0 for v in ctx["root_id"])
            or type(ctx["workspace_identity_version"]) is not int or ctx["workspace_identity_version"] != 1
            or not _hex(ctx["workspace_identity"], 64)
            or ctx["git_state"] not in {"worktree", "detached", "unborn", "non_git"}
            or ctx["head_sha"] is not None and not _hex(ctx["head_sha"], 40)):
            raise ValueError
        if record["format"] == 2:
            try:
                _validate_patch(record)
            except Fault as error:
                raise ValueError from error
            return
        if record["status"] == "partial":
            raise ValueError
        expected = record["expected"]
        if (set(expected) != {"path", "workspace_identity", "head_sha", "sha256", "plan_sha256"}
            or not _hex(expected["workspace_identity"], 64) or not _hex(expected["head_sha"], 40, empty=True)
            or not _hex(expected["sha256"], 64, empty=record["operation"] == "write")
            or not _hex(expected["plan_sha256"], 64, empty=True)):
            raise ValueError
        if not isinstance(expected["path"], str) or len(expected["path"].encode()) > 1024:
            raise ValueError
        file_parts(expected["path"])
        if record["error"] is not None and (not isinstance(record["error"], str) or len(record["error"]) > 128):
            raise ValueError
        plan, effects = record["plan"], record["effects"]
        if plan is None:
            if record["plan_sha256"] is not None or effects or record["status"] not in {"accepted", "rejected", "outcome_unknown"}:
                raise ValueError
            return
        if (set(plan) != {"context", "path", "before_sha256", "after_sha256", "before_mode", "after_mode", "ancestors", "ranges"}
            or _hash(plan) != record["plan_sha256"] or plan["context"] != ctx
            or plan["path"] != expected["path"] or plan["before_sha256"] != expected["sha256"]
            or ctx["workspace_identity"] != expected["workspace_identity"]
            or (ctx["head_sha"] or "") != expected["head_sha"]
            or not _hex(plan["after_sha256"], 64) or not isinstance(effects, list) or len(effects) != 1
            or type(plan["after_mode"]) is not int or not 0 <= plan["after_mode"] <= 0o777
            or plan["before_mode"] is not None and (type(plan["before_mode"]) is not int or not 0 <= plan["before_mode"] <= 0o777)
            or not isinstance(plan["ranges"], list) or not 1 <= len(plan["ranges"]) <= MAX_EDITS):
            raise ValueError
        if (record["status"] == "accepted" or
            (plan["before_mode"] is None) != (plan["before_sha256"] == "") or
            plan["after_mode"] != (plan["before_mode"] if plan["before_mode"] is not None else 0o644) or
            not isinstance(plan["ancestors"], list) or not plan["ancestors"] or
            plan["ancestors"][0] != ctx["root_id"] or
            any(not isinstance(item, list) or len(item) != 2 or any(type(v) is not int or v < 0 for v in item)
                for item in plan["ancestors"])):
            raise ValueError
        prior_old = prior_new = 0
        for span in plan["ranges"]:
            if set(span) != {"old_start", "old_end", "new_start", "new_end"} or any(type(v) is not int or not 0 <= v <= MAX_SOURCE for v in span.values()):
                raise ValueError
            if (span["old_start"] > span["old_end"] or span["new_start"] > span["new_end"] or
                span["old_start"] < prior_old or span["new_start"] < prior_new):
                raise ValueError
            prior_old, prior_new = span["old_end"], span["new_end"]
        effect = effects[0]
        if (set(effect) != {"path", "temporary", "stage", "result_sha256", "synced", "readback_verified", "error", "cleanup"}
            or effect["path"] != plan["path"] or effect["temporary"] != ".openmachine-change-" + change_id
            or effect["stage"] not in {"not_attempted", "attempting", "applied", "unchanged", "failed_no_effect", "unknown"}
            or type(effect["synced"]) is not bool or type(effect["readback_verified"]) is not bool
            or effect["cleanup"] not in {"not_created", "pending", "removed", "unknown"}
            or effect["result_sha256"] is not None and not _hex(effect["result_sha256"], 64, empty=True)
            or effect["error"] is not None and (not isinstance(effect["error"], str) or len(effect["error"]) > 128)):
            raise ValueError
        if record["status"] == "applied" and (effect["stage"] != "applied" or not effect["synced"] or
            not effect["readback_verified"] or effect["result_sha256"] != plan["after_sha256"] or effect["cleanup"] != "removed"):
            raise ValueError
        if record["status"] == "unchanged" and (effect["stage"] != "unchanged" or plan["before_sha256"] != plan["after_sha256"]):
            raise ValueError
        if record["status"] == "rejected" and effect["stage"] not in {"not_attempted", "failed_no_effect"}:
            raise ValueError
        if record["status"] == "prepared" and effect["stage"] != "not_attempted":
            raise ValueError
        if record["status"] == "applying" and effect["stage"] != "attempting":
            raise ValueError
        if record["status"] in {"applied", "unchanged"} and (record["error"] is not None or effect["error"] is not None):
            raise ValueError
        if effect["stage"] in {"not_attempted", "attempting"} and (effect["synced"] or effect["readback_verified"] or effect["result_sha256"] is not None):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise Fault("change_store_corrupt", "The retained change record is invalid; never repair or replay it implicitly.") from error


def _load(directory: Path) -> dict:
    _private(directory, directory=True)
    _private(directory / "writer.lock")
    path = directory / "record.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        actual = os.fstat(source.fileno())
        # An atomic record save may unlink this already-open prior snapshot.
        if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid() or actual.st_mode & 0o077
            or actual.st_nlink > 1 or actual.st_size > MAX_RECORD):
            raise Fault("change_store_corrupt", "The opened change record is unsafe or oversized.")
        data = source.read(MAX_RECORD + 1)
    try:
        record = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise Fault("change_store_corrupt", "The retained change is not valid bounded JSON.") from error
    if not isinstance(record, dict) or len(data) > MAX_RECORD:
        raise Fault("change_store_corrupt", "The retained change is not a bounded object.")
    _validate(record, directory.name)
    return record


def _save(directory: Path, record: dict) -> None:
    candidate = record | {"revision": record["revision"] + 1}
    _validate(candidate, directory.name)
    if len(json.dumps(candidate, sort_keys=True).encode()) + 1 > MAX_RECORD:
        raise Fault("request_limit", "The complete change record exceeds 64 KiB before effects.")
    try:
        if record["revision"] and _load(directory)["revision"] != record["revision"]:
            raise Fault("change_store_corrupt", "The original writer revision changed.")
        save_json(directory / "record.json", candidate)
        if _load(directory) != candidate:
            raise OSError("Record readback mismatch")
    except (OSError, Fault) as error:
        raise Fault("change_record_unverified", "The original record save is unverified; do not repeat this change.") from error
    record["revision"] = candidate["revision"]


@contextmanager
def _own(state: Path, change_id: str):
    private_directory(state)
    locks = private_directory(state / "locks")
    store, allocation = state / "workspace-changes", locks / "workspace-changes.lock"
    initial = not os.path.lexists(store) and not os.path.lexists(allocation)
    if not initial:
        _private(allocation)
    with ExitStack() as owner:
        with lease(allocation, create=initial):
            _private(allocation)
            if initial and not os.path.lexists(store):
                private_directory(store)
                fd = os.open(state, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            _private(store, directory=True)
            directory = store / change_id
            existing = os.path.lexists(directory)
            if not existing:
                if len(_entries(store)) >= MAX_CHANGES:
                    raise Fault("change_store_full", "The target retains 10000 identities; existing IDs remain readable.")
                directory.mkdir(mode=0o700)
            _private(directory, directory=True)
            if not existing:
                owner.enter_context(lease(directory / "writer.lock"))
                _private(directory / "writer.lock")
                fd = os.open(store, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        yield directory, existing


def _observed(directory: Path, record: dict | None = None, *, owned: bool = False) -> dict:
    """Observe ownership before loading; a stale snapshot must never prove no effects."""
    with ExitStack() as ownership:
        alive = owned
        if not owned:
            _private(directory, directory=True)
            _private(directory / "writer.lock")
            try:
                ownership.enter_context(lease(directory / "writer.lock", shared=True, create=False))
            except Fault as error:
                if error.code != "resource_busy":
                    raise
                alive = True
        observed_at = time.time()
        if not owned:
            try:
                record = _load(directory)
            except FileNotFoundError:
                if alive:
                    raise Fault("resource_busy", "The original writer has not published its first receipt yet.") from None
                raise
        classification = "terminal" if record["status"] in TERMINAL else "owner_active" if alive else (
            "no_effects" if record["status"] in {"accepted", "prepared"} else "outcome_unknown")
        result = record | {"writer_alive": alive, "owner_observed_at": observed_at, "reconciliation": classification,
                           "historical_diff_available": False,
                           "next_observation": {"operation": "read_workspace_change", "change_id": record["change_id"]}}
        # Derived guidance only; never persist it or infer safety from an error code alone.
        if record["status"] == "rejected" and not record["effects"]:
            hint = {
                "match_ambiguous": "Use unique surrounding text to select the intended occurrence.",
                "match_missing": "Read the current source and correct the absent old fragment.",
                "context_mismatch": "Re-read workspace, branch and HEAD; reconcile the changed context.",
                "content_mismatch": "Read the current source and reconcile its changed content.",
            }.get(record["error"])
            if hint:
                preparation = "review the complete replacement" if record["operation"] == "write" else "preview the corrected intent"
                result["recovery_hint"] = ("No file effects were recorded. " + hint +
                    f" Continue only within current authorization; {preparation}, then use a new change ID."
                    " Preserve this original receipt; do not replay its request.")
        return result


def check_change_state(state: Path, *, require_idle: bool = False) -> dict:
    """Public, read-only format validation for candidate/target compatibility checks."""
    store = _store(state)
    count, formats = 0, set()
    if store is not None:
        owner = _owner(state)
        for directory in _entries(store):
            try:
                record = _load(directory)
            except FileNotFoundError as error:
                raise Fault("change_history_unavailable", "A retained allocation has incomplete history.") from error
            if record["owner_sha256"] != owner:
                raise Fault("change_store_corrupt", "A retained change belongs to a different state owner.")
            if require_idle and record["status"] not in {"applied", "unchanged", "rejected"}:
                raise Fault("activation_deferred", "An original workspace change is active or unresolved; no write was replayed.")
            count += 1
            formats.add(record["format"])
    return {"workspace_change_format": CHANGE_FORMAT, "workspace_change_read_formats": list(READ_CHANGE_FORMATS),
            "workspace_change_formats_present": sorted(formats), "workspace_change_records": count,
            "workspace_change_store_present": store is not None}


def _initial_record(request, change_id, observed_context, owner, fingerprint):
    expected = {"workspace_identity": request["expected_workspace_identity"], "head_sha": request["expected_head_sha"],
                "plan_sha256": request["expected_plan_sha256"]}
    patching = request["operation"] == "patch"
    expected.update({"preconditions": request["preconditions"], "create_parents": request["create_parents"]} if patching else
                    {"path": request["path"], "sha256": request["expected_sha256"]})
    return {"format": CHANGE_FORMAT if patching else 1, "change_id": change_id, "writer_version": __version__,
            "owner_sha256": owner, "request_sha256": fingerprint, "operation": request["operation"],
            "created_at": time.time(), "revision": 0, "context": observed_context, "expected": expected,
            "plan": None, "plan_sha256": None, "effects": [], "status": "accepted", "finished_at": None, "error": None}
