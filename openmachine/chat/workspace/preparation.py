"""Exact request preparation and bounded proposed diffs; no writes or receipt allocation."""

import io
import itertools
import json
from pathlib import Path
import stat
from openmachine.foundation.files import MAX_TEXT_FILE, TEXT_PAGE, file_parts
from openmachine.foundation.sanitize import Fault, redact
from .files import MAX_SOURCE, digest, root_for, snapshot
from . import patch
from . import change_schema as schema


def _text(value: str) -> bytes:
    try:
        if not isinstance(value, str) or "\0" in value:
            raise ValueError
        raw = value.encode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise Fault("invalid_content", "Supply valid UTF-8 scalar text without NUL.") from error
    if len(raw) > MAX_TEXT_FILE:
        raise Fault("invalid_content", "Text payloads remain limited to 128 KiB.")
    return raw


def _request(operation, path, payload, identity, head, sha, plan_sha) -> dict:
    path_bytes = _text(path)
    file_parts(path)
    if len(path_bytes) > 1024:
        raise Fault("request_limit", "Use a path of at most 1024 UTF-8 bytes.")
    if not (schema.is_hex(identity, 64) and schema.is_hex(head, 40, empty=True) and
            schema.is_hex(sha, 64, empty=operation == "write") and schema.is_hex(plan_sha, 64, empty=True)):
        raise Fault("invalid_precondition", "Supply the observed workspace identity, HEAD and complete source hash.")
    if operation == "write":
        _text(payload)
    else:
        if not isinstance(payload, list) or not 1 <= len(payload) <= schema.MAX_EDITS:
            raise Fault("invalid_edits", "Supply 1 through 64 exact replacements.")
        size = 0
        for edit in payload:
            if not isinstance(edit, dict) or set(edit) != {"old_text", "new_text"}:
                raise Fault("invalid_edits", "Each replacement needs exactly old_text and new_text.")
            old, new = _text(edit["old_text"]), _text(edit["new_text"])
            if not old or old == new:
                raise Fault("invalid_edits", "Old text must be nonempty and differ from new text.")
            size += len(old) + len(new)
            if size > MAX_TEXT_FILE:
                raise Fault("request_limit", "Combined replacement strings exceed 128 KiB.")
    request = {"operation": operation, "path": path, "payload": payload,
               "expected_workspace_identity": identity, "expected_head_sha": head,
               "expected_sha256": sha, "expected_plan_sha256": plan_sha}
    if len(json.dumps(request, ensure_ascii=True).encode()) > 1024 * 1024:
        raise Fault("request_limit", "The encoded request exceeds one MiB.")
    return request


def _replace(raw: bytes, edits: list[dict]) -> tuple[bytes, list[dict]]:
    spans = []
    for edit in edits:
        old, new = edit["old_text"].encode(), edit["new_text"].encode()
        start = raw.find(old)
        if start < 0:
            raise Fault("match_missing", "An exact old fragment is absent from the original source.")
        if raw.find(old, start + 1) >= 0:
            raise Fault("match_ambiguous", "An old fragment has multiple occurrences, including overlapping starts.")
        spans.append((start, start + len(old), new))
    spans.sort(key=lambda span: span[0])
    end = shift = 0
    chunks, ranges = [], []
    resulting_size = len(raw) + sum(len(new) - (stop - start) for start, stop, new in spans)
    if resulting_size > MAX_SOURCE:
        raise Fault("source_limit", "The resulting source exceeds 2 MiB.")
    for start, stop, new in spans:
        if start < end:
            raise Fault("match_overlap", "Replacement ranges overlap in the original source.")
        ranges.append({"old_start": start, "old_end": stop,
                       "new_start": start + shift, "new_end": start + shift + len(new)})
        chunks.extend((raw[end:start], new))
        shift += len(new) - (stop - start)
        end = stop
    chunks.append(raw[end:])
    return b"".join(chunks), ranges


def _context(source: dict) -> dict:
    value = source["context"]
    return {"root": str(source["root"]), **{key: value[key] for key in (
        "root_id", "worktree", "workspace_identity", "workspace_identity_version", "git_state", "head_sha")}}


