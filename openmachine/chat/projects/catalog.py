"""Bounded local directory metadata and branch-independent target bindings."""

import hashlib
import itertools
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
from typing import TYPE_CHECKING

from openmachine.foundation.files import MAX_SCAN_ENTRIES, file_parent, file_parts
from openmachine.foundation.sanitize import Fault
from openmachine.workspace.files import root_for
from openmachine.workspace.git import context

if TYPE_CHECKING:
    from openmachine.config import Settings

MAX_PATH = 1024
MAX_DEPTH = 32
MAX_PAGE = 50
MAX_RESULT = 65536


def capabilities() -> dict:
    return {"protocol": 2, "actions": ["root", "list", "inspect"], "transports": ["local"],
            "max_entries": MAX_SCAN_ENTRIES, "max_page": MAX_PAGE, "max_path_bytes": MAX_PATH,
            "max_depth": MAX_DEPTH, "result_bytes": MAX_RESULT}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _parts(path: str) -> tuple[str, ...]:
    parts = () if path == "." else file_parts(path)
    if len(path.encode("utf-8")) > MAX_PATH or len(parts) > MAX_DEPTH:
        raise Fault("catalog_limit", "Use a relative path within the advertised byte and depth limits.")
    return parts


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _git(directory: Path) -> tuple[str, list]:
    observed = context(directory)
    if observed["git_status"] == "unavailable":
        raise Fault("project_unavailable", "The repository identity could not be verified.")
    if observed["git_status"] == "not_a_worktree":
        return "folder", []
    root = Path(observed["worktree"])
    marker = (root / ".git").lstat()
    if not (stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode) and marker.st_nlink == 1):
        raise Fault("project_unavailable", "Repository control links and special files are not selectable.")
    paths = [Path(observed[key]) for key in ("worktree", "git_dir", "common_dir")]
    identities = []
    for path in paths:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise Fault("project_unavailable", "Repository directories must remain ordinary directories.")
        identities.append([str(path), *_identity(info)])
    kind = "folder"
    if directory == root:
        kind = "git_repo" if paths[1] == paths[2] else "git_worktree"
    # Branch, HEAD, index, and ordinary file/directory timestamps are deliberately absent.
    return kind, [identities, _identity(marker)]


def _target(config: "Settings", machine: str, workspace: str, path: str) -> dict:
    parts = _parts(path)
    observations = []
    try:
        for _ in range(2):
            root = root_for(config, machine, workspace)
            ancestors = []
            with file_parent(root, (*parts, "unused"), identities=ancestors):
                kind, git = _git(root.joinpath(*parts))
                attached = []
                with file_parent(root, (*parts, "unused"), identities=attached):
                    if attached != ancestors or root_for(config, machine, workspace) != root:
                        raise Fault("project_changed", "The directory changed during observation; select it again.")
            observations.append([1, machine, workspace, config.workspaces[workspace].root if workspace in config.workspaces else workspace,
                                 str(root), path, ancestors, git])
    except (OSError, RuntimeError) as error:
        raise Fault("project_unavailable", "The selected directory is missing, linked or unreadable.") from error
    if observations[0] != observations[1]:
        raise Fault("project_changed", "The project changed during observation; select it again.")
    return {"machine": machine, "workspace_name": workspace, "relative_path": path,
            "name": parts[-1] if parts else Path(workspace).name or workspace,
            "kind": "workspace" if not parts and workspace in config.workspaces else kind,
            "target_id": _digest(observations[1])}


def desktop(machine: str) -> dict:
    """The account's own Desktop, from the account database; never $HOME, Finder or a fallback."""
    if sys.platform != "darwin":
        raise Fault("desktop_unsupported", "Desktop-first discovery is available on macOS; navigate explicitly.")
    path = Path(pwd.getpwuid(os.getuid()).pw_dir) / "Desktop"
    try:
        info = os.lstat(path)
        canonical = path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise Fault("desktop_missing", "This account has no Desktop folder; navigate to an explicit directory.") from error
    except (OSError, RuntimeError) as error:
        raise Fault("desktop_access_denied", "The OS denied access to the Desktop folder.") from error
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or canonical != path):
        raise Fault("desktop_redirected", "The Desktop folder is linked or redirected; navigate to it explicitly.")
    return {"protocol": 2, "root": {"machine": machine, "workspace_name": str(path), "relative_path": ".",
                                    "name": "Desktop"}}


