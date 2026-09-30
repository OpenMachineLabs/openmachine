"""Workspace snapshots and the single guarded file publisher."""

import hashlib
import os
from pathlib import Path
import re
import stat
from typing import TYPE_CHECKING

from openmachine.foundation.files import excluded, file_bytes, file_parent, file_parts, text_page
from openmachine.foundation.sanitize import Fault

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


def digest(raw: bytes | None) -> str:
    return hashlib.sha256(raw).hexdigest() if raw is not None else ""


def read_text_file(config: "Settings", machine: str, workspace: str, path: str,
                   offset: int = 0, expected_sha256: str = "") -> dict:
    parts = file_parts(path)
    root = root_for(config, machine, workspace)
    if not re.fullmatch(r"(?:[0-9a-f]{64})?", expected_sha256):
        raise Fault("invalid_hash", "Use the complete raw-byte SHA256 returned by the original read.")
    observed = context(root)
    with file_parent(root, parts) as parent:
        raw, _ = file_bytes(parent, parts[-1])
    current = context(root)
    if (observed["workspace_identity"], observed["head_sha"]) != (current["workspace_identity"], current["head_sha"]):
        raise Fault("source_changed", "The workspace changed during observation.")
    sha = digest(raw)
    if expected_sha256 and sha != expected_sha256:
        raise Fault("source_changed", "The file changed between pages; inspect its current version.")
    return {"machine": machine, "workspace": workspace, "path": path, "context": current,
            "exists": raw is not None, "sha256": sha, **text_page(raw or b"", offset)}


def _metadata(info: os.stat_result | None) -> tuple | None:
    if info is None:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _ordinary_metadata(parent: int, name: str, info: os.stat_result | None) -> None:
    if info is None:
        return
    if info.st_mode & 0o7000 or getattr(info, "st_flags", 0) or info.st_uid != os.getuid():
        raise Fault("unsupported_metadata", "Only ordinary user-owned files and POSIX permission bits can be published.")
    if hasattr(os, "listxattr"):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            if os.listxattr(fd):
                raise Fault("unsupported_metadata", "Extended attributes or ACL metadata cannot be preserved by this publisher.")
        finally:
            os.close(fd)


def snapshot(root: Path, path: str) -> dict:
    """Read original bytes and their containing directory identities without effects."""
    parts = file_parts(path)
    if len(path.encode("utf-8")) > 1024:
        raise Fault("request_limit", "Use a relative path of at most 1024 UTF-8 bytes.")
    observed = context(root)
    if observed["git_status"] == "unavailable":
        raise Fault(observed.get("context_error", "context_unavailable"), "Workspace identity is unavailable.")
    ancestors = []
    with file_parent(root, parts, identities=ancestors) as parent:
        raw, metadata = file_bytes(parent, parts[-1])
        _ordinary_metadata(parent, parts[-1], metadata)
    # A nested repository is a separate configured workspace, not a descendant edit.
    parent_context = context(root.joinpath(*parts[:-1]))
    if parent_context["git_status"] == "unavailable" or (
        parent_context["git_status"] == "observed" and
        parent_context["worktree"] != observed["worktree"]
    ):
        raise Fault("nested_workspace", "Configure the nested repository as its own workspace.")
    current = context(root)
    if (current["workspace_identity"], current["head_sha"]) != (observed["workspace_identity"], observed["head_sha"]):
        raise Fault("context_mismatch", "The workspace changed while preparing the file.")
    return {"root": root, "path": path, "parts": parts, "raw": raw, "metadata": metadata,
            "context": observed, "ancestors": ancestors}


def _recheck(source: dict, parent: int, config, machine: str, workspace: str) -> None:
    root, parts = source["root"], source["parts"]
    if root_for(config, machine, workspace) != root:
        raise Fault("context_mismatch", "The configured workspace changed before publication.")
    observed, current = source["context"], context(root)
    if (current["workspace_identity"] is None or
        (current["workspace_identity"], current["head_sha"]) != (observed["workspace_identity"], observed["head_sha"])):
        raise Fault("context_mismatch", "Workspace root, branch or HEAD changed before publication.")
    ancestors = []
    with file_parent(root, parts, identities=ancestors) as attached:
        if ancestors != source["ancestors"] or os.fstat(attached).st_ino != os.fstat(parent).st_ino:
            raise Fault("context_mismatch", "A containing directory was replaced or detached.")
    raw, info = file_bytes(parent, parts[-1])
    if raw != source["raw"] or _metadata(info) != _metadata(source["metadata"]):
        raise Fault("content_mismatch", "The original bytes or file metadata changed before publication.")
    _ordinary_metadata(parent, parts[-1], info)


def publish_file(config, machine: str, workspace: str, source: dict, desired: bytes, temporary: str) -> dict:
    """Publish once. Report possible effects even when an OS operation loses its reply."""
    effect = {"stage": "failed_no_effect", "result_sha256": None, "synced": False,
              "readback_verified": False, "error": None, "cleanup": "not_created"}
    attempted = False
    created = None
    with file_parent(source["root"], source["parts"]) as parent:
        name = source["parts"][-1]
        try:
            _recheck(source, parent, config, machine, workspace)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as output:
                created = os.fstat(output.fileno())
                effect["cleanup"] = "pending"
                output.write(desired)
                info = source["metadata"]
                desired_mode = stat.S_IMODE(info.st_mode) if info else 0o644
                os.fchmod(output.fileno(), desired_mode)
                output.flush()
                os.fsync(output.fileno())
            _recheck(source, parent, config, machine, workspace)
            # Checks and rename/link are distinct syscalls, not a CAS against external editors.
            attempted = True
            if source["metadata"] is None:
                try:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    attempted = False
                    raise Fault("destination_exists", "The absent destination appeared before publication.") from None
                os.unlink(temporary, dir_fd=parent)
            else:
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            effect["cleanup"] = "removed"
            os.fsync(parent)
            effect["synced"] = True
            actual, info = file_bytes(parent, name)
            effect["result_sha256"] = digest(actual)
            if (actual != desired or info is None or stat.S_IMODE(info.st_mode) != desired_mode or
                (info.st_dev, info.st_ino) != (created.st_dev, created.st_ino)):
                raise Fault("publication_raced", "Post-publication bytes or ownership changed; preserve the original ID.")
            ancestors = []
            with file_parent(source["root"], source["parts"], identities=ancestors):
                if ancestors != source["ancestors"]:
                    raise Fault("publication_raced", "The target was detached during publication.")
            if root_for(config, machine, workspace) != source["root"]:
                raise Fault("publication_raced", "The configured root changed during publication.")
            current = context(source["root"])
            if (current["workspace_identity"], current["head_sha"]) != (
                source["context"]["workspace_identity"], source["context"]["head_sha"]
            ):
                raise Fault("publication_raced", "Workspace context changed during publication.")
            effect.update(stage="applied", readback_verified=True)
        except (OSError, Fault) as error:
            effect.update(stage="unknown" if attempted else "failed_no_effect",
                          error=error.code if isinstance(error, Fault) else type(error).__name__)
        finally:
            if created is not None and effect["cleanup"] != "removed":
                try:
                    info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (created.st_dev, created.st_ino):
                        raise OSError("Temporary identity changed")
                    os.unlink(temporary, dir_fd=parent)
                    os.fsync(parent)
                    effect["cleanup"] = "removed"
                except FileNotFoundError:
                    effect["cleanup"] = "removed"
                except OSError:
                    effect["cleanup"] = "unknown"
                    effect["error"] = effect["error"] or "cleanup_unverified"
    return effect