def _prepare(config, machine, workspace, root: Path, request: dict) -> tuple[dict, bytes, dict]:
    source = snapshot(root, request["path"])
    observed = source["context"]
    if root_for(config, machine, workspace) != root or (
        observed["workspace_identity"] != request["expected_workspace_identity"] or
        (observed["head_sha"] or "") != request["expected_head_sha"]
    ):
        raise Fault("context_mismatch", "The workspace, branch or HEAD no longer matches the observation.")
    previous = source["raw"]
    if digest(previous) != request["expected_sha256"]:
        raise Fault("content_mismatch", "The complete original source no longer matches its observed hash.")
    if request["operation"] == "edit":
        if previous is None:
            raise Fault("source_missing", "Exact editing requires an existing file.")
        desired, ranges = _replace(previous, request["payload"])
    else:
        desired = request["payload"].encode()
        ranges = [{"old_start": 0, "old_end": len(previous or b""), "new_start": 0, "new_end": len(desired)}]
    mode = stat.S_IMODE(source["metadata"].st_mode) if source["metadata"] else None
    plan = {"context": _context(source), "path": request["path"], "before_sha256": digest(previous),
            "after_sha256": digest(desired), "before_mode": mode, "after_mode": mode if mode is not None else 0o644,
            "ancestors": [list(item) for item in source["ancestors"]], "ranges": ranges}
    if request["expected_plan_sha256"] and request["expected_plan_sha256"] != schema.fingerprint(plan):
        raise Fault("plan_mismatch", "The recomputed plan differs from the approved preview.")
    return source, desired, plan


def _diff(source: dict, desired: bytes | None, ranges: list[dict], destination: str | None = None) -> dict:
    """Render changed line windows directly from prepared spans, without fuzzy comparison."""
    before, after = source["raw"] or b"", desired or b""
    if before == after:
        return {"diff": "", "diff_omitted": False, "truncated": False, "content_redacted": False}
    originals = [before.decode(), after.decode()]
    safe = [redact(value) for value in originals]
    if any(a.count("\n") != b.count("\n") for a, b in zip(originals, safe)):
        return {"diff": "", "diff_omitted": True, "truncated": False, "content_redacted": True}
    windows = []
    for span in ranges:
        pair = []
        for raw, start_key, end_key in ((before, "old_start", "old_end"), (after, "new_start", "new_end")):
            start, end = span[start_key], span[end_key]
            first = raw.count(b"\n", 0, start)
            empty_tail = start == end == len(raw) and (not raw or raw.endswith(b"\n"))
            last = first if empty_tail else raw.count(b"\n", 0, max(start, end - 1)) + 1
            pair.extend((first, last))
        if windows and (pair[0] <= windows[-1][1] or pair[2] <= windows[-1][3]):
            windows[-1][1] = max(windows[-1][1], pair[1])
            windows[-1][3] = max(windows[-1][3], pair[3])
        else:
            windows.append(pair)
    def pieces(streams):
        positions = [0, 0]
        old = "/dev/null" if source["raw"] is None else "a/" + source["path"]
        new = "/dev/null" if desired is None else "b/" + (destination or source["path"])
        yield f"--- {old}\n+++ {new}\n"
        for a, b, c, d in windows:
            yield f"@@ -{a + 1 if b > a else a},{b - a} +{c + 1 if d > c else c},{d - c} @@\n"
            for index, (prefix, first, last) in enumerate((("-", a, b), ("+", c, d))):
                for _ in itertools.islice(streams[index], first - positions[index]):
                    pass
                for line in itertools.islice(streams[index], last - first):
                    yield prefix
                    yield line
                    if not line.endswith("\n"):
                        yield "\n\\ No newline at end of file\n"
                positions[index] = last

    result, truncated = bytearray(), False
    with io.StringIO(safe[0]) as old, io.StringIO(safe[1]) as new:
        for piece in pieces((old, new)):
            raw = piece.encode()
            room = TEXT_PAGE - len(result)
            result.extend(raw[:room])
            if len(raw) > room:
                truncated = True
                break
    return {"diff": result.decode("utf-8", errors="ignore"), "diff_omitted": False,
            "truncated": truncated, "content_redacted": safe != originals}


def _patch_request(text, preconditions, identity, head, create_parents, plan_sha):
    operations = patch.parse(text)
    touched = patch.paths(operations)
    if type(create_parents) is not bool or not (schema.is_hex(identity, 64) and schema.is_hex(head, 40, empty=True)
                                                and schema.is_hex(plan_sha, 64, empty=True)):
        raise Fault("invalid_precondition", "Supply the observed context, optional plan hash and a boolean parent choice.")
    if (not isinstance(preconditions, list) or len(preconditions) != len(touched) or
        any(not isinstance(item, dict) or set(item) != {"path", "expected_sha256"} or
            not isinstance(item["path"], str) or not schema.is_hex(item["expected_sha256"], 64, empty=True)
            for item in preconditions)):
        raise Fault("invalid_precondition", "Preconditions must exactly cover every unique touched path.")
    names = [item["path"] for item in preconditions]
    if len(set(names)) != len(names) or set(names) != set(touched):
        raise Fault("invalid_precondition", "Reject missing, duplicate or surplus path preconditions.")
    request = {"operation": "patch", "payload": text, "preconditions": sorted(preconditions, key=lambda item: item["path"]),
               "expected_workspace_identity": identity, "expected_head_sha": head,
               "expected_plan_sha256": plan_sha, "create_parents": create_parents}
    if len(json.dumps(request, ensure_ascii=True).encode()) > 1024 * 1024:
        raise Fault("request_limit", "The encoded patch request exceeds one MiB.")
    return request, operations


