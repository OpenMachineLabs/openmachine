"""Task lookup metadata and persisted checkpoint/index integrity."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from openmachine.foundation.sanitize import Fault, redact

MAX_CHECKPOINT = 32768
MAX_INDEX = 2 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


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


def _bytes(path: Path, limit: int) -> bytes:
    private_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise Fault("corrupt_tasks", "A task state file exceeds its bound or has an invalid type.")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Fault("corrupt_tasks", "A task state file exceeds its size bound.")
    return raw


def read_index(root: Path) -> dict:
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
            if metadata(item["title"], item["anchors"]) != {k: item[k] for k in ("title", "anchors")}:
                raise ValueError("invalid normalized metadata")
        if value["generation"] != sum(item["revision"] for item in value["tasks"].values()):
            raise ValueError("inconsistent metadata generations")
        names = set()
        for path in root.iterdir():
            if path.name == "index.json" or path.name.startswith(".record-"):
                continue
            task_id(path.name)
            private_path(path, directory=True)
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


def read_checkpoint(root: Path, index: dict, identity: str) -> tuple[dict, bytes]:
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
