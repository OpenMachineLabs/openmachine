"""Verify and retain an offline runtime payload; no downloads or activation."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import sys

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory

MANIFEST_LIMIT = 4 * 1024 * 1024
FILE_LIMIT = 20000
BYTE_LIMIT = 2 * 1024 * 1024 * 1024
TARGET = {"os": "macos", "arch": "arm64", "minimum_os": "13.0"}


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or str(path) != value or ".." in path.parts
            or "\\" in value or any(ord(char) < 32 for char in value)):
        raise ValueError("unsafe payload path")
    return path


def _unreadable(error: OSError) -> None:
    raise error


def inventory(root: Path) -> dict:
    """Bind every directory, regular file and contained symlink, without following links."""
    entries, total = {}, 0
    for directory, directories, files in os.walk(root, followlinks=False, onerror=_unreadable):
        for name in sorted(directories + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            _relative(relative)
            if relative == "manifest.json":
                continue
            info = path.lstat()
            if len(entries) >= FILE_LIMIT:
                raise ValueError("payload entry limit")
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(path)
                if (Path(link).is_absolute() or "\\" in link
                        or any(ord(char) < 32 for char in link)
                        or not path.resolve(strict=True).is_relative_to(root)):
                    raise ValueError("escaping or broken payload link")
                entries[relative] = {"link": link}
            elif info.st_mode & 0o022:
                raise ValueError("writable payload member")
            elif stat.S_ISDIR(info.st_mode):
                entries[relative] = {"directory": True}
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if total > BYTE_LIMIT:
                    raise ValueError("payload byte limit")
                with path.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                entries[relative] = {"sha256": digest, "bytes": info.st_size,
                                     "executable": bool(info.st_mode & 0o111)}
            else:
                raise ValueError("unsupported payload member")
    return entries


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def verify(root: Path, expected_sha256: str) -> dict:
    """The expected manifest digest must come from an already trusted product artifact."""
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("missing trusted manifest digest")
        root = root.resolve(strict=True)
        if not root.is_dir() or root.stat().st_mode & 0o022:
            raise ValueError("unsafe payload root")
        path = root / "manifest.json"
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > MANIFEST_LIMIT or info.st_mode & 0o022):
            raise ValueError("unsafe manifest")
        raw = path.read_bytes()
        if len(raw) > MANIFEST_LIMIT or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("manifest digest mismatch")
        value = json.loads(raw, object_pairs_hook=_pairs)
        if (set(value) != {"format", "target", "core", "python", "git", "files"}
                or type(value["format"]) is not int or value["format"] != 1
                or value["target"] != TARGET):
            raise ValueError("unsupported manifest")
        if (sys.platform != "darwin" or platform.machine() != "arm64"
                or int(platform.mac_ver()[0].split(".")[0]) < 13):
            raise Fault("unsupported_payload_host", "This runtime payload requires Apple Silicon and macOS 13 or newer.")
        core = value["core"]
        if (set(core) != {"version", "commit", "wheel"}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+!-]{0,99}", core["version"])
                or not re.fullmatch(r"[0-9a-f]{40}", core["commit"])
                or _relative(core["wheel"]).parent != PurePosixPath("wheels")
                or not core["wheel"].endswith(".whl")):
            raise ValueError("invalid Core identity")
        for name in ("python", "git"):
            item = value[name]
            if (set(item) != {"version", "build", "url", "sha256"}
                    or any(not isinstance(item[key], str) or not 0 < len(item[key]) <= 2048 for key in item)
                    or not item["url"].startswith("https://")
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("invalid input provenance")
        if not isinstance(value["files"], dict) or inventory(root) != value["files"]:
            raise ValueError("payload contents differ from the complete manifest")
        for name in ("python/bin/python3", "bin/git", "git/bin/git", core["wheel"],
                     "requirements.lock", "THIRD_PARTY_NOTICES.md"):
            if name not in value["files"] or not (root / name).is_file():
                raise ValueError("missing required payload member")
        for name in ("python/bin/python3", "bin/git", "git/bin/git"):
            if not os.access(root / name, os.X_OK):
                raise ValueError("payload entry is not executable")
        return value
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError) as error:
        raise Fault("invalid_runtime_payload", "The runtime payload is incomplete, altered, unsafe or incompatible; no installation was selected.") from error


def retain(state: Path, source: Path, expected_sha256: str) -> tuple[Path, dict]:
    """Caller holds the existing service operation lock; preserve incomplete copies."""
    manifest = verify(source, expected_sha256)
    source = source.resolve(strict=True)
    parent = private_directory(state / "service/payloads")
    destination = parent / expected_sha256
    if destination.is_symlink():
        raise Fault("invalid_runtime_payload", "The retained payload path is not an owned directory.")
    if destination.exists():
        verify(destination, expected_sha256)
        return destination, manifest
    if destination.is_relative_to(source):
        raise Fault("invalid_runtime_payload", "The retained destination must be outside the source payload.")
    # Reserve space for both the retained payload and its isolated environment.
    required = sum(item.get("bytes", 0) for item in manifest["files"].values()) * 2
    if shutil.disk_usage(parent).free < required:
        raise Fault("insufficient_disk_space", "There is insufficient space to retain this runtime; no installation was selected.")
    shutil.copytree(source, destination, symlinks=True)
    verify(destination, expected_sha256)
    # Prevent ordinary imports from adding caches to the immutable payload. This
    # is a lifetime invariant, not protection against the owning native account.
    for directory, directories, files in os.walk(destination, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o500 if path.stat().st_mode & 0o111 else 0o400)
        for name in directories:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o500)
    destination.chmod(0o500)
    return destination, manifest


def environment(root: Path) -> dict[str, str]:
    """Offline installation never inherits pip, Python, loader or Git configuration."""
    return {"PATH": os.pathsep.join((str(root / "bin"), str(root / "git/bin"), os.defpath)),
            "HOME": str(Path.home()), "LANG": "en_US.UTF-8", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1", "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
