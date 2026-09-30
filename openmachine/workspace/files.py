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

MAX_SOURCE = 2 * 1024 * 1024
MAX_LINES = 500

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
                   offset: int = 0, expected_sha256: str = "", start_line: int | None = None,
                   line_count: int = 200) -> dict:
    if (type(offset) is not int or offset < 0 or type(line_count) is not int or not 1 <= line_count <= MAX_LINES or
        start_line is not None and (type(start_line) is not int or start_line < 1 or offset != 0)):
        raise Fault("invalid_range", "Use 1-based lines, 1..500 lines per request, and offset zero in line mode.")
    parts = file_parts(path)
    if len(path.encode("utf-8")) > 1024:
        raise Fault("request_limit", "Use a path of at most 1024 UTF-8 bytes.")
    root = root_for(config, machine, workspace)
    if not re.fullmatch(r"(?:[0-9a-f]{64})?", expected_sha256):
        raise Fault("invalid_hash", "Use the complete raw-byte SHA256 returned by the original read.")
    observed = context(root)
    with file_parent(root, parts) as parent:
        raw, metadata = file_bytes(parent, parts[-1], max_bytes=MAX_SOURCE)
    current = context(root)
    if (observed["workspace_identity"], observed["head_sha"]) != (current["workspace_identity"], current["head_sha"]):
        raise Fault("source_changed", "The workspace changed during observation.")
    sha = digest(raw)
    if expected_sha256 and sha != expected_sha256:
        raise Fault("source_changed", "The file changed between pages; inspect its current version.")
    for depth in range(1, len(parts)):
        instruction = root.joinpath(*parts[:depth], "AGENTS.md")
        if instruction.is_file() and not instruction.is_symlink():
            current["agents_files"].append(str(instruction))
    source = raw or b""
    stop = len(source)
    if start_line is not None:
        offset = 0
        for _ in range(start_line - 1):
            newline = source.find(b"\n", offset)
            if newline < 0:
                offset = len(source)
                break
            offset = newline + 1
        stop = offset
        for _ in range(line_count):
            newline = source.find(b"\n", stop)
            if newline < 0:
                stop = len(source)
                break
            stop = newline + 1
    page = text_page(source, offset, end=stop)
    end = page["next_offset"]
    page.update(start_line=source.count(b"\n", 0, offset) + 1 if end > offset else None,
                end_line=source.count(b"\n", 0, end - 1) + 1 if end > offset else None,
                line_truncated=0 < end < len(source) and source[end - 1] != 10,
                range_complete=end == stop)
    if end < len(source) and (end == 0 or source[end - 1] == 10):
        page["next_line"] = source.count(b"\n", 0, end) + 1
    if start_line is not None:
        page.update(requested_start_line=start_line, requested_line_count=line_count)
    return {"machine": machine, "workspace": workspace, "path": path, "context": current,
            "exists": raw is not None, "sha256": sha, "chunk_sha256": digest(source[offset:end]),
            "mtime_ns": metadata.st_mtime_ns if metadata else None, **page}


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


def snapshot(root: Path, path: str, *, max_bytes: int = MAX_SOURCE, allow_missing: bool = False) -> dict:
    """Read original bytes and their containing directory identities without effects."""
    parts = file_parts(path)
    if len(path.encode("utf-8")) > 1024:
        raise Fault("request_limit", "Use a relative path of at most 1024 UTF-8 bytes.")
    observed = context(root)
    if observed["git_status"] == "unavailable":
        raise Fault(observed.get("context_error", "context_unavailable"), "Workspace identity is unavailable.")
    ancestors, missing = [], []
    with file_parent(root, parts, identities=ancestors, missing=missing if allow_missing else None) as parent:
        raw, metadata = (None, None) if missing else file_bytes(parent, parts[-1], max_bytes=min(MAX_SOURCE, max_bytes))
        _ordinary_metadata(parent, parts[-1], metadata)
    # Check the last existing ancestor, not a nonexistent proposed directory.
    parent_context = context(root.joinpath(*parts[:len(ancestors) - 1]))
    if parent_context["git_status"] == "unavailable" or (
        parent_context["git_status"] == "observed" and
        parent_context["worktree"] != observed["worktree"]
    ):
        raise Fault("nested_workspace", "Configure the nested repository as its own workspace.")
    current = context(root)
    if (current["workspace_identity"], current["head_sha"]) != (observed["workspace_identity"], observed["head_sha"]):
        raise Fault("context_mismatch", "The workspace changed while preparing the file.")
    return {"root": root, "path": path, "parts": parts, "raw": raw, "metadata": metadata,
            "context": observed, "ancestors": ancestors,
            "missing_parents": ["/".join(parts[:depth]) for depth in range(len(ancestors), len(parts))]}


