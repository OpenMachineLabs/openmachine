"""Small, bounded readers; configured paths are routing, not a sandbox."""

import hashlib
import itertools
import os
from pathlib import Path
import stat
import subprocess
from typing import TYPE_CHECKING

from openmachine.sanitize import Fault, redact

if TYPE_CHECKING:
    from openmachine.config import Settings

MAX_READ = 65536
SECRET_NAMES = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".netrc", ".pypirc",
                "credentials", "credentials.json", "secrets.toml", "id_rsa", "id_ed25519"}


def excluded(path: Path) -> bool:
    return any(part in SECRET_NAMES or part == ".env" or part.startswith(".env.")
               or part.endswith((".pem", ".key", ".p12", ".pfx")) for part in path.parts)


def root_for(config: "Settings", machine: str, workspace: str) -> Path:
    config.machine(machine)
    item = config.workspaces.get(workspace)
    if item is None or item.machine != machine:
        raise Fault("unknown_workspace", "Select a workspace configured on this machine.")
    root = Path(item.root).resolve(strict=True)
    if not root.is_dir():
        raise Fault("source_unavailable", "The configured workspace is not a directory.")
    return root


def context(cwd: Path) -> dict:
    """Capture Git identity without changing the worktree or selecting an environment."""
    result = {"cwd": str(cwd), "worktree": str(cwd), "head_sha": None, "branch": None,
              "agents_files": [str(p / "AGENTS.md") for p in reversed((cwd, *cwd.parents))
                               if (p / "AGENTS.md").is_file()]}
    try:
        probe = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=5)
        if probe.returncode:
            result["git_status"] = "not_a_worktree"
            return result
        result["worktree"] = str(Path(probe.stdout.strip()).resolve())
        for key, args in (("head_sha", ["rev-parse", "--verify", "HEAD"]),
                          ("branch", ["symbolic-ref", "--quiet", "--short", "HEAD"])):
            item = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                                  text=True, timeout=5)
            result[key] = item.stdout.strip() if item.returncode == 0 else None
        result["git_status"] = "observed"
    except (OSError, subprocess.TimeoutExpired):
        result["git_status"] = "unavailable"
    return result


def inspect(config: "Settings", machine: str, workspace: str, action: str,
            path: str = ".", offset: int = 0, limit: int = 100) -> dict:
    root = root_for(config, machine, workspace)
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or excluded(relative):
        raise Fault("path_denied", "Use a relative, non-credential-bearing workspace path.")
    source = (root / relative).resolve(strict=True)
    if not source.is_relative_to(root) or excluded(source.relative_to(root)):
        raise Fault("path_denied", "The resolved path is outside the reader scope.")
    identity = {"machine": machine, "workspace": workspace, "path": str(source), "cwd": str(root)}
    if offset < 0 or not 1 <= limit <= 200:
        raise Fault("invalid_range", "Use a nonnegative offset and a limit from 1 to 200.")
    before = source.stat()
    if action == "files":
        if offset > 100000:
            raise Fault("invalid_range", "Directory listing offsets must not exceed 100000.")
        if not stat.S_ISDIR(before.st_mode):
            raise Fault("invalid_source", "File listing requires a directory.")
        with os.scandir(source) as entries:
            visible = (entry for entry in entries if not excluded(Path(entry.name)))
            page = list(itertools.islice(visible, offset, offset + limit + 1))
            items = [{"name": e.name, "directory": e.is_dir(follow_symlinks=False),
                      "symlink": e.is_symlink()} for e in page[:limit]]
        return {**identity, "entries": items, "next_offset": offset + limit if len(page) > limit else None,
                "mtime_ns": before.st_mtime_ns, "credential_paths_excluded": True,
                "ordering": "filesystem; recheck mtime when paging"}
    if action != "read":
        raise Fault("unsupported_operation", "This increment supports files and read only.")
    # O_NONBLOCK plus fstat prevents a replaced FIFO/device from hanging the service.
    fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        observed = os.fstat(stream.fileno())
        if not stat.S_ISREG(observed.st_mode):
            raise Fault("invalid_source", "Text reads require a regular file.")
        if offset > observed.st_size:
            raise Fault("invalid_range", "The byte offset is beyond the current file size.")
        stream.seek(offset)
        data = stream.read(MAX_READ)
        after = os.fstat(stream.fileno())
    if b"\0" in data:
        raise Fault("binary_source", "The selected range is not a text file.")
    return {**identity, "text": redact(data.decode("utf-8", errors="replace")),
            "offset": offset, "next_offset": offset + len(data), "size": after.st_size,
            "has_more": offset + len(data) < after.st_size, "mtime_ns": after.st_mtime_ns,
            "chunk_sha256": hashlib.sha256(data).hexdigest(),
            "source_changed": (observed.st_size, observed.st_mtime_ns) != (after.st_size, after.st_mtime_ns)}
