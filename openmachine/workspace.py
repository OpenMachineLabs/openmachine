"""Small, bounded readers; configured paths are routing, not a sandbox."""

import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

from openmachine.files import MAX_SCAN_ENTRIES, MAX_TEXT_FILE, excluded, file_bytes, file_parent, file_parts, text_page
from openmachine.sanitize import Fault, redact
from openmachine.state import lease, private_directory

if TYPE_CHECKING:
    from openmachine.config import Settings

MAX_READ = 65536
MAX_CAPTURE = 1024 * 1024
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_MATCHES = 1000


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


def context(cwd: Path) -> dict:
    """Capture Git identity without changing the worktree or selecting an environment."""
    result = {"cwd": str(cwd), "worktree": str(cwd), "head_sha": None, "branch": None,
              "agents_files": [str(p / "AGENTS.md") for p in reversed((cwd, *cwd.parents))
                               if (p / "AGENTS.md").is_file()]}
    try:
        probe = subprocess.run(["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=5, env=_git_environment())
        if probe.returncode:
            result["git_status"] = "not_a_worktree"
            return result
        result["worktree"] = str(Path(probe.stdout.strip()).resolve())
        for key, args in (("head_sha", ["rev-parse", "--verify", "HEAD"]),
                          ("branch", ["symbolic-ref", "--quiet", "--short", "HEAD"])):
            item = subprocess.run(["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(cwd), *args], capture_output=True,
                                  text=True, timeout=5, env=_git_environment())
            result[key] = item.stdout.strip() if item.returncode == 0 else None
        result["git_status"] = "observed"
    except (OSError, subprocess.TimeoutExpired):
        result["git_status"] = "unavailable"
    return result


def inspect(config: "Settings", machine: str, workspace: str, action: str,
            path: str = ".", offset: int = 0, limit: int = 100, *, query: str = "",
            staged: bool = False, expected_snapshot: str = "") -> dict:
    if config.machine(machine).transport == "ssh":
        from integrations.ssh import call
        return call(config, None, machine, "inspect_workspace", workspace=workspace, action=action, path=path,
                    offset=offset, limit=limit, query=query, staged=staged, expected_snapshot=expected_snapshot)
    root = root_for(config, machine, workspace)
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or excluded(relative):
        raise Fault("path_denied", "Use a relative, non-credential-bearing workspace path.")
    if action in ("search", "status", "diff"):
        if offset < 0 or not 1 <= limit <= 200:
            raise Fault("invalid_range", "Use a nonnegative offset and a limit from 1 to 200.")
        if action == "search":
            return _search(root, machine, workspace, relative, query, offset, limit, expected_snapshot)
        return _inspect_git(root, machine, workspace, relative, action, staged, offset, limit, expected_snapshot)
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
        raise Fault("unsupported_operation", "Use files, read, search, status, or diff.")
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


def _git_environment() -> dict:
    routing = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
    return {key: value for key, value in os.environ.items() if key not in routing}


def _git(root: Path, args: list[str]) -> tuple[bytes, int, bool]:
    """Capture bounded output without a shell, pager, optional locks or fsmonitor."""
    command = ["git", "--no-optional-locks", "--no-pager", "--literal-pathspecs",
               "-c", "core.fsmonitor=false", "-C", str(root), *args]
    output, diagnostic = bytearray(), bytearray()
    clipped = False
    try:
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=_git_environment()) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, output)
                    selector.register(process.stderr, selectors.EVENT_READ, diagnostic)
                    deadline = time.monotonic() + 5
                    while selector.get_map():
                        if time.monotonic() >= deadline:
                            raise Fault("git_timeout", "Git inspection exceeded five seconds; narrow the path.")
                        for key, _ in selector.select(0.05):
                            chunk = os.read(key.fd, MAX_READ)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            budget = MAX_CAPTURE if key.data is output else 4096
                            room = budget - len(key.data)
                            key.data.extend(chunk[:room])
                            if len(chunk) > room:
                                if key.data is diagnostic:
                                    raise Fault("git_failed", "Git diagnostics exceeded the safe limit; inspect locally.")
                                clipped = True
                                break
                        if clipped:
                            break
                if clipped:
                    process.kill()
                code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    except FileNotFoundError as error:
        raise Fault("missing_dependency", "Install Git to inspect a Git workspace.") from error
    except subprocess.TimeoutExpired as error:
        raise Fault("git_timeout", "Git did not finish within the inspection budget.") from error
    return bytes(output), code, clipped


def _reader_path(root: Path, relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts or excluded(relative):
        return False
    try:
        resolved = (root / relative).resolve()
        return resolved.is_relative_to(root) and not excluded(resolved.relative_to(root))
    except (OSError, RuntimeError):
        return False


def _snapshot(data: bytes, expected: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    if expected and expected != digest:
        raise Fault("source_changed", "The inspection changed between pages; restart at offset zero.")
    return digest


def _inspect_git(root: Path, machine: str, workspace: str, relative: Path, action: str,
                 staged: bool, offset: int, limit: int, expected: str) -> dict:
    if not _reader_path(root, relative):
        raise Fault("path_denied", "The selected path is outside the non-credential reader scope.")
    observed = context(root)
    if observed["git_status"] != "observed":
        raise Fault("git_unavailable", "This workspace is not a readable Git worktree.")
    # Clean/process filters can run even with --no-ext-diff and --no-textconv.
    names, code, clipped = _git(root, ["config", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process)$"])
    if clipped or code not in (0, 1):
        raise Fault("git_config_unavailable", "Git filter settings could not be inspected safely.")
    filters = sorted({os.fsdecode(name).rsplit(".", 1)[0] for name in names.split(b"\0") if name})
    flags = [item for name in filters for item in ("-c", f"{name}.clean=", "-c", f"{name}.process=", "-c", f"{name}.required=false")]
    warnings = ["External Git filters are disabled; this is a raw worktree comparison."] if filters else []
    skipped = 0
    if action == "status":
        data, code, clipped = _git(root, [*flags, "status", "--porcelain=v1", "-z", "--no-renames",
                                         "--untracked-files=all", "--ignore-submodules=dirty", "--", str(relative)])
        entries = []
        for item in data.split(b"\0")[:-1]:
            name = os.fsdecode(item[3:])
            path = Path(observed["worktree"]) / name
            if not path.is_relative_to(root) or not _reader_path(root, path.relative_to(root)):
                skipped += 1
                continue
            entries.append({"path": str(path.relative_to(root)), "index": chr(item[0]),
                            "worktree": chr(item[1]), "untracked": item[:2] == b"??"})
        payload = {"entries": entries[offset:offset + limit], "offset": offset,
                   "next_offset": offset + limit if offset + limit < len(entries) else None,
                   "has_more": offset + limit < len(entries)}
        if offset > len(entries):
            raise Fault("invalid_range", "The offset is beyond the current status entries.")
        warnings.append("Submodule contents are not inspected; renames are represented as add/delete.")
    else:
        listed, code, clipped = _git(root, [*flags, "diff", "--name-only", "-z", "--no-renames",
                                           "--no-ext-diff", "--no-textconv", "--relative", "--ignore-submodules=dirty",
                                           *(["--cached"] if staged else []), "--", str(relative)])
        if code and not clipped:
            raise Fault("git_failed", "Git could not enumerate the selected tracked files.")
        candidates = sorted(set(os.fsdecode(name) for name in listed.split(b"\0")[:-1]))
        allowed = [name for name in candidates if _reader_path(root, Path(name))]
        skipped = len(candidates) - len(allowed)
        limited = clipped or len(allowed) > MAX_SCAN_ENTRIES
        allowed = allowed[:MAX_SCAN_ENTRIES]
        data, code, clipped = _git(root, [*flags, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                                         "--no-color", "--relative", "--submodule=short", "--ignore-submodules=dirty",
                                         *(["--cached"] if staged else []), "--", *allowed]) if allowed else (b"", 0, False)
        if code and not clipped:
            raise Fault("git_failed", "Git diff failed; do not interpret this as an empty result.")
        clipped = clipped or limited
        if offset > len(data):
            raise Fault("invalid_range", "The byte offset is beyond this diff; narrow the path if truncated.")
        chunk = data[offset:offset + MAX_READ]
        payload = {"text": redact(chunk.decode("utf-8", errors="replace")), "offset": offset,
                   "next_offset": offset + len(chunk), "has_more": offset + len(chunk) < len(data),
                   "staged": staged, "captured_bytes": len(data)}
        warnings.append("Untracked files require status and individual reads; submodule contents are not inspected.")
    if code and not clipped:
        raise Fault("git_failed", "Git inspection failed; do not interpret this as an empty result.")
    after = context(root)
    if (observed["head_sha"], observed["branch"]) != (after["head_sha"], after["branch"]):
        raise Fault("source_changed", "Git identity changed during inspection; read the workspace again.")
    digest = _snapshot(json.dumps([observed, str(relative), action, staged], sort_keys=True).encode() + data, expected)
    return {"machine": machine, "workspace": workspace, "context": observed, **payload,
            "snapshot": digest, "truncated": clipped, "skipped_paths": skipped,
            "warnings": warnings, "credential_paths_excluded": True}


def _search(root: Path, machine: str, workspace: str, relative: Path, query: str,
            offset: int, limit: int, expected: str) -> dict:
    if not query or len(query) > 256 or "\0" in query or "\n" in query:
        raise Fault("invalid_query", "Supply a literal, single-line query of 1-256 characters.")
    if not _reader_path(root, relative):
        raise Fault("path_denied", "Search must stay inside the non-credential reader scope.")
    source = (root / relative).resolve(strict=True)
    pending = [source]
    matches, warnings = [], {"excluded": 0, "symlink": 0, "binary": 0, "unreadable": 0, "oversized": 0}
    seen = total = 0
    truncated = changed = False
    digest = hashlib.sha256(f"{root}\0{source}\0{query}".encode())
    while pending and seen < MAX_SCAN_ENTRIES and total < MAX_SCAN_BYTES and len(matches) < MAX_MATCHES:
        path = pending.pop()
        seen += 1
        rel = path.relative_to(root)
        if not _reader_path(root, rel):
            warnings["excluded"] += 1
            continue
        try:
            info = path.lstat()
            digest.update(f"{rel}:{info.st_size}:{info.st_mtime_ns}".encode())
            if stat.S_ISLNK(info.st_mode):
                warnings["symlink"] += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                room = max(0, MAX_SCAN_ENTRIES - seen - len(pending))
                with os.scandir(path) as entries:
                    names = [entry.name for entry in itertools.islice(entries, room + 1)]
                if len(names) > room:
                    truncated = True
                pending.extend(path / name for name in sorted(names[:room], reverse=True))
                continue
            if not stat.S_ISREG(info.st_mode):
                warnings["unreadable"] += 1
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    warnings["unreadable"] += 1
                    continue
                budget = min(MAX_CAPTURE, MAX_SCAN_BYTES - total)
                content = stream.read(budget)
                after = os.fstat(stream.fileno())
            total += len(content)
            changed |= (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            digest.update(content)
            if len(content) < after.st_size:
                warnings["oversized"] += 1
            if b"\0" in content:
                warnings["binary"] += 1
                continue
            for line, text in enumerate(content.decode("utf-8", errors="replace").split("\n"), 1):
                column = text.find(query)
                if column >= 0:
                    begin = max(0, column - 80)
                    matches.append({"path": str(rel), "line": line, "column": column + 1,
                                    "text": redact(text[begin:begin + 320]), "line_truncated": begin > 0 or len(text) > 320})
                    if len(matches) == MAX_MATCHES:
                        truncated = True
                        break
        except OSError:
            warnings["unreadable"] += 1
    truncated |= bool(pending) or bool(warnings["oversized"])
    if offset > len(matches):
        raise Fault("invalid_range", "The offset is beyond captured search matches; narrow the path if truncated.")
    snapshot = _snapshot(digest.digest(), expected)
    return {"machine": machine, "workspace": workspace, "cwd": str(root), "path": str(source),
            "matches": matches[offset:offset + limit], "offset": offset,
            "next_offset": offset + limit if offset + limit < len(matches) else None,
            "has_more": offset + limit < len(matches), "snapshot": snapshot,
            "truncated": truncated, "source_changed": changed, "scanned_entries": seen, "scanned_bytes": total,
            "skipped": warnings, "credential_paths_excluded": True,
            "complete": not (truncated or changed or any(warnings.values()))}


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
