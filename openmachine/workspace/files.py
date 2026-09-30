"""Workspace path resolution and guarded text-file publication."""

import hashlib
import os
from pathlib import Path
import re
import uuid
from typing import TYPE_CHECKING

from openmachine.foundation.files import MAX_TEXT_FILE, excluded, file_bytes, file_parent, file_parts, text_page
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory

from .git import context

if TYPE_CHECKING:
    from openmachine.config import Settings


def root_for(config: "Settings", machine: str, workspace: str) -> Path:
    if config.machine(machine).transport != "local":
        raise Fault("remote_path", "Resolve this workspace through its SSH adapter, not the controller filesystem.")
    item = config.workspaces.get(workspace)
    if item is None or item.machine != machine:
        raise Fault("unknown_workspace", "Select a workspace configured on this machine.")
    root = Path(item.root).resolve(strict=True)
    if not root.is_dir():
        raise Fault("source_unavailable", "The configured workspace is not a directory.")
    return root


def reader_path(root: Path, relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts or excluded(relative):
        return False
    try:
        resolved = (root / relative).resolve()
        return resolved.is_relative_to(root) and not excluded(resolved.relative_to(root))
    except (OSError, RuntimeError):
        return False


def read_text_file(config: "Settings", machine: str, workspace: str, path: str,
                   offset: int = 0, expected_sha256: str = "") -> dict:
    if config.machine(machine).transport == "ssh":
        from integrations.ssh import call
        return call(config, None, machine, "read_workspace_file", workspace=workspace, path=path,
                    offset=offset, expected_sha256=expected_sha256)
    parts = file_parts(path)
    root = root_for(config, machine, workspace)
    if not re.fullmatch(r"(?:[0-9a-f]{64})?", expected_sha256):
        raise Fault("invalid_hash", "Use the complete raw-byte SHA256 returned by the original read.")
    with file_parent(root, parts) as parent:
        raw, _ = file_bytes(parent, parts[-1])
    digest = hashlib.sha256(raw).hexdigest() if raw is not None else ""
    if expected_sha256 and digest != expected_sha256:
        raise Fault("source_changed", "The file changed between pages; inspect its current version.")
    return {"machine": machine, "workspace": workspace, "path": path, "context": context(root),
            "exists": raw is not None, "sha256": digest, **text_page(raw or b"", offset)}


def write_text_file(config: "Settings", state: Path, machine: str, workspace: str, path: str,
                    content: str, expected_head_sha: str, expected_sha256: str) -> dict:
    if config.machine(machine, write=True).transport == "ssh":
        from integrations.ssh import call
        return call(config, state, machine, "write_workspace_file", workspace=workspace, path=path, content=content,
                    expected_head_sha=expected_head_sha, expected_sha256=expected_sha256)
    parts = file_parts(path)
    if (not re.fullmatch(r"(?:[0-9a-f]{40})?", expected_head_sha)
            or not re.fullmatch(r"(?:[0-9a-f]{64})?", expected_sha256)):
        raise Fault("invalid_precondition", "Supply the observed HEAD and SHA256; empty values explicitly mean absent.")
    desired = content.encode("utf-8")
    if len(desired) > MAX_TEXT_FILE or b"\0" in desired:
        raise Fault("invalid_content", "Supply at most 128 KiB of UTF-8 text without NUL bytes.")
    root = root_for(config, machine, workspace)
    observed = context(root)
    if observed["git_status"] == "unavailable":
        raise Fault("context_unavailable", "Git/worktree identity could not be checked.")
    state = private_directory(state)
    locks = private_directory(state / "locks")
    key = hashlib.sha256(observed["worktree"].encode()).hexdigest()
    with lease(locks / f"workspace-{key}.lock"):
        current = context(root)
        if (root_for(config, machine, workspace) != root or current["git_status"] == "unavailable"
                or current["worktree"] != observed["worktree"]
                or (current["head_sha"] or "") != expected_head_sha):
            raise Fault("context_mismatch", "Workspace or Git HEAD changed; inspect the original workspace.")
        result = {"machine": machine, "workspace": workspace, "path": path,
                  "head_sha": current["head_sha"], "sha256": hashlib.sha256(desired).hexdigest(),
                  "size_bytes": len(desired)}
        with file_parent(root, parts) as parent:
            previous, metadata = file_bytes(parent, parts[-1])
            digest = hashlib.sha256(previous).hexdigest() if previous is not None else ""
            if previous == desired:
                return result | {"status": "already_current", "previous_sha256": digest}
            if digest != expected_sha256:
                raise Fault("content_mismatch", "The file no longer matches the observed bytes; do not overwrite it.")
            temporary = ".openmachine-edit-" + uuid.uuid4().hex
            published = False
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(desired)
                    os.fchmod(stream.fileno(), metadata.st_mode & 0o777 if metadata else 0o644)
                    stream.flush()
                    os.fsync(stream.fileno())
                latest = context(root)
                if (latest["git_status"] == "unavailable" or latest["worktree"] != current["worktree"]
                        or (latest["head_sha"] or "") != expected_head_sha):
                    raise Fault("context_mismatch", "Git HEAD changed before replacement.")
                # Final content check is adjacent to publication, not a kernel CAS against external writers.
                check, info = file_bytes(parent, parts[-1])
                if check != previous or (metadata and (info.st_ino, info.st_mode) != (metadata.st_ino, metadata.st_mode)):
                    raise Fault("content_mismatch", "The file changed before replacement.")
                if metadata is None:
                    os.link(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    published = True
                    os.unlink(temporary, dir_fd=parent)
                else:
                    os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                    published = True
                os.fsync(parent)
                actual, _ = file_bytes(parent, parts[-1])
                if actual != desired:
                    return result | {"status": "write_outcome_unknown", "detail": "Read the destination before retrying."}
            except (OSError, Fault):
                if published:
                    return result | {"status": "write_outcome_unknown", "detail": "Replacement occurred; durability/readback is unverified."}
                raise
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return result | {"status": "written", "previous_sha256": digest}
