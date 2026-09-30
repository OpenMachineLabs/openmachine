"""Exact-edit preparation and target-owned, non-replaying change receipts."""

from contextlib import contextmanager, ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from openmachine import __version__
from openmachine.foundation.files import MAX_TEXT_FILE, TEXT_PAGE, file_parts
from openmachine.foundation.sanitize import Fault, redact
from openmachine.foundation.state import lease, private_directory, save_json

from .files import digest, publish_file, root_for, snapshot
from .git import context, lock_root

CHANGE_FORMAT = 1
MAX_RECORD = 65536
MAX_CHANGES = 10000
MAX_EDITS = 64
MAX_WORKSPACE_RESULT = 512 * 1024
TERMINAL = {"applied", "unchanged", "rejected", "outcome_unknown"}


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _text(value: str) -> bytes:
    try:
        if not isinstance(value, str) or "\0" in value:
            raise ValueError
        raw = value.encode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise Fault("invalid_content", "Supply valid UTF-8 scalar text without NUL.") from error
    if len(raw) > MAX_TEXT_FILE:
        raise Fault("invalid_content", "Text payloads and W1 sources are limited to 128 KiB.")
    return raw


def _hex(value, length: int, *, empty: bool = False) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{%d}" % length, value) or empty and value == "")


def _request(operation, path, payload, identity, head, sha, plan_sha) -> dict:
    path_bytes = _text(path)
    file_parts(path)
    if len(path_bytes) > 1024:
        raise Fault("request_limit", "Use a path of at most 1024 UTF-8 bytes.")
    if not (_hex(identity, 64) and _hex(head, 40, empty=True) and
            _hex(sha, 64, empty=operation == "write") and _hex(plan_sha, 64, empty=True)):
        raise Fault("invalid_precondition", "Supply the observed workspace identity, HEAD and complete source hash.")
    if operation == "write":
        _text(payload)
    else:
        if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_EDITS:
            raise Fault("invalid_edits", "Supply 1 through 64 exact replacements.")
        size = 0
        for edit in payload:
            if not isinstance(edit, dict) or set(edit) != {"old_text", "new_text"}:
                raise Fault("invalid_edits", "Each replacement needs exactly old_text and new_text.")
            old, new = _text(edit["old_text"]), _text(edit["new_text"])
            if not old or old == new:
                raise Fault("invalid_edits", "Old text must be nonempty and differ from new text.")
            size += len(old) + len(new)
            if size > MAX_TEXT_FILE:
                raise Fault("request_limit", "Combined replacement strings exceed 128 KiB.")
    request = {"operation": operation, "path": path, "payload": payload,
               "expected_workspace_identity": identity, "expected_head_sha": head,
               "expected_sha256": sha, "expected_plan_sha256": plan_sha}
    if len(json.dumps(request, ensure_ascii=True).encode()) > 1024 * 1024:
        raise Fault("request_limit", "The encoded request exceeds one MiB.")
    return request


def _replace(raw: bytes, edits: list[dict]) -> tuple[bytes, list[dict]]:
    spans = []
    for edit in edits:
        old, new = edit["old_text"].encode(), edit["new_text"].encode()
        start = raw.find(old)
        if start < 0:
            raise Fault("match_missing", "An exact old fragment is absent from the original source.")
        if raw.find(old, start + 1) >= 0:
            raise Fault("match_ambiguous", "An old fragment has multiple occurrences, including overlapping starts.")
        spans.append((start, start + len(old), new))
    spans.sort(key=lambda span: span[0])
    end = shift = 0
    chunks, ranges = [], []
    resulting_size = len(raw)
    for start, stop, new in spans:
        if start < end:
            raise Fault("match_overlap", "Replacement ranges overlap in the original source.")
        resulting_size += len(new) - (stop - start)
        ranges.append({"old_start": start, "old_end": stop,
                       "new_start": start + shift, "new_end": start + shift + len(new)})
        chunks.extend((raw[end:start], new))
        shift += len(new) - (stop - start)
        end = stop
    if resulting_size > MAX_TEXT_FILE:
        raise Fault("source_limit", "The resulting W1 source exceeds 128 KiB.")
    chunks.append(raw[end:])
    return b"".join(chunks), ranges


def _context(source: dict) -> dict:
    value = source["context"]
    return {"root": str(source["root"]), **{key: value[key] for key in (
        "root_id", "worktree", "workspace_identity", "workspace_identity_version", "git_state", "head_sha")}}


