"""Private logical checkpoints and their lookup index, never an execution scheduler."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from openmachine.execution import _lease, _save, private_directory
from openmachine.sanitize import Fault, redact

MAX_CHECKPOINT = 32768
MAX_INDEX = 2 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def task_id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Fault("invalid_task_id", "Use 1-64 letters, digits, hyphens or underscores, beginning with a letter or digit.")
    return value


def _metadata(title: str, anchors: list[str]) -> dict:
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise Fault("invalid_task", "Supply a nonempty single-line title of at most 200 characters.")
    if (not isinstance(anchors, list) or len(anchors) > 16
            or any(not isinstance(a, str) or not a or len(a) > 256 or any(ord(c) < 32 for c in a) for a in anchors)):
        raise Fault("invalid_task", "Use at most 16 explicit single-line lookup anchors of 1-256 characters.")
    if any(redact(value) != value for value in (title, *anchors)):
        raise Fault("invalid_task", "Keep credentials out of task metadata.")
    return {"title": title, "anchors": sorted(set(anchors))}


def _private(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not kind or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Fault("corrupt_tasks", "Task state must use private, user-owned directories and regular files without links.")
    return info


def _bytes(path: Path, limit: int) -> bytes:
    _private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise Fault("corrupt_tasks", "A task state file exceeds its bound or has an invalid type.")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Fault("corrupt_tasks", "A task state file exceeds its size bound.")
    return raw


def _index(root: Path) -> dict:
    try:
        value = json.loads(_bytes(root / "index.json", MAX_INDEX))
        if not isinstance(value, dict) or type(value.get("format_version")) is not int or value["format_version"] != 1:
            raise Fault("unsupported_task_format", "This runtime does not support the task index format.")
        if (set(value) != {"format_version", "generation", "tasks"}
                or type(value.get("generation")) is not int or value["generation"] < 0 or not isinstance(value.get("tasks"), dict)):
            raise ValueError("invalid index")
        for identity, item in value["tasks"].items():
            task_id(identity)
            if (not isinstance(item, dict) or set(item) != {"title", "anchors", "revision", "sha256"}
                    or type(item["revision"]) is not int or item["revision"] < 1
                    or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("invalid metadata")
            if _metadata(item["title"], item["anchors"]) != {k: item[k] for k in ("title", "anchors")}:
                raise ValueError("invalid normalized metadata")
        if value["generation"] != sum(item["revision"] for item in value["tasks"].values()):
            raise ValueError("inconsistent metadata generations")
        names = set()
        for path in root.iterdir():
            if path.name == "index.json" or path.name.startswith(".record-"):
                continue
            task_id(path.name)
            _private(path, directory=True)
            names.add(path.name)
        if names != set(value["tasks"]):
            raise Fault("corrupt_tasks", "Task directories and index disagree; preserve state and reconcile the interrupted save.")
        return value
    except Fault as error:
        if error.code in {"unsupported_task_format", "corrupt_tasks"}:
            raise
        raise Fault("corrupt_tasks", "Task index metadata is invalid; do not initialize over existing state.") from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Fault("corrupt_tasks", "Task index is missing or corrupt; preserve existing records.") from error


def _checkpoint(root: Path, index: dict, identity: str) -> tuple[dict, bytes]:
    if identity not in index["tasks"]:
        raise Fault("task_not_found", "No checkpoint exists for this task ID in this store.")
    item = index["tasks"][identity]
    try:
        raw = _bytes(root / identity / "task.md", MAX_CHECKPOINT)
        if hashlib.sha256(raw).hexdigest() != item["sha256"] or not raw.strip() or b"\0" in raw:
            raise ValueError("checkpoint mismatch")
        raw.decode("utf-8")
    except (OSError, ValueError) as error:
        raise Fault("corrupt_tasks", "The original checkpoint is missing, changed or incomplete; do not overwrite it.") from error
    return item, raw


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
    _private(state, directory=True)
    try:
        root.lstat()
    except FileNotFoundError:
        if os.path.lexists(state / "locks/tasks.lock"):
            raise Fault("corrupt_tasks", "The existing task store disappeared; preserve its original history.")
        yield None
        return
    try:
        _private(root, directory=True)
        _private(state / "locks", directory=True)
        _private(state / "locks/tasks.lock")
        with _lease(state / "locks/tasks.lock", shared=True, create=False):
            yield root, _index(root)
    except OSError as error:
        raise Fault("corrupt_tasks", "Existing task state or its lock is unavailable; preserve it.") from error


def read(state: Path, identity: str) -> dict:
    task_id(identity)
    with _reading(state) as store:
        if store is None:
            raise Fault("tasks_uninitialized", "This private task store has not been initialized.")
        root, index = store
        item, raw = _checkpoint(root, index, identity)
        body = raw.decode("utf-8")
        return {"task_id": identity, **item, "body": redact(body), "content_redacted": redact(body) != body,
                "generation": index["generation"], "resume_authorized": False}


def find(state: Path, query: str = "", anchors: list[str] | None = None, identity: str = "",
         offset: int = 0, limit: int = 20, expected_generation: int = -1) -> dict:
    if identity:
        task_id(identity)
    wanted = _metadata("lookup", anchors or [])["anchors"]
    if (not isinstance(query, str) or len(query) > 200 or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 50 or type(expected_generation) is not int or expected_generation < -1):
        raise Fault("invalid_range", "Use a bounded lookup query, nonnegative offset and page size from 1 to 50.")
    with _reading(state) as store:
        if store is None:
            if expected_generation != -1:
                raise Fault("task_index_changed", "The original task index is no longer available.")
            return {"status": "uninitialized", "tasks": [], "next_offset": None, "generation": None,
                    "coverage": "this_store_only", "resume_authorized": False}
        root, index = store
        if expected_generation != -1 and expected_generation != index["generation"]:
            raise Fault("task_index_changed", "Task lookup metadata changed between pages; read the current index.")
        matches = [(key, value) for key, value in sorted(index["tasks"].items())
                   if (not identity or key == identity) and set(wanted) <= set(value["anchors"])
                   and query.casefold() in (key + " " + value["title"]).casefold()]
        page = matches[offset:offset + limit]
        for key, _ in page:
            _checkpoint(root, index, key)
        total = len(matches)
        exact = bool(identity or wanted) and not query
        status = "matched" if exact and total == 1 else "ambiguous" if exact and total > 1 else "candidates"
        return {"status": status, "tasks": [{"task_id": key, "title": item["title"], "revision": item["revision"]}
                                            for key, item in page], "total": total,
                "next_offset": offset + limit if offset + limit < total else None,
                "generation": index["generation"], "coverage": "registered_tasks_only", "resume_authorized": False}


def _replace(path: Path, raw: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(name).unlink(missing_ok=True)


def save(state: Path, identity: str, expected_revision: int, title: str, body: str, anchors: list[str]) -> dict:
    task_id(identity)
    metadata = _metadata(title, anchors)
    if type(expected_revision) is not int or expected_revision < 0:
        raise Fault("invalid_revision", "Supply the observed nonnegative checkpoint revision; zero means new.")
    raw = body.encode("utf-8")
    if not raw.strip() or len(raw) > MAX_CHECKPOINT or b"\0" in raw or redact(body) != body:
        raise Fault("invalid_checkpoint", "Supply a nonempty checkpoint up to 32 KiB, without credentials or NUL bytes.")
    if expected_revision and not (state / "tasks").exists():
        raise Fault("corrupt_tasks", "The original task store is missing; do not initialize new history.")
    state = private_directory(state)
    locks = private_directory(state / "locks")
    root = state / "tasks"
    previous_store = os.path.lexists(locks / "tasks.lock")
    with _lease(locks / "tasks.lock"):
        _private(locks / "tasks.lock")
        try:
            root.lstat()
        except FileNotFoundError:
            if previous_store:
                raise Fault("corrupt_tasks", "A prior task store is missing; do not initialize new history.")
            private_directory(root)
            _save(root / "index.json", {"format_version": 1, "generation": 0, "tasks": {}})
        _private(root, directory=True)
        index = _index(root)
        revision, previous = 0, None
        if identity in index["tasks"]:
            item, previous = _checkpoint(root, index, identity)
            revision = item["revision"]
            if raw == previous and metadata == {key: item[key] for key in ("title", "anchors")}:
                if expected_revision not in (revision, revision - 1):
                    raise Fault("task_conflict", "This retry does not match the last checkpoint revision.")
                return {"task_id": identity, **item, "generation": index["generation"], "status": "unchanged"}
        if expected_revision != revision:
            raise Fault("task_conflict", "A newer checkpoint exists; read it before updating progress.")
        item = {**metadata, "revision": revision + 1, "sha256": hashlib.sha256(raw).hexdigest()}
        index["tasks"][identity] = item
        index["generation"] += 1
        if len(json.dumps(index).encode()) > MAX_INDEX:
            raise Fault("task_index_full", "The bounded task index is full; preserve existing records.")
        directory = private_directory(root / identity)
        try:
            _replace(directory / "task.md", raw)
            _save(root / "index.json", index)
            actual = _index(root)
            observed, content = _checkpoint(root, actual, identity)
            if observed != item or content != raw:
                raise Fault("task_write_unverified", "Checkpoint readback did not match; inspect the original task.")
        except OSError as error:
            raise Fault("task_write_unverified", "The save has no verified durable receipt; read the original task before retrying.") from error
        return {"task_id": identity, **item, "generation": index["generation"], "status": "saved"}
