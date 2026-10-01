"""Exact-edit preparation and target-owned, non-replaying change receipts."""

from contextlib import contextmanager, ExitStack
import hashlib
import io
import itertools
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from openmachine import __version__
from openmachine.foundation.files import MAX_TEXT_FILE, TEXT_PAGE, file_parts
from openmachine.foundation.sanitize import Fault, redact
from openmachine.foundation.state import lease, private_directory, save_json

from .files import MAX_SOURCE, digest, publish_file, publish_path, root_for, snapshot
from . import patch
from .git import context, lock_root

CHANGE_FORMAT = 2
READ_CHANGE_FORMATS = (1, 2)
MAX_PREPARED = 8 * 1024 * 1024
MAX_PARENTS = 16
MAX_PARENT_DEPTH = 8
MAX_RECORD = 65536
MAX_CHANGES = 10000
MAX_EDITS = 64
MAX_WORKSPACE_RESULT = 512 * 1024
TERMINAL = {"applied", "unchanged", "rejected", "partial", "outcome_unknown"}


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


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


def _hex(value, length: int, *, empty: bool = False) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{%d}" % length, value) or empty and value == "")


def _request(operation, path, payload, identity, head, sha, plan_sha) -> dict:
    path_bytes = _text(path)
    file_parts(path)
    if len(path_bytes) > 1024:
        raise Fault("request_limit", "Use a path of at most 1024 UTF-8 bytes.")
    if not (_hex(identity, 64) and _hex(head, 40, empty=True) and
            _hex(sha, 64, empty=operation == "write") and _hex(plan_sha, 64, empty=True)):
        raise Fault("invalid_precondition", "Supply the observed workspace identity, HEAD and complete source hash.")
    if operation == "write":
        _text(payload)
    else:
        if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_EDITS:
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
    if request["expected_plan_sha256"] and request["expected_plan_sha256"] != _hash(plan):
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
    if type(create_parents) is not bool or not (_hex(identity, 64) and _hex(head, 40, empty=True)
                                                and _hex(plan_sha, 64, empty=True)):
        raise Fault("invalid_precondition", "Supply the observed context, optional plan hash and a boolean parent choice.")
    if (not isinstance(preconditions, list) or len(preconditions) != len(touched) or
        any(not isinstance(item, dict) or set(item) != {"path", "expected_sha256"} or
            not isinstance(item["path"], str) or not _hex(item["expected_sha256"], 64, empty=True)
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
        source = snapshot(root, path, max_bytes=min(MAX_SOURCE, MAX_PREPARED - total), allow_missing=True)
        ctx = _context(source)
        if (root_for(config, machine, workspace) != root or ctx["workspace_identity"] != request["expected_workspace_identity"]
            or (ctx["head_sha"] or "") != request["expected_head_sha"] or observed is not None and ctx != observed):
            raise Fault("context_mismatch", "Every source must share the originally observed workspace and HEAD.")
        observed = ctx
        if digest(source["raw"]) != expected[path]:
            raise Fault("content_mismatch", "A complete original source no longer matches its precondition.")
        if len(source["missing_parents"]) > MAX_PARENT_DEPTH:
            raise Fault("request_limit", "A destination may have at most eight missing parent levels.")
        if source["missing_parents"] and not request["create_parents"]:
            raise Fault("parent_missing", "Missing parents require explicit create_parents=true.")
        total += len(source["raw"] or b"")
        sources[path] = source
    parents, prepared_operations, modes = {}, [], {}
    for operation in operations:
        path, kind = operation["path"], operation["kind"]
        source = sources[path]
        result, ranges = patch.prepare(operation, source["raw"], min(MAX_SOURCE, MAX_PREPARED - total))
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
                    if len(parents) >= MAX_PARENTS:
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
    if request["expected_plan_sha256"] and request["expected_plan_sha256"] != _hash(plan):
        raise Fault("plan_mismatch", "The recomputed patch differs from the approved preview.")
    return plan, sources | parents, desired


def _prepared(config, machine, workspace, root, request, operations):
    if operations is not None:
        return _prepare_patch(config, machine, workspace, root, request, operations)
    source, desired, plan = _prepare(config, machine, workspace, root, request)
    return plan, {source["path"]: source}, {source["path"]: desired}


def _effect_specs(plan):
    if "operations" not in plan:
        return [("publish", plan["path"])]
    specs, created = [], set()
    for operation in plan["operations"]:
        kind, path = operation["kind"], operation["path"]
        destination = operation.get("destination", path)
        if kind in {"add", "move"}:
            for parent in plan["parents"]:
                name = parent["path"]
                if name not in created and destination.startswith(name + "/"):
                    specs.append(("mkdir", name))
                    created.add(name)
        if kind == "move":
            specs.extend((("publish", destination), ("remove", path)))
        else:
            specs.append(("remove" if kind == "delete" else "publish", path))
    return specs


def _effects(plan, change_id):
    effects = []
    patching = "operations" in plan
    for index, (kind, path) in enumerate(_effect_specs(plan)):
        temporary = ".openmachine-change-" + change_id + (f"-{index:02x}" if patching else "") if kind == "publish" else None
        effect = {"path": path, "temporary": temporary, "stage": "not_attempted", "result_sha256": None,
                  "synced": False, "readback_verified": False, "error": None, "cleanup": "not_created"}
        if patching:
            effect.update(kind=kind, result_identity=None, result_mode=None)
        effects.append(effect)
    return effects


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


def _record_budget(record):
    """Reserve bounded room for every possible effect/error before any target IO."""
    largest = json.loads(json.dumps(record))
    largest.update(revision=2**63 - 1, status="outcome_unknown", error="x" * 128, finished_at=1.2345678901234567e+100)
    for effect in largest["effects"]:
        effect.update(stage="failed_no_effect", result_sha256="f" * 64, error="x" * 128, cleanup="not_created")
        if "kind" in effect:
            effect.update(result_identity=[2**64 - 1, 2**64 - 1], result_mode=511)
    # A 64 KiB complete record also leaves ample room for both escaped MCP
    # representations, identity metadata and a separately bounded 32 KiB diff.
    if len(json.dumps(largest, sort_keys=True).encode()) + 1024 > MAX_RECORD:
        raise Fault("request_limit", "The complete plan and worst-case effect evidence exceed the record budget.")


def _bound_source(source, effects):
    if not source.get("missing_parents"):
        return source
    created = {effect["path"]: effect["result_identity"] for effect in effects
               if effect.get("kind") == "mkdir" and effect["stage"] == "applied"}
    if any(path not in created for path in source["missing_parents"]):
        raise Fault("parent_unverified", "Every new ancestor needs its original verified creation effect.")
    return source | {"ancestors": source["ancestors"] + [tuple(created[path]) for path in source["missing_parents"]]}


def _validate_patch(record):
    expected = record["expected"]
    if (set(expected) != {"workspace_identity", "head_sha", "plan_sha256", "preconditions", "create_parents"}
        or not _hex(expected["workspace_identity"], 64) or not _hex(expected["head_sha"], 40, empty=True)
        or not _hex(expected["plan_sha256"], 64, empty=True) or type(expected["create_parents"]) is not bool
        or not isinstance(expected["preconditions"], list) or not 1 <= len(expected["preconditions"]) <= patch.MAX_PATHS):
        raise ValueError
    preconditions = expected["preconditions"]
    if any(set(item) != {"path", "expected_sha256"} or not _hex(item["expected_sha256"], 64, empty=True) for item in preconditions):
        raise ValueError
    names = [item["path"] for item in preconditions]
    patch.checked_paths(names)
    if names != sorted(names):
        raise ValueError
    if record["error"] is not None and (not isinstance(record["error"], str) or len(record["error"]) > 128):
        raise ValueError
    plan, effects, status = record["plan"], record["effects"], record["status"]
    if plan is None:
        if record["plan_sha256"] is not None or effects or status not in {"accepted", "rejected", "outcome_unknown"}:
            raise ValueError
        return
    if (set(plan) != {"context", "operations", "files", "parents"} or plan["context"] != record["context"]
        or _hash(plan) != record["plan_sha256"] or plan["context"]["workspace_identity"] != expected["workspace_identity"]
        or (plan["context"]["head_sha"] or "") != expected["head_sha"]
        or not isinstance(plan["files"], list) or len(plan["files"]) != len(names)
        or not isinstance(plan["parents"], list) or len(plan["parents"]) > MAX_PARENTS
        or not isinstance(plan["operations"], list) or not 1 <= len(plan["operations"]) <= patch.MAX_PATHS):
        raise ValueError
    def ancestry(item):
        parts = file_parts(item["path"])
        ancestors, missing = item["ancestors"], item["missing_parents"]
        if (len(item["path"].encode()) > 1024 or not isinstance(ancestors, list) or not ancestors
            or ancestors[0] != plan["context"]["root_id"]
            or any(not isinstance(pair, list) or len(pair) != 2 or any(type(v) is not int or v < 0 for v in pair) for pair in ancestors)
            or not isinstance(missing, list) or len(missing) > MAX_PARENT_DEPTH
            or len(ancestors) + len(missing) != len(parts)
            or missing != ["/".join(parts[:depth]) for depth in range(len(ancestors), len(parts))]):
            raise ValueError
    files = {}
    for item in plan["files"]:
        if set(item) != {"path", "ancestors", "missing_parents", "before_sha256", "after_sha256", "before_size", "after_size", "before_mode", "after_mode"}:
            raise ValueError
        ancestry(item)
        if item["path"] in files:
            raise ValueError
        files[item["path"]] = item
        for side in ("before", "after"):
            sha, mode, size = item[side + "_sha256"], item[side + "_mode"], item[side + "_size"]
            if (not _hex(sha, 64, empty=True) or (mode is None) != (sha == "")
                or mode is not None and (type(mode) is not int or not 0 <= mode <= 0o777)
                or type(size) is not int or not 0 <= size <= MAX_SOURCE or sha == "" and size != 0):
                raise ValueError
    if (set(files) != set(names) or any(files[item["path"]]["before_sha256"] != item["expected_sha256"] for item in preconditions)
        or sum(item["before_size"] + item["after_size"] for item in files.values()) > MAX_PREPARED):
        raise ValueError
    touched, needed = [], set()
    for operation in plan["operations"]:
        kind = operation["kind"]
        if (kind not in {"add", "update", "delete", "move"} or
            set(operation) != ({"kind", "path", "ranges", "destination"} if kind == "move" else {"kind", "path", "ranges"})):
            raise ValueError
        path, destination = operation["path"], operation.get("destination", operation["path"])
        before, after = files[path], files[destination]
        touched.append(path)
        if kind == "move":
            touched.append(destination)
        if (kind == "add" and (before["before_sha256"] != "" or after["after_mode"] != 0o644)
            or kind != "add" and not before["before_sha256"]
            or kind in {"delete", "move"} and before["after_sha256"] != ""
            or kind in {"add", "update", "move"} and not after["after_sha256"]
            or kind in {"update", "move"} and before["before_mode"] != after["after_mode"]
            or kind == "move" and after["before_sha256"] != ""):
            raise ValueError
        if kind in {"add", "move"}:
            needed.update(after["missing_parents"])
        elif before["missing_parents"]:
            raise ValueError
        ranges, old_end, new_end = operation["ranges"], 0, 0
        if not isinstance(ranges, list) or len(ranges) > patch.MAX_HUNKS + 1 or not ranges and kind != "move":
            raise ValueError
        for span in ranges:
            if (set(span) != {"old_start", "old_end", "new_start", "new_end"}
                or any(type(value) is not int for value in span.values())
                or not old_end <= span["old_start"] <= span["old_end"] <= before["before_size"]
                or not new_end <= span["new_start"] <= span["new_end"] <= after["after_size"]):
                raise ValueError
            old_end, new_end = span["old_end"], span["new_end"]
    if touched != list(files) or len(set(touched)) != len(touched):
        raise ValueError
    parents = set()
    for item in plan["parents"]:
        if set(item) != {"path", "ancestors", "missing_parents"} or item["path"] in parents:
            raise ValueError
        ancestry(item)
        if any(path not in parents for path in item["missing_parents"]):
            raise ValueError
        parents.add(item["path"])
    if parents != needed or parents and not expected["create_parents"]:
        raise ValueError
    templates = _effects(plan, record["change_id"])
    if not isinstance(effects, list) or len(effects) != len(templates):
        raise ValueError
    stopped = False
    for effect, template in zip(effects, templates):
        if set(effect) != set(template) or any(effect[key] != template[key] for key in ("path", "temporary", "kind")):
            raise ValueError
        stage, kind = effect["stage"], effect["kind"]
        if (stage not in {"not_attempted", "attempting", "applied", "unchanged", "failed_no_effect", "unknown"}
            or stopped and stage != "not_attempted"
            or type(effect["synced"]) is not bool or type(effect["readback_verified"]) is not bool
            or effect["cleanup"] not in {"not_created", "pending", "removed", "unknown"}
            or effect["error"] is not None and (not isinstance(effect["error"], str) or len(effect["error"]) > 128)
            or effect["result_sha256"] is not None and not _hex(effect["result_sha256"], 64, empty=True)):
            raise ValueError
        stopped |= stage not in {"applied", "unchanged"}
        identity, mode = effect["result_identity"], effect["result_mode"]
        if (identity is not None and (kind != "mkdir" or not isinstance(identity, list) or len(identity) != 2
                                     or any(type(value) is not int or value < 0 for value in identity))
            or mode is not None and (kind != "mkdir" or type(mode) is not int or not 0 <= mode <= 0o777)):
            raise ValueError
        if stage in {"not_attempted", "attempting"} and effect != template | {"stage": stage}:
            raise ValueError
        if stage in {"applied", "unchanged"}:
            if not effect["readback_verified"] or effect["error"] is not None:
                raise ValueError
            if stage == "applied" and (not effect["synced"] or effect["cleanup"] != ("removed" if kind == "publish" else "not_created")):
                raise ValueError
            if kind == "mkdir":
                if stage != "applied" or identity is None or mode is None or effect["result_sha256"] is not None:
                    raise ValueError
            elif effect["result_sha256"] != files[effect["path"]]["after_sha256"]:
                raise ValueError
            if stage == "unchanged" and (kind != "publish" or effect["cleanup"] != "not_created"
                or files[effect["path"]]["before_sha256"] != files[effect["path"]]["after_sha256"]):
                raise ValueError
    stages = [effect["stage"] for effect in effects]
    uncertain = any(effect["stage"] in {"unknown", "attempting"} or effect["cleanup"] == "unknown" for effect in effects)
    if (status == "accepted" or status == "prepared" and any(stage != "not_attempted" for stage in stages)
        or status == "applying" and any(stage in {"unknown", "failed_no_effect"} for stage in stages)
        or status == "applied" and (any(stage not in {"applied", "unchanged"} for stage in stages) or "applied" not in stages)
        or status == "unchanged" and any(stage != "unchanged" for stage in stages)
        or status == "rejected" and ("applied" in stages or uncertain)
        or status == "partial" and ("applied" not in stages or uncertain or all(stage in {"applied", "unchanged"} for stage in stages))
        or status in {"applied", "unchanged"} and record["error"] is not None):
        raise ValueError



def _private(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or
        info.st_uid != os.getuid() or info.st_mode & 0o077 or not directory and info.st_nlink != 1):
        raise Fault("change_store_corrupt", "Preserve unsafe or linked change state; it cannot be used.")
    return info


def _owner(state: Path) -> str:
    info = _private(state, directory=True)
    if state.resolve() != state:
        raise Fault("change_store_corrupt", "Use the original canonical target state directory.")
    return _hash({"state": str(state), "device": info.st_dev, "inode": info.st_ino, "uid": os.getuid()})


def _entries(store: Path) -> list[Path]:
    entries = []
    for item in store.iterdir():
        if not _hex(item.name, 32):
            raise Fault("change_store_corrupt", "An invalid change identity is retained in the store.")
        _private(item, directory=True)
        entries.append(item)
        if len(entries) > MAX_CHANGES:
            raise Fault("change_store_full", "Retain existing history; this store exceeds its identity budget.")
    return entries


def _store(state: Path) -> Path | None:
    store, lock = state / "workspace-changes", state / "locks/workspace-changes.lock"
    if not os.path.lexists(store):
        if os.path.lexists(lock):
            raise Fault("change_store_corrupt", "The established change store is missing; do not initialize replacement history.")
        return None
    _owner(state)
    _private(store, directory=True)
    _private(state / "locks", directory=True)
    _private(lock)
    return store


def _validate(record: dict, change_id: str) -> None:
    try:
        if type(record.get("format")) is not int or record["format"] not in READ_CHANGE_FORMATS:
            raise Fault("unsupported_change_format", "The retained change format requires a compatible reader.")
        if set(record) != {"format", "change_id", "writer_version", "owner_sha256", "request_sha256", "operation",
                           "created_at", "revision", "context", "expected", "plan", "plan_sha256", "effects",
                           "status", "finished_at", "error"}:
            raise ValueError
        if (record["change_id"] != change_id or not _hex(change_id, 32) or
            not _hex(record["owner_sha256"], 64) or not _hex(record["request_sha256"], 64) or
            record["operation"] not in {"edit", "write", "patch"} or
            (record["operation"] == "patch") != (record["format"] == 2) or
            not isinstance(record["writer_version"], str) or not 1 <= len(record["writer_version"]) <= 100 or
            type(record["revision"]) is not int or record["revision"] < 1 or
            record["status"] not in TERMINAL | {"accepted", "prepared", "applying"}):
            raise ValueError
        for value in (record["created_at"], record["finished_at"]):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError
        if record["created_at"] is None or (record["finished_at"] is not None) != (record["status"] in TERMINAL):
            raise ValueError
        ctx = record["context"]
        if (set(ctx) != {"root", "root_id", "worktree", "workspace_identity", "workspace_identity_version", "git_state", "head_sha"}
            or not Path(ctx["root"]).is_absolute() or not Path(ctx["worktree"]).is_absolute()
            or not isinstance(ctx["root_id"], list) or len(ctx["root_id"]) != 2
            or any(type(v) is not int or v < 0 for v in ctx["root_id"])
            or type(ctx["workspace_identity_version"]) is not int or ctx["workspace_identity_version"] != 1
            or not _hex(ctx["workspace_identity"], 64)
            or ctx["git_state"] not in {"worktree", "detached", "unborn", "non_git"}
            or ctx["head_sha"] is not None and not _hex(ctx["head_sha"], 40)):
            raise ValueError
        if record["format"] == 2:
            try:
                _validate_patch(record)
            except Fault as error:
                raise ValueError from error
            return
        if record["status"] == "partial":
            raise ValueError
        expected = record["expected"]
        if (set(expected) != {"path", "workspace_identity", "head_sha", "sha256", "plan_sha256"}
            or not _hex(expected["workspace_identity"], 64) or not _hex(expected["head_sha"], 40, empty=True)
            or not _hex(expected["sha256"], 64, empty=record["operation"] == "write")
            or not _hex(expected["plan_sha256"], 64, empty=True)):
            raise ValueError
        if not isinstance(expected["path"], str) or len(expected["path"].encode()) > 1024:
            raise ValueError
        file_parts(expected["path"])
        if record["error"] is not None and (not isinstance(record["error"], str) or len(record["error"]) > 128):
            raise ValueError
        plan, effects = record["plan"], record["effects"]
        if plan is None:
            if record["plan_sha256"] is not None or effects or record["status"] not in {"accepted", "rejected", "outcome_unknown"}:
                raise ValueError
            return
        if (set(plan) != {"context", "path", "before_sha256", "after_sha256", "before_mode", "after_mode", "ancestors", "ranges"}
            or _hash(plan) != record["plan_sha256"] or plan["context"] != ctx
            or plan["path"] != expected["path"] or plan["before_sha256"] != expected["sha256"]
            or ctx["workspace_identity"] != expected["workspace_identity"]
            or (ctx["head_sha"] or "") != expected["head_sha"]
            or not _hex(plan["after_sha256"], 64) or not isinstance(effects, list) or len(effects) != 1
            or type(plan["after_mode"]) is not int or not 0 <= plan["after_mode"] <= 0o777
            or plan["before_mode"] is not None and (type(plan["before_mode"]) is not int or not 0 <= plan["before_mode"] <= 0o777)
            or not isinstance(plan["ranges"], list) or not 1 <= len(plan["ranges"]) <= MAX_EDITS):
            raise ValueError
        if (record["status"] == "accepted" or
            (plan["before_mode"] is None) != (plan["before_sha256"] == "") or
            plan["after_mode"] != (plan["before_mode"] if plan["before_mode"] is not None else 0o644) or
            not isinstance(plan["ancestors"], list) or not plan["ancestors"] or
            plan["ancestors"][0] != ctx["root_id"] or
            any(not isinstance(item, list) or len(item) != 2 or any(type(v) is not int or v < 0 for v in item)
                for item in plan["ancestors"])):
            raise ValueError
        prior_old = prior_new = 0
        for span in plan["ranges"]:
            if set(span) != {"old_start", "old_end", "new_start", "new_end"} or any(type(v) is not int or not 0 <= v <= MAX_SOURCE for v in span.values()):
                raise ValueError
            if (span["old_start"] > span["old_end"] or span["new_start"] > span["new_end"] or
                span["old_start"] < prior_old or span["new_start"] < prior_new):
                raise ValueError
            prior_old, prior_new = span["old_end"], span["new_end"]
        effect = effects[0]
        if (set(effect) != {"path", "temporary", "stage", "result_sha256", "synced", "readback_verified", "error", "cleanup"}
            or effect["path"] != plan["path"] or effect["temporary"] != ".openmachine-change-" + change_id
            or effect["stage"] not in {"not_attempted", "attempting", "applied", "unchanged", "failed_no_effect", "unknown"}
            or type(effect["synced"]) is not bool or type(effect["readback_verified"]) is not bool
            or effect["cleanup"] not in {"not_created", "pending", "removed", "unknown"}
            or effect["result_sha256"] is not None and not _hex(effect["result_sha256"], 64, empty=True)
            or effect["error"] is not None and (not isinstance(effect["error"], str) or len(effect["error"]) > 128)):
            raise ValueError
        if record["status"] == "applied" and (effect["stage"] != "applied" or not effect["synced"] or
            not effect["readback_verified"] or effect["result_sha256"] != plan["after_sha256"] or effect["cleanup"] != "removed"):
            raise ValueError
        if record["status"] == "unchanged" and (effect["stage"] != "unchanged" or plan["before_sha256"] != plan["after_sha256"]):
            raise ValueError
        if record["status"] == "rejected" and effect["stage"] not in {"not_attempted", "failed_no_effect"}:
            raise ValueError
        if record["status"] == "prepared" and effect["stage"] != "not_attempted":
            raise ValueError
        if record["status"] == "applying" and effect["stage"] != "attempting":
            raise ValueError
        if record["status"] in {"applied", "unchanged"} and (record["error"] is not None or effect["error"] is not None):
            raise ValueError
        if effect["stage"] in {"not_attempted", "attempting"} and (effect["synced"] or effect["readback_verified"] or effect["result_sha256"] is not None):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise Fault("change_store_corrupt", "The retained change record is invalid; never repair or replay it implicitly.") from error


def _load(directory: Path) -> dict:
    _private(directory, directory=True)
    _private(directory / "writer.lock")
    path = directory / "record.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        actual = os.fstat(source.fileno())
        # An atomic record save may unlink this already-open prior snapshot.
        if (not stat.S_ISREG(actual.st_mode) or actual.st_uid != os.getuid() or actual.st_mode & 0o077
            or actual.st_nlink > 1 or actual.st_size > MAX_RECORD):
            raise Fault("change_store_corrupt", "The opened change record is unsafe or oversized.")
        data = source.read(MAX_RECORD + 1)
    try:
        record = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise Fault("change_store_corrupt", "The retained change is not valid bounded JSON.") from error
    if not isinstance(record, dict) or len(data) > MAX_RECORD:
        raise Fault("change_store_corrupt", "The retained change is not a bounded object.")
    _validate(record, directory.name)
    return record


def _save(directory: Path, record: dict) -> None:
    candidate = record | {"revision": record["revision"] + 1}
    _validate(candidate, directory.name)
    if len(json.dumps(candidate, sort_keys=True).encode()) + 1 > MAX_RECORD:
        raise Fault("request_limit", "The complete change record exceeds 64 KiB before effects.")
    try:
        if record["revision"] and _load(directory)["revision"] != record["revision"]:
            raise Fault("change_store_corrupt", "The original writer revision changed.")
        save_json(directory / "record.json", candidate)
        if _load(directory) != candidate:
            raise OSError("Record readback mismatch")
    except (OSError, Fault) as error:
        raise Fault("change_record_unverified", "The original record save is unverified; do not repeat this change.") from error
    record["revision"] = candidate["revision"]


@contextmanager
def _own(state: Path, change_id: str):
    private_directory(state)
    locks = private_directory(state / "locks")
    store, allocation = state / "workspace-changes", locks / "workspace-changes.lock"
    initial = not os.path.lexists(store) and not os.path.lexists(allocation)
    if not initial:
        _private(allocation)
    with ExitStack() as owner:
        with lease(allocation, create=initial):
            _private(allocation)
            if initial and not os.path.lexists(store):
                private_directory(store)
                fd = os.open(state, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            _private(store, directory=True)
            directory = store / change_id
            existing = os.path.lexists(directory)
            if not existing:
                if len(_entries(store)) >= MAX_CHANGES:
                    raise Fault("change_store_full", "The target retains 10000 identities; existing IDs remain readable.")
                directory.mkdir(mode=0o700)
            _private(directory, directory=True)
            if not existing:
                owner.enter_context(lease(directory / "writer.lock"))
                _private(directory / "writer.lock")
                fd = os.open(store, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        yield directory, existing


def _scope(config, state: Path, machine: str, record: dict) -> str:
    config.machine(machine)
    if _owner(state) != record["owner_sha256"]:
        raise Fault("scope_unavailable", "This receipt belongs to its original target state owner.")
    for name, item in config.workspaces.items():
        if item.machine != machine:
            continue
        try:
            root = root_for(config, machine, name)
            info = root.stat()
            if str(root) == record["context"]["root"] and [info.st_dev, info.st_ino] == record["context"]["root_id"]:
                return name
        except (OSError, Fault):
            continue
    raise Fault("scope_unavailable", "Current configuration no longer grants the original canonical workspace.")


def _observed(directory: Path, record: dict | None = None, *, owned: bool = False) -> dict:
    """Observe ownership before loading; a stale snapshot must never prove no effects."""
    with ExitStack() as ownership:
        alive = owned
        if not owned:
            _private(directory, directory=True)
            _private(directory / "writer.lock")
            try:
                ownership.enter_context(lease(directory / "writer.lock", shared=True, create=False))
            except Fault as error:
                if error.code != "resource_busy":
                    raise
                alive = True
        observed_at = time.time()
        if not owned:
            try:
                record = _load(directory)
            except FileNotFoundError:
                if alive:
                    raise Fault("resource_busy", "The original writer has not published its first receipt yet.") from None
                raise
        classification = "terminal" if record["status"] in TERMINAL else "owner_active" if alive else (
            "no_effects" if record["status"] in {"accepted", "prepared"} else "outcome_unknown")
        result = record | {"writer_alive": alive, "owner_observed_at": observed_at, "reconciliation": classification,
                           "historical_diff_available": False,
                           "next_observation": {"operation": "read_workspace_change", "change_id": record["change_id"]}}
        # Derived guidance only; never persist it or infer safety from an error code alone.
        if record["status"] == "rejected" and not record["effects"]:
            hint = {
                "match_ambiguous": "Use unique surrounding text to select the intended occurrence.",
                "match_missing": "Read the current source and correct the absent old fragment.",
                "context_mismatch": "Re-read workspace, branch and HEAD; reconcile the changed context.",
                "content_mismatch": "Read the current source and reconcile its changed content.",
            }.get(record["error"])
            if hint:
                preparation = "review the complete replacement" if record["operation"] == "write" else "preview the corrected intent"
                result["recovery_hint"] = ("No file effects were recorded. " + hint +
                    f" Continue only within current authorization; {preparation}, then use a new change ID."
                    " Preserve this original receipt; do not replay its request.")
        return result


def read_change(config, state: Path, machine: str, change_id: str, observe_current: bool = False) -> dict:
    config.machine(machine)
    if not _hex(change_id, 32) or type(observe_current) is not bool:
        raise Fault("invalid_change_id", "Use the original 32-lowercase-hex change ID and a boolean observation flag.")
    store = _store(state)
    if store is None or not os.path.lexists(store / change_id):
        raise Fault("change_not_found", "No retained record was found; absence is not proof of non-admission.")
    directory = store / change_id
    try:
        record = _observed(directory)
    except FileNotFoundError as error:
        raise Fault("change_history_unavailable", "The original allocation is incomplete; never reuse this ID.") from error
    name = _scope(config, state, machine, record)
    result = record | {"machine": machine, "workspace": name}
    if observe_current and record["format"] == 2:
        current, total = [], 0
        for item in record["expected"]["preconditions"]:
            try:
                source = snapshot(Path(record["context"]["root"]), item["path"],
                                  max_bytes=min(MAX_SOURCE, MAX_PREPARED - total), allow_missing=True)
                total += len(source["raw"] or b"")
                current.append({"path": item["path"], "exists": source["raw"] is not None,
                                "sha256": digest(source["raw"]), "context": _context(source)})
            except (OSError, Fault) as error:
                current.append({"path": item["path"], "error": error.code if isinstance(error, Fault) else type(error).__name__})
        result["current"] = {"files": current, "observed_at": time.time(), "complete": all("error" not in item for item in current)}
    elif observe_current:
        try:
            source = snapshot(Path(record["context"]["root"]), record["expected"]["path"])
            result["current"] = {"exists": source["raw"] is not None, "sha256": digest(source["raw"]),
                                 "observed_at": time.time(), "context": _context(source)}
        except (OSError, Fault) as error:
            result["current"] = {"error": error.code if isinstance(error, Fault) else type(error).__name__}
    return result


def check_change_state(state: Path) -> dict:
    """Public, read-only format validation for candidate/target compatibility checks."""
    store = _store(state)
    count, formats = 0, set()
    if store is not None:
        owner = _owner(state)
        for directory in _entries(store):
            try:
                record = _load(directory)
            except FileNotFoundError as error:
                raise Fault("change_history_unavailable", "A retained allocation has incomplete history.") from error
            if record["owner_sha256"] != owner:
                raise Fault("change_store_corrupt", "A retained change belongs to a different state owner.")
            count += 1
            formats.add(record["format"])
    return {"workspace_change_format": CHANGE_FORMAT, "workspace_change_read_formats": list(READ_CHANGE_FORMATS),
            "workspace_change_formats_present": sorted(formats), "workspace_change_records": count,
            "workspace_change_store_present": store is not None}


def _initial_record(request, change_id, observed_context, owner, fingerprint):
    expected = {"workspace_identity": request["expected_workspace_identity"], "head_sha": request["expected_head_sha"],
                "plan_sha256": request["expected_plan_sha256"]}
    patching = request["operation"] == "patch"
    expected.update({"preconditions": request["preconditions"], "create_parents": request["create_parents"]} if patching else
                    {"path": request["path"], "sha256": request["expected_sha256"]})
    return {"format": CHANGE_FORMAT if patching else 1, "change_id": change_id, "writer_version": __version__,
            "owner_sha256": owner, "request_sha256": fingerprint, "operation": request["operation"],
            "created_at": time.time(), "revision": 0, "context": observed_context, "expected": expected,
            "plan": None, "plan_sha256": None, "effects": [], "status": "accepted", "finished_at": None, "error": None}


def _apply(config, state, machine, workspace, request, change_id, dry_run, validate_new, operations=None):
    root = root_for(config, machine, workspace)
    if dry_run:
        plan, sources, desired = _prepared(config, machine, workspace, root, request, operations)
        # Use the actual record shape for pure preview sizing without allocating
        # a state owner, change identity, lease, temporary file or parent.
        sizing = _initial_record(request, "0" * 32, plan["context"], "0" * 64, "0" * 64)
        sizing.update(plan=plan, plan_sha256=_hash(plan), effects=_effects(plan, "0" * 32), status="prepared")
        _record_budget(sizing)
        target = config.machine(machine)
        grant = target.workspace_write_enabled
        effective = target.execution_enabled if grant is None else grant
        return {"machine": machine, "workspace": workspace, "status": "preview", "plan": plan,
                "plan_sha256": _hash(plan), "workspace_write_enabled": effective, "diff_kind": "proposed",
                **_display(plan, sources, desired)}
    fingerprint = _hash(request | {"root": str(root)})
    # Reconcile retained identities before mutable source, grants or new leases.
    store = _store(state)
    if store is not None and os.path.lexists(store / change_id):
        original = read_change(config, state, machine, change_id)
        if original["request_sha256"] != fingerprint:
            raise Fault("change_id_conflict", "This change ID already belongs to a different immutable request.")
        return original
    config.workspace_machine(machine)
    observed = context(root)
    if observed["workspace_identity"] is None:
        raise Fault("context_unavailable", "Workspace identity is unavailable; no change was admitted.")
    record = _initial_record(request, change_id, _context({"root": root, "context": observed}),
                             "0" * 64, fingerprint)
    # Reject oversized admission/rejection evidence before reserving an identity.
    # The actual owner has the same encoded width; prepared plans are sized later.
    _record_budget(record)
    patching = operations is not None
    with _own(state, change_id) as (directory, existing):
        if existing:
            original = read_change(config, state, machine, change_id)
            if original["request_sha256"] != fingerprint:
                raise Fault("change_id_conflict", "This change ID belongs to a different request.")
            return original
        record["owner_sha256"] = _owner(state)
        _save(directory, record)
        try:
            if validate_new is not None:
                validate_new(root)
            key = hashlib.sha256(lock_root(config, machine, root, observed).encode()).hexdigest()
            with lease(state / "locks" / f"workspace-{key}.lock"):
                plan, sources, desired = _prepared(config, machine, workspace, root, request, operations)
                if plan["context"] != record["context"]:
                    raise Fault("context_mismatch", "Workspace context changed after request admission.")
                prepared = record | {"plan": plan, "plan_sha256": _hash(plan), "effects": _effects(plan, change_id), "status": "prepared"}
                _record_budget(prepared)
                record.update(prepared)
                _save(directory, record)
                display = _display(plan, sources, desired)
                modes = {item["path"]: item["after_mode"] for item in plan["files"]} if patching else {}
                for effect in record["effects"]:
                    kind, path = effect.get("kind", "publish"), effect["path"]
                    source = _bound_source(sources[path], record["effects"])
                    if kind == "publish" and source["raw"] == desired[path]:
                        effect.update(stage="unchanged", result_sha256=digest(desired[path]), readback_verified=True)
                    else:
                        record["status"] = "applying"
                        effect["stage"] = "attempting"
                        _save(directory, record)
                        if kind == "publish":
                            arguments = {"mode": modes[path]} if patching else {}
                            observation = publish_file(config, machine, workspace, source, desired[path], effect["temporary"], **arguments)
                        else:
                            observation = publish_path(config, machine, workspace, source, kind)
                        effect.update(observation)
                        if effect["stage"] != "applied" or effect["cleanup"] not in {"removed", "not_created"}:
                            record["error"] = effect["error"]
                            break
                    if patching:
                        record["status"] = "applying"
                        _save(directory, record)
                effects = record["effects"]
                if any(effect["stage"] in {"unknown", "attempting"} or effect["cleanup"] == "unknown" for effect in effects):
                    outcome = "outcome_unknown"
                elif all(effect["stage"] in {"applied", "unchanged"} for effect in effects):
                    outcome = "applied" if any(effect["stage"] == "applied" for effect in effects) else "unchanged"
                else:
                    outcome = "partial" if any(effect["stage"] == "applied" for effect in effects) else "rejected"
                record.update(status=outcome, finished_at=time.time())
                _save(directory, record)
                result = _observed(directory, record, owned=True) | {"machine": machine, "workspace": workspace}
                if outcome in {"applied", "unchanged"}:
                    result.update(display, diff_kind="applied")
                else:
                    result.update(diff_omitted=True)
                return result
        except (OSError, Fault) as error:
            code = error.code if isinstance(error, Fault) else type(error).__name__
            uncertain = code == "change_record_unverified" or any(e["stage"] in {"attempting", "unknown"} or
                        e["cleanup"] == "unknown" for e in record["effects"])
            applied = any(e["stage"] == "applied" for e in record["effects"])
            record.update(status="outcome_unknown" if uncertain else "partial" if applied else "rejected",
                          error=code, finished_at=time.time())
            if not uncertain:
                _save(directory, record)
                return _observed(directory, record, owned=True) | {"machine": machine, "workspace": workspace}
            raise Fault("change_outcome_unknown", "Read this original change and current files; never replay uncertain effects.",
                        record | {"record_persistence": "unverified", "machine": machine, "workspace": workspace}) from error


def apply_patch(config, state: Path, machine: str, workspace: str, patch_text: str,
                expected_workspace_identity: str, expected_head_sha: str, preconditions: list[dict],
                create_parents: bool = False, change_id: str = "", dry_run: bool = False,
                expected_plan_sha256: str = "", *, validate_new=None) -> dict:
    try:
        if type(dry_run) is not bool or (change_id != "" if dry_run else not _hex(change_id, 32)):
            raise Fault("invalid_change_id", "Preview requires no ID; apply requires a new 32-lowercase-hex ID.")
        request, operations = _patch_request(patch_text, preconditions, expected_workspace_identity,
                                             expected_head_sha, create_parents, expected_plan_sha256)
        return _apply(config, state, machine, workspace, request, change_id, dry_run, validate_new, operations)
    except Fault as error:
        error.evidence = {"machine": machine, "workspace": workspace, "change_id": change_id, **(error.evidence or {})}
        raise


def edit_text_file(config, state: Path, machine: str, workspace: str, path: str, edits: list[dict],
                   expected_workspace_identity: str, expected_head_sha: str, expected_sha256: str,
                   change_id: str = "", dry_run: bool = False, expected_plan_sha256: str = "",
                   *, validate_new=None) -> dict:
    try:
        if type(dry_run) is not bool or (change_id != "" if dry_run else not _hex(change_id, 32)):
            raise Fault("invalid_change_id", "Preview requires no ID; apply requires a new 32-lowercase-hex ID.")
        request = _request("edit", path, edits, expected_workspace_identity, expected_head_sha, expected_sha256, expected_plan_sha256)
        return _apply(config, state, machine, workspace, request, change_id, dry_run, validate_new)
    except Fault as error:
        error.evidence = {"machine": machine, "workspace": workspace, "change_id": change_id,
                          **(error.evidence or {})}
        raise


def write_text_file(config, state: Path, machine: str, workspace: str, path: str, content: str,
                    expected_head_sha: str, expected_sha256: str, change_id: str = "",
                    expected_workspace_identity: str = "", *, validate_new=None) -> dict:
    if not _hex(change_id, 32, empty=True) or not _hex(expected_workspace_identity, 64, empty=True):
        raise Fault("invalid_precondition", "Use a valid change ID and workspace identity, or explicit empty legacy values.")
    if change_id:
        try:
            request = _request("write", path, content, expected_workspace_identity, expected_head_sha, expected_sha256, "")
            return _apply(config, state, machine, workspace, request, change_id, False, validate_new)
        except Fault as error:
            error.evidence = {"machine": machine, "workspace": workspace, "change_id": change_id, **(error.evidence or {})}
            raise
    config.workspace_machine(machine)
    if not _hex(expected_head_sha, 40, empty=True) or not _hex(expected_sha256, 64, empty=True):
        raise Fault("invalid_precondition", "Supply the observed HEAD and whole-file hash; empty means absent.")
    desired = _text(content)
    root = root_for(config, machine, workspace)
    observed = context(root)
    key = hashlib.sha256(lock_root(config, machine, root, observed).encode()).hexdigest()
    private_directory(state)
    private_directory(state / "locks")
    if validate_new is not None:
        validate_new(root)
    with lease(state / "locks" / f"workspace-{key}.lock"):
        source = snapshot(root, path)
        if (source["context"]["head_sha"] or "") != expected_head_sha:
            raise Fault("context_mismatch", "Git HEAD changed; inspect the original workspace.")
        result = {"machine": machine, "workspace": workspace, "path": path, "head_sha": source["context"]["head_sha"],
                  "sha256": digest(desired), "previous_sha256": digest(source["raw"]), "size_bytes": len(desired),
                  "durable_recovery": False}
        if source["raw"] == desired:
            return result | {"status": "already_current"}
        if digest(source["raw"]) != expected_sha256:
            raise Fault("content_mismatch", "The original source changed; do not overwrite it.")
        effect = publish_file(config, machine, workspace, source, desired, ".openmachine-edit-" + uuid.uuid4().hex)
        if effect["stage"] == "failed_no_effect" and effect["cleanup"] != "unknown":
            raise Fault(effect["error"] or "publication_failed", "No file replacement was performed.")
        return result | {"status": "written" if effect["stage"] == "applied" and effect["cleanup"] == "removed" else "write_outcome_unknown",
                         "effect": effect}


def bound_result(payload: dict, encoded_size, limit: int = MAX_WORKSPACE_RESULT) -> dict:
    """Bound presentation without skipping source bytes or captured entries."""
    result = dict(payload)
    while encoded_size(result) > limit and result.get("diff"):
        raw = result["diff"].encode()
        result["diff"] = raw[:len(raw) // 2].decode("utf-8", errors="ignore")
        result["truncated"] = True
    for field in ("matches", "entries"):
        while encoded_size(result) > limit and len(result.get(field, [])) > 1:
            result[field] = result[field][:len(result[field]) // 2]
            result.update(next_offset=result["offset"] + len(result[field]), has_more=True,
                          presentation_truncated=True)
    # Text pages are already bounded by their source owner. Further clipping here
    # would lose the raw cursor mapping after redaction; reject without advancing.
    if encoded_size(result) > limit:
        evidence = {"change_id": result["change_id"], "status": "outcome_unknown"} if result.get("change_id") else None
        raise Fault("workspace_response_limit", "Workspace metadata exceeds its response budget; narrow the observation.", evidence)
    return result