def _prepare(config, machine, workspace, root: Path, request: dict) -> tuple[dict, bytes, dict]:
    source = snapshot(root, request["path"])
    observed = source["context"]
    if root_for(config, machine, workspace) != root or (
        observed["workspace_identity"] != request["expected_workspace_identity"] or
        (observed["head_sha"] or "") != request["expected_head_sha"]
    ):
        raise Fault("context_mismatch", "The workspace, branch or HEAD no longer matches the observation.")
    previous = source["raw"]
    if digest(previous) != request["expected_sha256"]:
        raise Fault("content_mismatch", "The complete original source no longer matches its observed hash.")
    if request["operation"] == "edit":
        if previous is None:
            raise Fault("source_missing", "Exact editing requires an existing file.")
        desired, ranges = _replace(previous, request["payload"])
    else:
        desired = request["payload"].encode()
        ranges = [{"old_start": 0, "old_end": len(previous or b""), "new_start": 0, "new_end": len(desired)}]
    mode = stat.S_IMODE(source["metadata"].st_mode) if source["metadata"] else None
    plan = {"context": _context(source), "path": request["path"], "before_sha256": digest(previous),
            "after_sha256": digest(desired), "before_mode": mode, "after_mode": mode if mode is not None else 0o644,
            "ancestors": [list(item) for item in source["ancestors"]], "ranges": ranges}
    if request["expected_plan_sha256"] and request["expected_plan_sha256"] != _hash(plan):
        raise Fault("plan_mismatch", "The recomputed plan differs from the approved preview.")
    return source, desired, plan