def _ancestry(source):
    return {"path": source["path"], "ancestors": [list(item) for item in source["ancestors"]],
            "missing_parents": list(source.get("missing_parents", []))}


def _prepare_patch(config, machine, workspace, root, request, operations):
    expected = {item["path"]: item["expected_sha256"] for item in request["preconditions"]}
    sources, desired, total = {}, {}, 0
    observed = None
    for path in patch.paths(operations):
        source = snapshot(root, path, max_bytes=min(MAX_SOURCE, schema.MAX_PREPARED - total), allow_missing=True)
        ctx = _context(source)
        if (root_for(config, machine, workspace) != root or ctx["workspace_identity"] != request["expected_workspace_identity"]
            or (ctx["head_sha"] or "") != request["expected_head_sha"] or observed is not None and ctx != observed):
            raise Fault("context_mismatch", "Every source must share the originally observed workspace and HEAD.")
        observed = ctx
        if digest(source["raw"]) != expected[path]:
            raise Fault("content_mismatch", "A complete original source no longer matches its precondition.")
        if len(source["missing_parents"]) > schema.MAX_PARENT_DEPTH:
            raise Fault("request_limit", "A destination may have at most eight missing parent levels.")
        if source["missing_parents"] and not request["create_parents"]:
            raise Fault("parent_missing", "Missing parents require explicit create_parents=true.")
        total += len(source["raw"] or b"")
        sources[path] = source
    parents, prepared_operations, modes = {}, [], {}
    for operation in operations:
        path, kind = operation["path"], operation["kind"]
        source = sources[path]
        result, ranges = patch.prepare(operation, source["raw"], min(MAX_SOURCE, schema.MAX_PREPARED - total))
        total += len(result or b"")
        mode = stat.S_IMODE(source["metadata"].st_mode) if source["metadata"] else 0o644
        item = {"kind": kind, "path": path, "ranges": ranges}
        destination = operation.get("destination", path)
        if kind == "move":
            if sources[destination]["raw"] is not None:
                raise Fault("destination_exists", "A move requires an absent destination.")
            item["destination"] = destination
            desired[path], modes[path] = None, None
        desired[destination] = result
        modes[destination] = mode if result is not None else None
        if kind in {"add", "move"}:
            for parent in sources[destination]["missing_parents"]:
                if parent not in parents:
                    if len(parents) >= schema.MAX_PARENTS:
                        raise Fault("request_limit", "A patch may create at most sixteen parent directories.")
                    parent_source = snapshot(root, parent, max_bytes=0, allow_missing=True)
                    if parent_source["raw"] is not None or _context(parent_source) != observed:
                        raise Fault("context_mismatch", "A proposed parent changed during preflight.")
                    parents[parent] = parent_source
        prepared_operations.append(item)
    entries = []
    for path, source in sources.items():
        before, after = source["raw"], desired[path]
        entries.append(_ancestry(source) | {"before_sha256": digest(before), "after_sha256": digest(after),
            "before_size": len(before or b""), "after_size": len(after or b""),
            "before_mode": stat.S_IMODE(source["metadata"].st_mode) if source["metadata"] else None,
            "after_mode": modes[path]})
    plan = {"context": observed, "operations": prepared_operations, "files": entries,
            "parents": [_ancestry(source) for source in parents.values()]}
    if request["expected_plan_sha256"] and request["expected_plan_sha256"] != schema.fingerprint(plan):
        raise Fault("plan_mismatch", "The recomputed patch differs from the approved preview.")
    return plan, sources | parents, desired


def _prepared(config, machine, workspace, root, request, operations):
    if operations is not None:
        return _prepare_patch(config, machine, workspace, root, request, operations)
    source, desired, plan = _prepare(config, machine, workspace, root, request)
    return plan, {source["path"]: source}, {source["path"]: desired}


def _display(plan, sources, desired):
    if "operations" not in plan:
        path = plan["path"]
        return _diff(sources[path], desired[path], plan["ranges"])
    result = {"diff": "", "truncated": False, "diff_omitted": False, "content_redacted": False}
    used = 0
    for operation in plan["operations"]:
        path, destination = operation["path"], operation.get("destination", operation["path"])
        view = _diff(sources[path], desired[destination], operation["ranges"], destination)
        raw = view["diff"].encode()
        room = TEXT_PAGE - used
        piece = raw[:room].decode("utf-8", errors="ignore")
        result["diff"] += piece
        used += len(piece.encode())
        result["truncated"] |= view["truncated"] or len(raw) > room
        result["diff_omitted"] |= view["diff_omitted"]
        result["content_redacted"] |= view["content_redacted"]
    return result
