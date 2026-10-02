"""Pinned credential stores; values never cross the public connection interface."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory, save_json

MAX_BYTES = 16 * 1024
KEYCHAIN_CHUNK = 120  # security(1) stdin password input truncates at 128 bytes.
MAX_KEYCHAIN_CHUNKS = (MAX_BYTES + KEYCHAIN_CHUNK - 1) // KEYCHAIN_CHUNK
StoreName = Literal["keychain", "file"]
Number = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    format: Literal[1] = 1
    account_id: Number
    phase: Literal["ready", "refreshing", "unverified"]
    access_token: str = Field(min_length=16, max_length=2048, repr=False)
    refresh_token: str = Field(min_length=16, max_length=2048, repr=False)
    access_expires_at_ms: Number
    refresh_expires_at_ms: Number

    @model_validator(mode="after")
    def valid(self):
        if (not 0 < self.access_expires_at_ms < self.refresh_expires_at_ms
                or self.phase != "unverified" and not self.account_id):
            raise ValueError("invalid credential identity or deadlines")
        return self


def select() -> StoreName:
    return "keychain" if sys.platform == "darwin" else "file"


def _name(state: Path, identity: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise Fault("integration_credential_invalid", "Use the original owned credential reference.")
    return hashlib.sha256(str(state.resolve()).encode()).hexdigest()[:16] + ":" + identity


def _directory(state: Path, *, create: bool = False) -> Path:
    paths = (state, state / "credentials", state / "credentials/github")
    for path in paths:
        if create:
            private_directory(path)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe private directory")
    return paths[-1]


def _safe(info) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > MAX_BYTES):
        raise ValueError("unsafe credential file")


def _decode(raw: bytes) -> Credential:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate field")
            value[key] = item
        return value
    try:
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError("invalid size")
        return Credential.model_validate(json.loads(raw, object_pairs_hook=pairs))
    except (UnicodeError, ValueError, TypeError):
        # Validation exceptions may contain tokens; do not chain them.
        raise Fault("integration_credential_invalid", "The pinned credential is invalid; no fallback was used.") from None


def _security(arguments: list[str], data: bytes | None = None):
    try:
        result = subprocess.run(["/usr/bin/security", *arguments], input=data,
                                capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Fault("integration_credential_store_unavailable", "The system credential store is unavailable.") from None
    if result.returncode not in {0, 44}:
        raise Fault("integration_credential_store_unavailable", "The system credential store is locked or unavailable.")
    return result


def _keychain_read(account: str) -> bytes | None:
    result = _security(["find-generic-password", "-a", account, "-s", "org.openmachine.github", "-w"])
    return None if result.returncode == 44 else result.stdout.rstrip(b"\n")


def _keychain_write(account: str, value: bytes) -> None:
    if not value or len(value) > KEYCHAIN_CHUNK or b"\n" in value:
        raise Fault("integration_credential_invalid", "The Keychain credential chunk is invalid.")
    result = _security(["add-generic-password", "-U", "-a", account, "-s", "org.openmachine.github", "-w"],
                       value + b"\n" + value + b"\n")
    if result.returncode or _keychain_read(account) != value:
        raise Fault("integration_credential_store_unavailable", "System credential persistence could not be verified.")


def _keychain_manifest(name: str) -> tuple[int, str] | None:
    raw = _keychain_read(name + ":manifest")
    if raw is None:
        return None
    try:
        count_text, digest = raw.decode("ascii").split(":", 1)
        count = int(count_text)
        if not 1 <= count <= MAX_KEYCHAIN_CHUNKS or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError
        return count, digest
    except (UnicodeError, ValueError):
        raise Fault("integration_credential_invalid", "The pinned Keychain manifest is invalid.") from None


def load(state: Path, identity: str, store: StoreName) -> Credential:
    name = _name(state, identity)
    if store == "keychain":
        manifest = _keychain_manifest(name)
        if manifest is None:
            raise Fault("integration_credential_missing", "The pinned credential is missing; reconnect the same account.")
        count, digest = manifest
        chunks = []
        for index in range(count):
            chunk = _keychain_read(f"{name}:{index}")
            if chunk is None:
                raise Fault("integration_credential_invalid", "The pinned Keychain credential is incomplete.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != digest:
            raise Fault("integration_credential_invalid", "The pinned Keychain credential failed integrity verification.")
        return _decode(raw)
    try:
        path = _directory(state) / f"{identity}.json"
        _safe(path.lstat())
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            _safe(os.fstat(stream.fileno()))
            return _decode(stream.read(MAX_BYTES + 1))
    except FileNotFoundError:
        raise Fault("integration_credential_missing", "The pinned credential is missing; reconnect the same account.") from None
    except (OSError, ValueError):
        raise Fault("integration_credential_store_unavailable", "The private credential file is unavailable or unsafe.") from None


def save(state: Path, identity: str, store: StoreName, value: Credential) -> None:
    name = _name(state, identity)
    raw = json.dumps(value.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    _decode(raw)
    if store == "keychain":
        # security(1) caps stdin password input at 128 bytes. Keep every secret
        # chunk below that bound and commit a small integrity manifest last.
        previous = _keychain_manifest(name)
        chunks = [raw[index:index + KEYCHAIN_CHUNK] for index in range(0, len(raw), KEYCHAIN_CHUNK)]
        if not chunks or len(chunks) > MAX_KEYCHAIN_CHUNKS:
            raise Fault("integration_credential_invalid", "The Keychain credential exceeds its bounded format.")
        for index, chunk in enumerate(chunks):
            _keychain_write(f"{name}:{index}", chunk)
        previous_count = previous[0] if previous is not None else 0
        for index in range(len(chunks), previous_count):
            result = _security(["delete-generic-password", "-a", f"{name}:{index}",
                                "-s", "org.openmachine.github"])
            if result.returncode not in {0, 44}:
                raise Fault("integration_credential_store_unavailable",
                            "Obsolete Keychain credential chunks could not be removed.")
        manifest = f"{len(chunks)}:{hashlib.sha256(raw).hexdigest()}".encode("ascii")
        _keychain_write(name + ":manifest", manifest)
        if load(state, identity, store) != value:
            raise Fault("integration_credential_store_unavailable", "System credential persistence could not be verified.")
        return
    try:
        path = _directory(state, create=True) / f"{identity}.json"
        if os.path.lexists(path):
            _safe(path.lstat())
        save_json(path, value.model_dump())
        if load(state, identity, store) != value:
            raise ValueError("readback mismatch")
    except (OSError, ValueError):
        raise Fault("integration_credential_store_unavailable", "Private credential persistence could not be verified.") from None


def delete(state: Path, identity: str, store: StoreName) -> str:
    name = _name(state, identity)
    if store == "keychain":
        manifest = _keychain_manifest(name)
        if manifest is None:
            return "absent"
        count, _ = manifest
        for index in range(count):
            result = _security(["delete-generic-password", "-a", f"{name}:{index}",
                                "-s", "org.openmachine.github"])
            if result.returncode not in {0, 44}:
                raise Fault("integration_credential_store_unavailable",
                            "Owned Keychain credential cleanup failed; the connection stays disabled.")
        result = _security(["delete-generic-password", "-a", name + ":manifest",
                            "-s", "org.openmachine.github"])
        if result.returncode not in {0, 44}:
            raise Fault("integration_credential_store_unavailable",
                        "Owned Keychain credential cleanup failed; the connection stays disabled.")
        return "deleted"
    try:
        path = _directory(state) / f"{identity}.json"
        _safe(path.lstat())
        path.unlink()
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return "deleted"
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError):
        raise Fault("integration_credential_store_unavailable", "Owned credential cleanup failed; the connection stays disabled.") from None