def _diff(source: dict, desired: bytes, ranges: list[dict]) -> dict:
    """Render changed line windows directly from prepared spans, without fuzzy comparison."""
    before, after = source["raw"] or b"", desired
    if before == after:
        return {"diff": "", "diff_omitted": False, "truncated": False, "content_redacted": False}
    originals = [before.decode(), after.decode()]
    safe = [redact(value) for value in originals]
    if any(a.count("\n") != b.count("\n") for a, b in zip(originals, safe)):
        return {"diff": "", "diff_omitted": True, "truncated": False, "content_redacted": True}
    lines = []
    for value in safe:
        pieces = value.split("\n")
        lines.append([part + "\n" for part in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else []))
    windows = []
    for span in ranges:
        pair = []
        for raw, start_key, end_key in ((before, "old_start", "old_end"), (after, "new_start", "new_end")):
            start, end = span[start_key], span[end_key]
            first = raw.count(b"\n", 0, start)
            empty_tail = start == end == len(raw) and (not raw or raw.endswith(b"\n"))
            last = first if empty_tail else raw.count(b"\n", 0, max(start, end - 1)) + 1
            pair.extend((first, last))
        if windows and (pair[0] <= windows[-1][1] or pair[2] <= windows[-1][3]):
            windows[-1][1] = max(windows[-1][1], pair[1])
            windows[-1][3] = max(windows[-1][3], pair[3])
        else:
            windows.append(pair)
    result = [f"--- a/{source['path']}\n+++ b/{source['path']}\n"]
    for a, b, c, d in windows:
        result.append(f"@@ -{a + 1 if b > a else a},{b - a} +{c + 1 if d > c else c},{d - c} @@\n")
        for prefix, selected in (("-", lines[0][a:b]), ("+", lines[1][c:d])):
            for line in selected:
                result.append(prefix + line)
                if not line.endswith("\n"):
                    result.append("\n\\ No newline at end of file\n")
        if sum(len(value.encode()) for value in result) > TEXT_PAGE:
            break
    raw = "".join(result).encode()
    return {"diff": raw[:TEXT_PAGE].decode("utf-8", errors="ignore"), "diff_omitted": False,
            "truncated": len(raw) > TEXT_PAGE, "content_redacted": safe != originals}


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
        if type(record.get("format")) is not int or record["format"] != CHANGE_FORMAT:
            raise Fault("unsupported_change_format", "The retained change format requires a compatible reader.")
        if set(record) != {"format", "change_id", "writer_version", "owner_sha256", "request_sha256", "operation",
                           "created_at", "revision", "context", "expected", "plan", "plan_sha256", "effects",
                           "status", "finished_at", "error"}:
            raise ValueError
        if (record["change_id"] != change_id or not _hex(change_id, 32) or
            not _hex(record["owner_sha256"], 64) or not _hex(record["request_sha256"], 64) or
            record["operation"] not in {"edit", "write"} or
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
            if set(span) != {"old_start", "old_end", "new_start", "new_end"} or any(type(v) is not int or not 0 <= v <= MAX_TEXT_FILE for v in span.values()):
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


def _scope(config, state: Path, machine: str, record: dict) -> str:
    config.machine(machine)
    if _owner(state) != record["owner_sha256"]:
        raise Fault("scope_unavailable", "This receipt belongs to its original target state owner.")
    for name, item in config.workspaces.items():
        if item.machine != machine:
            continue
        try:
            root = root_for(config, machine, name)
            info = root.stat()
            if str(root) == record["context"]["root"] and [info.st_dev, info.st_ino] == record["context"]["root_id"]:
                return name
        except (OSError, Fault):
            continue
    raise Fault("scope_unavailable", "Current configuration no longer grants the original canonical workspace.")


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
        return record | {"writer_alive": alive, "owner_observed_at": observed_at, "reconciliation": classification,
                         "historical_diff_available": False,
                         "next_observation": {"operation": "read_workspace_change", "change_id": record["change_id"]}}


def read_change(config, state: Path, machine: str, change_id: str, observe_current: bool = False) -> dict:
    config.machine(machine)
    if not _hex(change_id, 32) or type(observe_current) is not bool:
        raise Fault("invalid_change_id", "Use the original 32-lowercase-hex change ID and a boolean observation flag.")
    store = _store(state)
    if store is None or not os.path.lexists(store / change_id):
        raise Fault("change_not_found", "No retained record was found; absence is not proof of non-admission.")
    directory = store / change_id
    try:
        record = _observed(directory)
    except FileNotFoundError as error:
        raise Fault("change_history_unavailable", "The original allocation is incomplete; never reuse this ID.") from error
    name = _scope(config, state, machine, record)
    result = record | {"machine": machine, "workspace": name}
    if observe_current:
        try:
            source = snapshot(Path(record["context"]["root"]), record["expected"]["path"])
            result["current"] = {"exists": source["raw"] is not None, "sha256": digest(source["raw"]),
                                 "observed_at": time.time(), "context": _context(source)}
        except (OSError, Fault) as error:
            result["current"] = {"error": error.code if isinstance(error, Fault) else type(error).__name__}
    return result


def check_change_state(state: Path) -> dict:
    """Public, read-only format validation for candidate/target compatibility checks."""
    store = _store(state)
    count = 0
    if store is not None:
        owner = _owner(state)
        for directory in _entries(store):
            try:
                record = _load(directory)
            except FileNotFoundError as error:
                raise Fault("change_history_unavailable", "A retained allocation has incomplete history.") from error
            if record["owner_sha256"] != owner:
                raise Fault("change_store_corrupt", "A retained change belongs to a different state owner.")
            count += 1
    return {"workspace_change_format": CHANGE_FORMAT, "workspace_change_records": count,
            "workspace_change_store_present": store is not None}


def _apply(config, state, machine, workspace, request, change_id, dry_run, validate_new):
    root = root_for(config, machine, workspace)
    if dry_run:
        source, desired, plan = _prepare(config, machine, workspace, root, request)
        target = config.machine(machine)
        grant = target.workspace_write_enabled
        effective = target.execution_enabled if grant is None else grant
        return {"machine": machine, "workspace": workspace, "status": "preview", "plan": plan,
                "plan_sha256": _hash(plan), "workspace_write_enabled": effective, "diff_kind": "proposed",
                **_diff(source, desired, plan["ranges"])}
    fingerprint = _hash(request | {"root": str(root)})
    # Existing IDs are reconciled before new permissions, leases or mutable source checks.
    store = _store(state)
    if store is not None and os.path.lexists(store / change_id):
        original = read_change(config, state, machine, change_id)
        if original["request_sha256"] != fingerprint:
            raise Fault("change_id_conflict", "This change ID already belongs to a different immutable request.")
        return original
    config.workspace_machine(machine)
    observed = context(root)
    if observed["workspace_identity"] is None:
        raise Fault("context_unavailable", "Workspace identity is unavailable; no change was admitted.")
    with _own(state, change_id) as (directory, existing):
        if existing:
            original = read_change(config, state, machine, change_id)
            if original["request_sha256"] != fingerprint:
                raise Fault("change_id_conflict", "This change ID belongs to a different request.")
            return original
        record = {"format": CHANGE_FORMAT, "change_id": change_id, "writer_version": __version__,
                  "owner_sha256": _owner(state), "request_sha256": fingerprint, "operation": request["operation"],
                  "created_at": time.time(), "revision": 0,
                  "context": _context({"root": root, "context": observed}),
                  "expected": {"path": request["path"], "workspace_identity": request["expected_workspace_identity"],
                               "head_sha": request["expected_head_sha"], "sha256": request["expected_sha256"],
                               "plan_sha256": request["expected_plan_sha256"]},
                  "plan": None, "plan_sha256": None, "effects": [], "status": "accepted", "finished_at": None, "error": None}
        _save(directory, record)
        try:
            if validate_new is not None:
                validate_new(root)
            key = hashlib.sha256(lock_root(config, machine, root, observed).encode()).hexdigest()
            with lease(state / "locks" / f"workspace-{key}.lock"):
                source, desired, plan = _prepare(config, machine, workspace, root, request)
                if plan["context"] != record["context"]:
                    raise Fault("context_mismatch", "Workspace context changed after request admission.")
                effect = {"path": request["path"], "temporary": ".openmachine-change-" + change_id,
                          "stage": "not_attempted", "result_sha256": None, "synced": False,
                          "readback_verified": False, "error": None, "cleanup": "not_created"}
                record.update(plan=plan, plan_sha256=_hash(plan), effects=[effect], status="prepared")
                _save(directory, record)
                display = _diff(source, desired, plan["ranges"])
                if source["raw"] == desired:
                    effect.update(stage="unchanged", result_sha256=digest(desired), readback_verified=True)
                    record["status"] = "unchanged"
                else:
                    record["status"] = "applying"
                    effect["stage"] = "attempting"
                    _save(directory, record)
                    effect.update(publish_file(config, machine, workspace, source, desired, effect["temporary"]))
                    record["status"] = "applied" if effect["stage"] == "applied" and effect["cleanup"] == "removed" else (
                        "rejected" if effect["stage"] == "failed_no_effect" and effect["cleanup"] != "unknown" else "outcome_unknown")
                    record["error"] = effect["error"]
                record["finished_at"] = time.time()
                _save(directory, record)
                result = _observed(directory, record, owned=True) | {"machine": machine, "workspace": workspace}
                if record["status"] in {"applied", "unchanged"}:
                    result.update(display, diff_kind="applied")
                return result
        except (OSError, Fault) as error:
            code = error.code if isinstance(error, Fault) else type(error).__name__
            uncertain = code == "change_record_unverified" or any(e["stage"] != "not_attempted" for e in record["effects"])
            record.update(status="outcome_unknown" if uncertain else "rejected", error=code, finished_at=time.time())
            if not uncertain:
                _save(directory, record)
                return _observed(directory, record, owned=True) | {"machine": machine, "workspace": workspace}
            raise Fault("change_outcome_unknown", "Read this original change and current files; never replay uncertain effects.",
                        record | {"record_persistence": "unverified", "machine": machine, "workspace": workspace}) from error


def edit_text_file(config, state: Path, machine: str, workspace: str, path: str, edits: list[dict],
                   expected_workspace_identity: str, expected_head_sha: str, expected_sha256: str,
                   change_id: str = "", dry_run: bool = False, expected_plan_sha256: str = "",
                   *, validate_new=None) -> dict:
    try:
        if type(dry_run) is not bool or (change_id != "" if dry_run else not _hex(change_id, 32)):
            raise Fault("invalid_change_id", "Preview requires no ID; apply requires a new 32-lowercase-hex ID.")
        request = _request("edit", path, edits, expected_workspace_identity, expected_head_sha, expected_sha256, expected_plan_sha256)
        return _apply(config, state, machine, workspace, request, change_id, dry_run, validate_new)
    except Fault as error:
        error.evidence = {"machine": machine, "workspace": workspace, "change_id": change_id,
                          **(error.evidence or {})}
        raise


def write_text_file(config, state: Path, machine: str, workspace: str, path: str, content: str,
                    expected_head_sha: str, expected_sha256: str, change_id: str = "",
                    expected_workspace_identity: str = "", *, validate_new=None) -> dict:
    if not _hex(change_id, 32, empty=True) or not _hex(expected_workspace_identity, 64, empty=True):
        raise Fault("invalid_precondition", "Use a valid change ID and workspace identity, or explicit empty legacy values.")
    if change_id:
        try:
            request = _request("write", path, content, expected_workspace_identity, expected_head_sha, expected_sha256, "")
            return _apply(config, state, machine, workspace, request, change_id, False, validate_new)
        except Fault as error:
            error.evidence = {"machine": machine, "workspace": workspace, "change_id": change_id, **(error.evidence or {})}
            raise
    config.workspace_machine(machine)
    if not _hex(expected_head_sha, 40, empty=True) or not _hex(expected_sha256, 64, empty=True):
        raise Fault("invalid_precondition", "Supply the observed HEAD and whole-file hash; empty means absent.")
    desired = _text(content)
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


def bound_result(payload: dict, encoded_size, limit: int = MAX_WORKSPACE_RESULT) -> dict:
    """Trim optional presentation only; never discard identity or effect evidence."""
    result = dict(payload)
    for field in ("diff", "text"):
        while encoded_size(result) > limit and result.get(field):
            raw = result[field].encode()
            result[field] = raw[:len(raw) // 2].decode("utf-8", errors="ignore")
            result["truncated"] = True
    if encoded_size(result) > limit:
        raise Fault("workspace_response_limit", "Mandatory workspace evidence exceeds its response budget.",
                    {"change_id": result.get("change_id"), "status": "outcome_unknown"})
    return result