def _directories(config: "Settings", machine: str, workspace: str, path: str) -> tuple[list[str], str]:
    """Direct child directories by lstat only: no recursion, link following, content or Git probe."""
    parts = _parts(path)
    rows, identities = [], []
    try:
        root = root_for(config, machine, workspace)
        with file_parent(root, (*parts, "unused"), identities=identities) as directory:
            before = os.fstat(directory)
            with os.scandir(directory) as stream:
                entries = list(itertools.islice(stream, MAX_SCAN_ENTRIES + 1))
            if len(entries) > MAX_SCAN_ENTRIES:
                raise Fault("catalog_limit", "This directory exceeds the bounded catalog; select a narrower path.")
            for entry in entries:
                try:
                    relative = "/".join((*parts, entry.name))
                    _parts(relative)
                except Fault as error:
                    if error.code not in {"path_denied", "catalog_limit"}:
                        raise
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    rows.append((relative, _identity(info)))
            after = os.fstat(directory)
        if (_identity(before), before.st_mtime_ns, before.st_ctime_ns) != (
                _identity(after), after.st_mtime_ns, after.st_ctime_ns):
            raise Fault("source_changed", "The directory changed while listing; restart at offset zero.")
    except PermissionError as error:
        raise Fault("directory_access_denied", "The OS denied listing this directory.") from error
    except OSError as error:
        raise Fault("project_unavailable", "Directory metadata could not be read safely.") from error
    if root_for(config, machine, workspace) != root:
        raise Fault("project_changed", "The listing root changed during observation.")
    rows.sort()
    return [name for name, _ in rows], _digest([2, machine, str(root), path, identities, rows,
                                                before.st_mtime_ns, before.st_ctime_ns])


def inspect(config: "Settings", machine: str, workspace_name: str = "", action: str = "list",
            path: str = ".", offset: int = 0, limit: int = MAX_PAGE,
            expected_snapshot: str = "", expected_target_id: str = "") -> dict:
    """Resolve the Desktop root, list direct child directories, or inspect/revalidate one target."""
    if config.machine(machine).transport != "local":
        raise Fault("unsupported_operation", "Project catalog protocol 2 supports local machines only.")
    if (not isinstance(action, str) or action not in {"root", "list", "inspect"} or not isinstance(workspace_name, str)
            or not all(isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{64})?", value)
                       for value in (expected_snapshot, expected_target_id))):
        raise Fault("invalid_project_request", "Use a catalog action and complete original identity hashes.")
    _parts(path)
    if (type(offset) is not int or not 0 <= offset <= MAX_SCAN_ENTRIES
            or type(limit) is not int or not 1 <= limit <= MAX_PAGE):
        raise Fault("invalid_range", "Use a bounded nonnegative offset and page size from 1 to 50.")
    if action == "root":
        if workspace_name or path != "." or offset or limit != MAX_PAGE or expected_snapshot or expected_target_id:
            raise Fault("invalid_project_request", "Root discovery takes no directory, pagination or identity inputs.")
        result = desktop(machine)
    elif action == "inspect":
        if not workspace_name or offset or limit != MAX_PAGE or expected_snapshot:
            raise Fault("invalid_project_request", "Inspect requires a workspace and no pagination inputs.")
        target = _target(config, machine, workspace_name, path)
        if expected_target_id and expected_target_id != target["target_id"]:
            raise Fault("project_changed", "The saved target no longer matches; explicit reselection is required.")
        result = {"protocol": 2, "project": target}
    else:
        if not workspace_name or expected_target_id or (offset and not expected_snapshot):
            raise Fault("invalid_project_request", "List one explicit directory; use action root for the Desktop. "
                        "Echo the snapshot on continuation.")
        names, snapshot = _directories(config, machine, workspace_name, path)
        if expected_snapshot and expected_snapshot != snapshot:
            raise Fault("source_changed", "The directory changed between pages; restart at offset zero.")
        if offset > len(names):
            raise Fault("invalid_range", "The offset exceeds this directory listing.")
        page = names[offset:offset + limit]
        more = offset + len(page) < len(names)
        result = {"protocol": 2, "directories": [{"relative_path": name, "name": name.rsplit("/", 1)[-1]} for name in page],
                  "snapshot": snapshot, "offset": offset,
                  "next_offset": offset + len(page) if more else None, "has_more": more}
    if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_RESULT:
        raise Fault("catalog_limit", "The metadata response exceeds its byte limit; use a smaller page.")
    return result
