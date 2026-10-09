"""Retained change store and writer ownership; no user-file publication."""

from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import stat
import time
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from . import change_schema as schema

MAX_CHANGES = 10000


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
    return schema.fingerprint({"state": str(state), "device": info.st_dev, "inode": info.st_ino, "uid": os.getuid()})


def _entries(store: Path) -> list[Path]:
    entries = []
    for item in store.iterdir():
        if not schema.is_hex(item.name, 32):
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


def _load(directory: Path) -> dict:
    _private(directory, directory=True)
    _private(directory / "writer.lock")
    path = directory / "record.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        actual = os.fstat(source.fileno())
        # An atomic record save may unlink this already-open prior snapshot.
        if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid() or actual.st_mode & 0o077
            or actual.st_nlink > 1 or actual.st_size > schema.MAX_RECORD):
            raise Fault("change_store_corrupt", "The opened change record is unsafe or oversized.")
        data = source.read(schema.MAX_RECORD + 1)
    try:
        record = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise Fault("change_store_corrupt", "The retained change is not valid bounded JSON.") from error
    if not isinstance(record, dict) or len(data) > schema.MAX_RECORD:
        raise Fault("change_store_corrupt", "The retained change is not a bounded object.")
    schema.validate_record(record, directory.name)
    return record


def _save(directory: Path, record: dict) -> None:
    candidate = record | {"revision": record["revision"] + 1}
    schema.validate_record(candidate, directory.name)
    if len(json.dumps(candidate, sort_keys=True).encode()) + 1 > schema.MAX_RECORD:
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
        classification = "terminal" if record["status"] in schema.TERMINAL else "owner_active" if alive else (
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
    return {"workspace_change_format": schema.CHANGE_FORMAT, "workspace_change_read_formats": list(schema.READ_CHANGE_FORMATS),
            "workspace_change_formats_present": sorted(formats), "workspace_change_records": count,
            "workspace_change_store_present": store is not None}