def _recheck_parent(source: dict, parent: int, config, machine: str, workspace: str) -> None:
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


def _recheck(source: dict, parent: int, config, machine: str, workspace: str) -> None:
    _recheck_parent(source, parent, config, machine, workspace)
    parts = source["parts"]
    raw, info = file_bytes(parent, parts[-1], max_bytes=MAX_SOURCE)
    if raw != source["raw"] or _metadata(info) != _metadata(source["metadata"]):
        raise Fault("content_mismatch", "The original bytes or file metadata changed before publication.")
    _ordinary_metadata(parent, parts[-1], info)


def publish_file(config, machine: str, workspace: str, source: dict, desired: bytes, temporary: str,
                 *, mode: int | None = None) -> dict:
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
                desired_mode = mode if mode is not None else stat.S_IMODE(info.st_mode) if info else 0o644
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
            actual, info = file_bytes(parent, name, max_bytes=MAX_SOURCE)
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



def publish_path(config, machine: str, workspace: str, source: dict, kind: str) -> dict:
    """Publish one declared directory or removal, with the same adjacent checks."""
    if kind not in {"mkdir", "remove"}:
        raise Fault("invalid_operation", "Select a prepared directory or file-removal effect.")
    effect = {"stage": "failed_no_effect", "result_sha256": None, "synced": False,
              "readback_verified": False, "error": None, "cleanup": "not_created",
              "result_identity": None, "result_mode": None}
    attempted = False
    try:
        with file_parent(source["root"], source["parts"]) as parent:
            name = source["parts"][-1]
            _recheck(source, parent, config, machine, workspace)
            if (source["raw"] is None) != (kind == "mkdir"):
                raise Fault("content_mismatch", "The prepared path presence no longer matches this effect.")
            attempted = True
            if kind == "mkdir":
                try:
                    os.mkdir(name, 0o755, dir_fd=parent)
                except FileExistsError:
                    attempted = False
                    raise Fault("destination_exists", "A planned directory appeared before publication.") from None
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    info = os.fstat(fd)
                    effect["result_identity"] = [info.st_dev, info.st_ino]
                    effect["result_mode"] = stat.S_IMODE(info.st_mode)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            else:
                os.unlink(name, dir_fd=parent)
            os.fsync(parent)
            effect["synced"] = True
            _recheck_parent(source, parent, config, machine, workspace)
            try:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if kind == "remove":
                if current is not None:
                    raise Fault("publication_raced", "The removed path reappeared before readback.")
                effect["result_sha256"] = ""
            elif (current is None or not stat.S_ISDIR(current.st_mode) or
                  [current.st_dev, current.st_ino] != effect["result_identity"] or
                  stat.S_IMODE(current.st_mode) != effect["result_mode"]):
                raise Fault("publication_raced", "The created directory changed before readback.")
            effect.update(stage="applied", readback_verified=True)
    except (OSError, Fault) as error:
        effect.update(stage="unknown" if attempted else "failed_no_effect",
                      error=error.code if isinstance(error, Fault) else type(error).__name__)
    return effect
