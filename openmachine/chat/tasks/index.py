"""Derived task lookup metadata and private legacy/canonical record readers."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from openmachine.foundation.sanitize import Fault, redact

from . import records

MAX_CHECKPOINT = 32768
MAX_INDEX = 2 * 1024 * 1024
MAX_TASKS = 10000
INDEX_PAGE = 50
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
TEMPORARY = re.compile(r"\.record-(?:[a-z0-9_]{8}|task-[0-9a-f]{32})")


def task_id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Fault("invalid_task_id", "Use 1-64 letters, digits, hyphens or underscores, beginning with a letter or digit.")
    return value


def metadata(title: str, anchors: list[str]) -> dict:
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise Fault("invalid_task", "Supply a nonempty single-line title of at most 200 characters.")
    if (not isinstance(anchors, list) or len(anchors) > 16
            or any(not isinstance(a, str) or not a or len(a) > 256 or any(ord(c) < 32 for c in a) for a in anchors)):
        raise Fault("invalid_task", "Use at most 16 explicit single-line lookup anchors of 1-256 characters.")
    if any(redact(value) != value for value in (title, *anchors)):
        raise Fault("invalid_task", "Keep credentials out of task metadata.")
    return {"title": title, "anchors": sorted(set(anchors))}


def private_path(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not kind or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Fault("corrupt_tasks", "Task state must use private, user-owned directories and regular files without links.")
    return info


def read_bytes(path: Path, limit: int) -> bytes:
    original = private_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit
                or info.st_uid != os.getuid() or info.st_mode & 0o077
                or (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino)):
            raise Fault("corrupt_tasks", "A task state file exceeds its bound or changed its private identity.")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Fault("corrupt_tasks", "A task state file exceeds its size bound.")
    return raw


def load_index(root: Path) -> dict:
    """Validate metadata only; canonical lookup does not depend on this file."""
    try:
        value = json.loads(read_bytes(root / "index.json", MAX_INDEX), object_pairs_hook=records._pairs)
        if not isinstance(value, dict) or type(value.get("format_version")) is not int or value["format_version"] not in (1, 2):
            raise Fault("unsupported_task_format", "This runtime does not support the task index format.")
        version = value["format_version"]
        keys = {"format_version", "generation", "tasks"} | ({"coverage", "next_cursor"} if version == 2 else set())
        if (set(value) != keys or type(value.get("generation")) is not int
                or not 0 <= value["generation"] <= records.MAX_INTEGER or not isinstance(value.get("tasks"), dict)
                or len(value["tasks"]) > MAX_TASKS):
            raise ValueError("invalid index")
        if version == 2:
            if value["coverage"] not in ("complete", "incomplete") or value["coverage"] == "complete" and value["next_cursor"] is not None:
                raise ValueError("invalid coverage")
            if value["next_cursor"] is not None:
                task_id(value["next_cursor"])
        for identity, item in value["tasks"].items():
            task_id(identity)
            item_keys = {"title", "anchors", "revision", "sha256"} | ({"owner"} if version == 2 else set())
            if (not isinstance(item, dict) or set(item) != item_keys
                    or type(item["revision"]) is not int or not 1 <= item["revision"] <= records.MAX_INTEGER
                    or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                    or version == 2 and item["owner"] not in ("legacy", "structured")):
                raise ValueError("invalid metadata")
            if metadata(item["title"], item["anchors"]) != {k: item[k] for k in ("title", "anchors")}:
                raise ValueError("invalid normalized metadata")
        if version == 1 and value["generation"] != sum(item["revision"] for item in value["tasks"].values()):
            raise ValueError("inconsistent legacy generations")
        return value
    except Fault as error:
        if error.code in {"unsupported_task_format", "corrupt_tasks"}:
            raise
        raise Fault("corrupt_tasks", "Task index metadata is invalid; do not initialize over existing state.") from error
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        raise Fault("corrupt_tasks", "Task index is missing or corrupt; preserve existing records.") from error


def directory_names(root: Path) -> list[str]:
    names = []
    try:
        for scanned, path in enumerate(root.iterdir(), 1):
            if scanned > MAX_TASKS + 1:
                raise Fault("task_index_full", "Task discovery exceeds its 10,000-entry budget; no history was removed.")
            if path.name == "index.json":
                continue
            if TEMPORARY.fullmatch(path.name):
                private_path(path, directory=path.name.startswith(".record-task-"))
                continue
            task_id(path.name)
            private_path(path, directory=True)
            names.append(path.name)
        return sorted(names)
    except (OSError, ValueError) as error:
        raise Fault("corrupt_tasks", "Task directory discovery is unavailable; preserve original history.") from error
    except Fault as error:
        if error.code == "invalid_task_id":
            raise Fault("corrupt_tasks", "An unrecognized task-store entry requires explicit reconciliation.") from error
        raise


def catalog_complete(root: Path, index: dict, names: list[str] | None = None) -> bool:
    names = directory_names(root) if names is None else names
    known = index["tasks"]
    if set(known) - set(names):
        raise Fault("corrupt_tasks", "A registered task directory is missing; do not remove its history from the index.")
    for identity in names:
        directory = root / identity
        structured = os.path.lexists(directory / "task.json")
        legacy = os.path.lexists(directory / "task.md")
        owner = known.get(identity, {}).get("owner", "legacy" if identity in known else None)
        if (structured and legacy or not structured and not legacy or owner == "structured" and not structured
                or owner == "legacy" and structured or owner is None and not structured):
            raise Fault("corrupt_tasks", "A task has missing or conflicting ownership evidence; do not convert or overwrite it.")
    return set(names) == set(known) and index.get("coverage", "complete") == "complete"


def read_index(root: Path) -> dict:
    value = load_index(root)
    if not catalog_complete(root, value):
        code = "corrupt_tasks" if value["format_version"] == 1 else "task_index_incomplete"
        raise Fault(code, "Task directories and index disagree; explicitly rebuild eligible derived metadata before new writes.")
    return value


def read_structured(root: Path, identity: str) -> tuple[dict, bytes]:
    try:
        private_path(root / identity, directory=True)
        if os.path.lexists(root / identity / "task.md"):
            raise Fault("corrupt_tasks", "Canonical and legacy task bodies cannot share an ID without approved conversion.")
        raw = read_bytes(root / identity / "task.json", records.MAX_RECORD)
        value = records.decode(raw)
        if value["task_id"] != identity:
            raise ValueError("task identity mismatch")
        return value, raw
    except (OSError, ValueError) as error:
        raise Fault("corrupt_tasks", "The original canonical task is unavailable; preserve it without Markdown fallback.") from error


def structured_metadata(record: dict, raw: bytes) -> dict:
    return {"owner": "structured", "title": record["title"], "anchors": record["anchors"],
            "revision": record["revision"], "sha256": hashlib.sha256(raw).hexdigest()}


def read_checkpoint(root: Path, index: dict, identity: str) -> tuple[dict, bytes]:
    if identity not in index["tasks"]:
        raise Fault("task_not_found", "No checkpoint exists for this task ID in this store.")
    item = index["tasks"][identity]
    if item.get("owner") == "structured" or os.path.lexists(root / identity / "task.json"):
        raise Fault("task_conflict", "Legacy save/read cannot convert a structured task or overwrite its canonical record.")
    try:
        private_path(root / identity, directory=True)
        raw = read_bytes(root / identity / "task.md", MAX_CHECKPOINT)
        if hashlib.sha256(raw).hexdigest() != item["sha256"] or not raw.strip() or b"\0" in raw:
            raise ValueError("checkpoint mismatch")
        raw.decode("utf-8")
    except (OSError, ValueError) as error:
        raise Fault("corrupt_tasks", "The original checkpoint is missing, changed or incomplete; do not overwrite it.") from error
    return {key: value for key, value in item.items() if key != "owner"}, raw
