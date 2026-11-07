"""Bounded listing, search and Git inspection results."""

import hashlib
from fnmatch import fnmatchcase
import io
import itertools
import json
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING

from openmachine.foundation.files import MAX_SCAN_ENTRIES, excluded, file_parent, file_parts, text_page
from openmachine.foundation.sanitize import Fault, redact_slice, redaction_spans

from .files import MAX_SOURCE, read_text_file, reader_path, root_for
from .git import MAX_CAPTURE, capture, context
from .worktrees import list_worktrees, select

if TYPE_CHECKING:
    from openmachine.config import Settings

MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_MATCHES = 1000


def inspect(config: "Settings", machine: str, workspace: str, action: str,
            path: str = ".", offset: int = 0, limit: int = 100, *, query: str = "",
            staged: bool = False, expected_snapshot: str = "", path_glob: str = "", worktree: str = "") -> dict:
    if action == "worktrees":
        if path != "." or query or staged or path_glob:
            raise Fault("invalid_filter", "Worktree discovery accepts only pagination and snapshot inputs.")
        return list_worktrees(config, machine, workspace, offset, limit, expected_snapshot, worktree=worktree)
    if worktree:
        config = select(config, machine, workspace, worktree)
    root = root_for(config, machine, workspace)
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or excluded(relative):
        raise Fault("path_denied", "Use a relative, non-credential-bearing workspace path.")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise Fault("invalid_range", "Use a nonnegative offset and a limit from 1 to 200.")
    patterns = _glob(path_glob)
    if patterns and action not in {"files", "search"}:
        raise Fault("invalid_filter", "Path filters apply only to file discovery and literal search.")
    if patterns and action == "files":
        candidates, facts, fingerprint = _candidates(root, relative, patterns)
        if offset > len(candidates):
            raise Fault("invalid_range", "The offset exceeds the captured file set.")
        entries = [{"name": str(path), "directory": False, "symlink": False} for path, _ in candidates]
        return {"machine": machine, "workspace": workspace, "cwd": str(root), "path": str(root / relative),
                **facts, "path_glob": path_glob, "entries": entries[offset:offset + limit], "offset": offset,
                "next_offset": offset + limit if offset + limit < len(entries) else None,
                "has_more": offset + limit < len(entries), "snapshot": _snapshot(fingerprint, expected_snapshot),
                "ordering": "workspace-relative captured paths", "credential_paths_excluded": True}
    if action in ("search", "status", "diff"):
        if action == "search":
            return _search(root, machine, workspace, relative, query, offset, limit, expected_snapshot, patterns)
        return _inspect_git(root, machine, workspace, relative, action, staged, offset, limit, expected_snapshot)
    source = (root / relative).resolve(strict=True)
    if not source.is_relative_to(root) or excluded(source.relative_to(root)):
        raise Fault("path_denied", "The resolved path is outside the reader scope.")
    identity = {"machine": machine, "workspace": workspace, "path": str(source), "cwd": str(root)}
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
        return {**identity, "entries": items, "offset": offset, "next_offset": offset + limit if len(page) > limit else None,
                "mtime_ns": before.st_mtime_ns, "credential_paths_excluded": True,
                "ordering": "filesystem; recheck mtime when paging"}
    if action != "read":
        raise Fault("unsupported_operation", "Use files, read, search, status, diff, or worktrees.")
    page = read_text_file(config, machine, workspace, str(relative), offset)
    return {**page, **identity, "size": page["size_bytes"], "source_changed": False}


