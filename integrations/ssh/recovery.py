"""Private shared SSH observation budgets; no task state or network effects."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

MAX_BYTES = 65536
FIELDS = {"episode_id", "started_at_ms", "deadline_ms", "attempts", "next_attempt_ms", "reason_code"}


def _private(path: Path, *, directory: bool = False):
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("unsafe recovery state")
    return info


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _validate(value: dict) -> None:
    if not isinstance(value, dict) or set(value) != {"format", "targets"} or type(value["format"]) is not int or value["format"] != 1:
        raise ValueError("unsupported recovery format")
    if not isinstance(value["targets"], dict):
        raise ValueError("invalid targets")
    for target, episode in value["targets"].items():
        if not re.fullmatch(r"[0-9a-f]{64}", target):
            raise ValueError("invalid target")
        if episode is None:
            continue
        if (not isinstance(episode, dict) or set(episode) != FIELDS
                or not isinstance(episode["episode_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", episode["episode_id"])
                or not isinstance(episode["reason_code"], str) or not re.fullmatch(r"[a-z0-9_]{1,128}", episode["reason_code"])
                or any(type(episode[key]) is not int or not 0 <= episode[key] <= 2**53 - 1
                       for key in ("started_at_ms", "deadline_ms", "next_attempt_ms", "attempts"))
                or not 1 <= episode["attempts"] <= 60
                or episode["deadline_ms"] > episode["started_at_ms"] + 3600000
                or not episode["started_at_ms"] <= episode["next_attempt_ms"] <= episode["deadline_ms"]):
            raise ValueError("invalid recovery episode")


def _load(state: Path) -> dict:
    path = state / "ssh-recovery.json"
    original = _private(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino) or info.st_size > MAX_BYTES:
            raise ValueError("recovery identity or size changed")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("recovery state exceeds bound")
    value = json.loads(raw, object_pairs_hook=_pairs)
    _validate(value)
    return value


def read(state: Path) -> dict:
    """Read without initializing; a missing initialized store never resets budgets."""
    path, lock = state / "ssh-recovery.json", state / "locks/ssh-recovery.lock"
    if not os.path.lexists(path) and not os.path.lexists(lock):
        return {"format": 1, "targets": {}}
    try:
        _private(state, directory=True)
        _private(lock.parent, directory=True)
        _private(lock)
        with lease(lock, shared=True, create=False):
            return _load(state)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise Fault("ssh_recovery_corrupt", "Shared SSH observation history is missing, unsafe or corrupt; preserve it.") from error


def replace(state: Path, target: str, expected: dict | None, episode: dict | None) -> None:
    """Compare and durably publish under a short lock, never across an SSH read."""
    path, lock = state / "ssh-recovery.json", state / "locks/ssh-recovery.lock"
    existing = os.path.lexists(path) or os.path.lexists(lock)
    try:
        if existing:
            _private(state, directory=True)
            _private(lock.parent, directory=True)
            _private(lock)
        else:
            private_directory(state)
            private_directory(lock.parent)
        with lease(lock, create=not existing):
            value = _load(state) if existing or os.path.lexists(path) else {"format": 1, "targets": {}}
            if value["targets"].get(target) != expected:
                raise Fault("ssh_recovery_conflict", "The original shared connection budget advanced; read it again.")
            if target in value["targets"] and expected == episode:
                return
            value["targets"][target] = episode
            _validate(value)
            if len(json.dumps(value, sort_keys=True).encode()) + 1 > MAX_BYTES:
                raise Fault("ssh_recovery_capacity", "The bounded SSH recovery store is full; no history was removed.")
            save_json(path, value)
            directory = os.open(lock.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            if _load(state) != value:
                raise ValueError("recovery readback mismatch")
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise Fault("ssh_recovery_unverified", "Shared SSH recovery publication is unverified; do not read again or reset history.") from error


def check_state(state: Path) -> dict:
    value = read(state)
    present = os.path.lexists(state / "ssh-recovery.json")
    return {"ssh_recovery_present": present, "ssh_recovery_format": 1,
            "ssh_recovery_targets": len(value["targets"]),
            "ssh_recovery_sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() if present else None}
