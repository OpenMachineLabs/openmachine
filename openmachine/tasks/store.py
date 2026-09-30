"""Lock-coordinated checkpoint/index reads and saves, not a two-file transaction."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.foundation.sanitize import Fault, redact

from .index import MAX_CHECKPOINT, MAX_INDEX, metadata as validate_metadata, private_path, read_checkpoint, read_index, task_id


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
            yield root, read_index(root)
    except OSError as error:
        raise Fault("corrupt_tasks", "Existing task state or its lock is unavailable; preserve it.") from error


def read(state: Path, identity: str) -> dict:
    task_id(identity)
    with _reading(state) as store:
        if store is None:
            raise Fault("tasks_uninitialized", "This private task store has not been initialized.")
        root, index = store
        item, raw = read_checkpoint(root, index, identity)
        body = raw.decode("utf-8")
        return {"task_id": identity, **item, "body": redact(body), "content_redacted": redact(body) != body,
                "generation": index["generation"], "resume_authorized": False}


def find(state: Path, query: str = "", anchors: list[str] | None = None, identity: str = "",
         offset: int = 0, limit: int = 20, expected_generation: int = -1) -> dict:
    if identity:
        task_id(identity)
    wanted = validate_metadata("lookup", anchors or [])["anchors"]
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
            read_checkpoint(root, index, key)
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
    metadata = validate_metadata(title, anchors)
    if type(expected_revision) is not int or expected_revision < 0:
        raise Fault("invalid_revision", "Supply the observed nonnegative checkpoint revision; zero means new.")
    raw = body.encode("utf-8")
    if not raw.strip() or len(raw) > MAX_CHECKPOINT or b"\0" in raw or redact(body) != body:
        raise Fault("invalid_checkpoint", "Supply a nonempty checkpoint up to 32 KiB, without credentials or NUL bytes.")
    if expected_revision and not (state / "tasks").exists():
        raise Fault("corrupt_tasks", "The original task store is missing; do not initialize new history.")
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
        private_path(root, directory=True)
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
        index["tasks"][identity] = item
        index["generation"] += 1
        if len(json.dumps(index).encode()) > MAX_INDEX:
            raise Fault("task_index_full", "The bounded task index is full; preserve existing records.")
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