def _snapshot(data: bytes, expected: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    if expected and expected != digest:
        raise Fault("source_changed", "The inspection changed between pages; restart at offset zero.")
    return digest


def _inspect_git(root: Path, machine: str, workspace: str, relative: Path, action: str,
                 staged: bool, offset: int, limit: int, expected: str) -> dict:
    if not reader_path(root, relative):
        raise Fault("path_denied", "The selected path is outside the non-credential reader scope.")
    observed = context(root)
    if observed["git_status"] != "observed":
        raise Fault("git_unavailable", "This workspace is not a readable Git worktree.")
    # Clean/process filters can run even with --no-ext-diff and --no-textconv.
    names, code, clipped = capture(root, ["config", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process)$"])
    if clipped or code not in (0, 1):
        raise Fault("git_config_unavailable", "Git filter settings could not be inspected safely.")
    filters = sorted({os.fsdecode(name).rsplit(".", 1)[0] for name in names.split(b"\0") if name})
    flags = [item for name in filters for item in ("-c", f"{name}.clean=", "-c", f"{name}.process=", "-c", f"{name}.required=false")]
    warnings = ["External Git filters are disabled; this is a raw worktree comparison."] if filters else []
    skipped = 0
    if action == "status":
        data, code, clipped = capture(root, [*flags, "status", "--porcelain=v1", "-z", "--no-renames",
                                         "--untracked-files=all", "--ignore-submodules=dirty", "--", str(relative)])
        entries = []
        for item in data.split(b"\0")[:-1]:
            name = os.fsdecode(item[3:])
            path = Path(observed["worktree"]) / name
            if not path.is_relative_to(root) or not reader_path(root, path.relative_to(root)):
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
        listed, code, clipped = capture(root, [*flags, "diff", "--name-only", "-z", "--no-renames",
                                           "--no-ext-diff", "--no-textconv", "--relative", "--ignore-submodules=dirty",
                                           *(["--cached"] if staged else []), "--", str(relative)])
        if code and not clipped:
            raise Fault("git_failed", "Git could not enumerate the selected tracked files.")
        candidates = sorted(set(os.fsdecode(name) for name in listed.split(b"\0")[:-1]))
        allowed = [name for name in candidates if reader_path(root, Path(name))]
        skipped = len(candidates) - len(allowed)
        limited = clipped or len(allowed) > MAX_SCAN_ENTRIES
        allowed = allowed[:MAX_SCAN_ENTRIES]
        data, code, clipped = capture(root, [*flags, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                                         "--no-color", "--relative", "--submodule=short", "--ignore-submodules=dirty",
                                         *(["--cached"] if staged else []), "--", *allowed]) if allowed else (b"", 0, False)
        if code and not clipped:
            raise Fault("git_failed", "Git diff failed; do not interpret this as an empty result.")
        clipped = clipped or limited
        if offset > len(data):
            raise Fault("invalid_range", "The byte offset is beyond this diff; narrow the path if truncated.")
        payload = {**text_page(data, offset, errors="replace"), "staged": staged, "captured_bytes": len(data)}
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


def _glob(value: str) -> tuple[str, ...]:
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > 1024:
            raise ValueError
    except (UnicodeError, ValueError) as error:
        raise Fault("invalid_filter", "Use a bounded UTF-8 relative path pattern.") from error
    if not value:
        return ()
    parts = value.split("/")
    if ("\\" in value or "**" in value or any(ord(c) < 32 for c in value) or
        any(part in {"", ".", ".."} for part in parts)):
        raise Fault("invalid_filter", "Use relative path segments with *, ? or character classes; ** is unsupported.")
    return tuple(parts)


def _candidates(root: Path, relative: Path, patterns: tuple[str, ...]) -> tuple[list, dict, bytes]:
    """Capture one finite file set; only the explicit filter enables Git discovery."""
    if not reader_path(root, relative):
        raise Fault("path_denied", "Discovery must stay inside the non-credential reader scope.")
    observed = context(root) if patterns else None
    if observed is not None and observed["git_status"] == "unavailable":
        raise Fault("git_unavailable", "Workspace identity is unavailable; no traversal fallback was used.")
    git_mode = observed is not None and observed["git_status"] == "observed"
    warnings = {"excluded": 0, "symlink": 0, "binary": 0, "unreadable": 0, "oversized": 0,
                "nested_repository": 0, "unsupported_path": 0}
    digest = hashlib.sha256(json.dumps([str(root), str(relative), patterns, observed],
                                      sort_keys=True, ensure_ascii=True).encode())
    clipped = changed = False
    if git_mode:
        raw, code, clipped = capture(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", str(relative)])
        if code and not clipped:
            raise Fault("git_failed", "Git file enumeration failed; no traversal fallback was used.")
        records = raw.split(b"\0", MAX_SCAN_ENTRIES + 1)
        clipped |= len(records) > MAX_SCAN_ENTRIES + 1 or bool(records[-1])
        names = list(dict.fromkeys(os.fsdecode(value) for value in records[:-1][:MAX_SCAN_ENTRIES] if value))
        pending = [Path(name) for name in reversed(names)]
        digest.update(raw)
    else:
        pending = [relative]
    files, seen = [], 0
    while pending and seen < MAX_SCAN_ENTRIES:
        path = pending.pop()
        seen += 1
        try:
            if len(os.fsencode(str(path))) > 1024:
                warnings["unsupported_path"] += 1
                continue
            # Unsupported source path encodings are skipped, never lossy edit names.
            try:
                str(path).encode("utf-8")
            except UnicodeError:
                warnings["unsupported_path"] += 1
                continue
            parts = () if path == Path(".") else file_parts(str(path))
            if not reader_path(root, path):
                warnings["excluded"] += 1
                continue
            if patterns and any(os.path.lexists(root / parent / ".git") or
                    ((root / parent / "HEAD").exists() and (root / parent / "objects").is_dir())
                    for parent in path.parents if parent != Path(".")):
                warnings["nested_repository"] += 1
                continue
            with file_parent(root, parts or ("unused",)) as parent:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False) if parts else os.fstat(parent)
            digest.update(os.fsencode(str(path)))
            digest.update(repr((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)).encode())
            if stat.S_ISLNK(info.st_mode):
                warnings["symlink"] += 1
            elif stat.S_ISDIR(info.st_mode):
                if patterns and path != Path(".") and os.path.lexists(root / path / ".git"):
                    warnings["nested_repository"] += 1
                    continue
                if git_mode:
                    warnings["nested_repository"] += 1
                    continue
                room = max(0, MAX_SCAN_ENTRIES - seen - len(pending))
                with file_parent(root, (*parts, "unused")) as directory:
                    before = os.fstat(directory)
                    with os.scandir(directory) as entries:
                        names = [entry.name for entry in itertools.islice(entries, room + 1)]
                    after = os.fstat(directory)
                changed |= (before.st_ino, before.st_mtime_ns) != (after.st_ino, after.st_mtime_ns)
                clipped |= len(names) > room
                pending.extend(path / name for name in sorted(names[:room], reverse=True))
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                warnings["unreadable"] += 1
            elif not patterns or (len(parts) == len(patterns) and
                                  all(fnmatchcase(part, pattern) for part, pattern in zip(parts, patterns, strict=True))):
                files.append((path, info))
        except Fault as error:
            if error.code != "path_denied":
                raise
            warnings["excluded"] += 1
        except OSError:
            warnings["unreadable"] += 1
    clipped |= bool(pending)
    if patterns:
        files.sort(key=lambda item: os.fsencode(str(item[0])))
        after = context(root)
        if (after["workspace_identity"], after["head_sha"]) != (observed["workspace_identity"], observed["head_sha"]):
            raise Fault("source_changed", "The workspace changed during discovery.")
    facts = {"discovery": "git" if git_mode else "filesystem", "scanned_entries": seen,
             "truncated": clipped, "source_changed": changed, "skipped": warnings,
             "complete": not (clipped or changed or any(warnings.values()))}
    if observed is not None:
        facts["context"] = observed
    return files, facts, digest.digest()


def _search(root: Path, machine: str, workspace: str, relative: Path, query: str,
            offset: int, limit: int, expected: str, patterns: tuple[str, ...]) -> dict:
    if not isinstance(query, str) or not query or len(query) > 256 or any(c in query for c in "\0\n\r"):
        raise Fault("invalid_query", "Supply a literal, single-line query of 1-256 characters.")
    candidates, facts, fingerprint = _candidates(root, relative, patterns)
    warnings = facts["skipped"]
    try:
        query_bytes = query.encode("utf-8")
    except UnicodeError as error:
        raise Fault("invalid_query", "Supply valid UTF-8 query text.") from error
    digest = hashlib.sha256(fingerprint + query_bytes)
    matches, total = [], 0
    for path, captured in candidates:
        if total >= MAX_SCAN_BYTES or len(matches) >= MAX_MATCHES:
            facts["truncated"] = True
            break
        parts = file_parts(str(path))
        try:
            with file_parent(root, parts) as parent:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        warnings["unreadable"] += 1
                        continue
                    budget = min(MAX_SOURCE if patterns else MAX_CAPTURE, MAX_SCAN_BYTES - total)
                    content = stream.read(budget)
                    after = os.fstat(stream.fileno())
            total += len(content)
            identities = {(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                          for info in (captured, before, after)}
            facts["source_changed"] |= len(identities) != 1
            digest.update(os.fsencode(str(path)))
            digest.update(content)
            partial = len(content) < after.st_size
            if partial:
                warnings["oversized"] += 1
            if b"\0" in content:
                warnings["binary"] += 1
                continue
            # A clipped final line is not enough context to expose a safe snippet.
            visible = content[:content.rfind(b"\n") + 1] if partial else content
            text = visible.decode("utf-8", errors="replace")
            spans = redaction_spans(text)
            position = 0
            with io.StringIO(text) as lines:
                for number, line in enumerate(lines, 1):
                    value = line.removesuffix("\n")
                    column = value.find(query)
                    if column >= 0:
                        begin = max(0, column - 80)
                        end = min(len(value), begin + 320)
                        clean = redact_slice(text, position + begin, position + end, spans=spans)
                        matches.append({"path": str(path), "line": number, "column": column + 1,
                                        "text": clean, "content_redacted": clean != value[begin:end],
                                        "line_truncated": begin > 0 or end < len(value)})
                        if len(matches) == MAX_MATCHES:
                            facts["truncated"] = True
                            break
                    position += len(line)
        except OSError:
            warnings["unreadable"] += 1
    if patterns:
        after = context(root)
        if (after["workspace_identity"], after["head_sha"]) != (
            facts["context"]["workspace_identity"], facts["context"]["head_sha"]
        ):
            raise Fault("source_changed", "Workspace context changed during content search.")
    facts["truncated"] |= bool(warnings["oversized"])
    facts["complete"] = not (facts["truncated"] or facts["source_changed"] or any(warnings.values()))
    if offset > len(matches):
        raise Fault("invalid_range", "The offset is beyond captured search matches; narrow the path if truncated.")
    snapshot = _snapshot(digest.digest(), expected)
    return {"machine": machine, "workspace": workspace, "cwd": str(root), "path": str(root / relative),
            **facts, "matches": matches[offset:offset + limit], "offset": offset,
            "next_offset": offset + limit if offset + limit < len(matches) else None,
            "has_more": offset + limit < len(matches), "snapshot": snapshot, "scanned_bytes": total,
            "path_glob": "/".join(patterns), "credential_paths_excluded": True}
