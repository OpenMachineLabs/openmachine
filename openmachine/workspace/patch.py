"""Strict om_patch_v1 parsing and original-byte preparation; no filesystem IO."""

import unicodedata

from openmachine.foundation.files import MAX_TEXT_FILE, file_parts
from openmachine.foundation.sanitize import Fault

LANGUAGE = "om_patch_v1"
MAX_PATHS = 32
MAX_HUNKS = 64


def checked_paths(paths: list[str]) -> None:
    """Reject aliases and file/parent conflicts without guessing filesystem rules."""
    if not 1 <= len(paths) <= MAX_PATHS:
        raise Fault("request_limit", "A patch may touch 1 through 32 unique file paths.")
    spellings, leaves = {}, set()
    for path in paths:
        parts = file_parts(path)
        if len(path.encode()) > 1024:
            raise Fault("request_limit", "Use paths of at most 1024 UTF-8 bytes.")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            key = unicodedata.normalize("NFC", prefix.casefold())
            if key in spellings and spellings[key] != prefix:
                raise Fault("path_conflict", "Case or Unicode-equivalent path spellings are ambiguous.")
            spellings[key] = prefix
            if depth < len(parts) and key in leaves:
                raise Fault("path_conflict", "A file cannot also be another target's parent.")
        if key in leaves or any(other.startswith(key + "/") for other in leaves):
            raise Fault("path_conflict", "Each path participates once, without file/parent conflicts.")
        leaves.add(key)


def paths(operations: list[dict]) -> list[str]:
    return [path for operation in operations for path in
            ([operation["path"], operation["destination"]] if operation["kind"] == "move"
             else [operation["path"]])]


def parse(text: str) -> list[dict]:
    try:
        if not isinstance(text, str) or "\0" in text or "\r" in text:
            raise ValueError
        raw = text.encode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise Fault("invalid_patch", "Use UTF-8 patch text with exact LF control lines, without NUL or CR.") from error
    if len(raw) > MAX_TEXT_FILE:
        raise Fault("request_limit", "Patch text is limited to 128 KiB.")
    lines = raw.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    if len(lines) < 3 or lines[0] != b"*** Begin Patch" or lines[-1] != b"*** End Patch":
        raise Fault("invalid_patch", "Use one nonempty om_patch_v1 document without fences or extra text.")
    operations, index, hunks = [], 1, 0
    headers = {b"*** Add File: ": "add", b"*** Update File: ": "update", b"*** Delete File: ": "delete"}
    while index < len(lines) - 1:
        header = next((prefix for prefix in headers if lines[index].startswith(prefix)), None)
        if header is None:
            raise Fault("invalid_patch", "Expected an exact Add, Update or Delete File header.")
        operation = {"kind": headers[header], "path": lines[index][len(header):].decode()}
        index += 1
        if operation["kind"] == "add":
            added = []
            while index < len(lines) - 1 and lines[index].startswith(b"+"):
                added.append(lines[index][1:])
                index += 1
            operation["lines"] = added
        elif operation["kind"] == "update":
            if lines[index].startswith(b"*** Move to: "):
                operation.update(kind="move", destination=lines[index][len(b"*** Move to: "):].decode())
                index += 1
            chunks = []
            while index < len(lines) - 1 and lines[index] == b"@@":
                index += 1
                body = []
                while index < len(lines) - 1 and lines[index][:1] in (b" ", b"-", b"+"):
                    body.append((chr(lines[index][0]), lines[index][1:]))
                    index += 1
                eof = index < len(lines) - 1 and lines[index] == b"*** End of File"
                if eof:
                    index += 1
                if not any(prefix in " -" for prefix, _ in body) or not any(prefix in "+-" for prefix, _ in body):
                    raise Fault("invalid_patch", "Each hunk needs original context and an addition or removal.")
                chunks.append({"body": body, "eof": eof})
                hunks += 1
                if hunks > MAX_HUNKS:
                    raise Fault("request_limit", "A patch may contain at most 64 hunks.")
            if not chunks and operation["kind"] != "move":
                raise Fault("invalid_patch", "An update needs a hunk; only a pure move may omit hunks.")
            operation["hunks"] = chunks
        operations.append(operation)
        if len(operations) > MAX_PATHS:
            raise Fault("request_limit", "A patch exceeds its path budget.")
    checked_paths(paths(operations))
    return operations


def _source_lines(raw: bytes) -> tuple[bytes, list[tuple[bytes, bytes]], list[int]]:
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    content = raw[len(bom):]
    lines, offsets = [], [len(bom)]
    pieces = content.split(b"\n") if content else []
    for index, piece in enumerate(pieces):
        has_lf = index < len(pieces) - 1
        if not has_lf and not piece:
            break
        ending = b"\r\n" if has_lf and piece.endswith(b"\r") else b"\n" if has_lf else b""
        payload = piece[:-1] if ending == b"\r\n" else piece
        if b"\r" in payload:
            raise Fault("invalid_patch_source", "Patch updates reject bare CR; use exact edits for these bytes.")
        lines.append((payload, ending))
        offsets.append(offsets[-1] + len(payload) + len(ending))
    return bom, lines, offsets


def _update(raw: bytes, hunks: list[dict], max_bytes: int) -> tuple[bytes, list[dict]]:
    bom, lines, before_offsets = _source_lines(raw)
    separator = next((ending for _, ending in lines if ending), b"\n")
    final_newline = bool(lines and lines[-1][1])
    # LF is a delimiter, not content, in this exact logical-line view. Byte find
    # avoids a quadratic comparison for repeated lines or long near-matches.
    logical = b"".join(payload + b"\n" for payload, _ in lines)
    matched = []
    for hunk in hunks:
        old = [payload for prefix, payload in hunk["body"] if prefix != "+"]
        needle = b"\n".join(old) + b"\n"
        found, start = None, logical.find(needle)
        while start >= 0:
            if (start == 0 or logical[start - 1] == 10) and (
                    not hunk["eof"] or start + len(needle) == len(logical)):
                if found is not None:
                    raise Fault("match_ambiguous", "A hunk has multiple exact locations in the original source.")
                found = start
            start = logical.find(needle, start + 1)
        if found is None:
            raise Fault("match_missing", "A hunk's exact original lines or required EOF are absent.")
        first = logical.count(b"\n", 0, found)
        last = first + len(old)
        if matched and first < matched[-1][1]:
            raise Fault("match_overlap", "Hunks must be source-ordered and non-overlapping in the original file.")
        matched.append((first, last, hunk))

    output, windows, position = [], [], 0
    for first, last, hunk in matched:
        output.extend((payload, ending, index) for index, (payload, ending) in
                      enumerate(lines[position:first], position))
        new_first = len(output)
        cursor = first
        for prefix, payload in hunk["body"]:
            if prefix == "+":
                output.append((payload, separator, None))
            else:
                if prefix == " ":
                    output.append((*lines[cursor], cursor))
                cursor += 1
        windows.append((first, last, new_first, len(output)))
        position = last
    output.extend((payload, ending, index) for index, (payload, ending) in enumerate(lines[position:], position))

    chunks, after_offsets, terminator_changes = [bom], [len(bom)], []
    for index, (payload, original_ending, source_index) in enumerate(output):
        ending = original_ending or separator
        if index == len(output) - 1 and not final_newline:
            ending = b""
        size = after_offsets[-1] + len(payload) + len(ending)
        if size > max_bytes:
            raise Fault("source_limit", "The prepared source or aggregate before/after budget is exceeded.")
        chunks.extend((payload, ending))
        after_offsets.append(size)
        if source_index is not None and ending != original_ending:
            terminator_changes.append((source_index, source_index + 1, index, index + 1))
    if len(bom) > max_bytes:
        raise Fault("source_limit", "The prepared source exceeds its remaining byte budget.")
    ranges = []
    for first, last, new_first, new_last in sorted(windows + terminator_changes):
        span = {"old_start": before_offsets[first], "old_end": before_offsets[last],
                "new_start": after_offsets[new_first], "new_end": after_offsets[new_last]}
        if ranges and (span["old_start"] <= ranges[-1]["old_end"] or span["new_start"] <= ranges[-1]["new_end"]):
            ranges[-1]["old_end"] = max(ranges[-1]["old_end"], span["old_end"])
            ranges[-1]["new_end"] = max(ranges[-1]["new_end"], span["new_end"])
        else:
            ranges.append(span)
    return b"".join(chunks), ranges


def prepare(operation: dict, raw: bytes | None, max_bytes: int) -> tuple[bytes | None, list[dict]]:
    """Return complete proposed bytes and raw ranges, never effects or IO."""
    kind = operation["kind"]
    if kind == "add":
        if raw is not None:
            raise Fault("destination_exists", "An add requires an absent destination.")
        size = sum(len(line) + 1 for line in operation["lines"])
        if size > max_bytes:
            raise Fault("source_limit", "The prepared source exceeds its remaining byte budget.")
        result = b"".join(line + b"\n" for line in operation["lines"])
        return result, [{"old_start": 0, "old_end": 0, "new_start": 0, "new_end": size}]
    if raw is None:
        raise Fault("source_missing", "An update, move or delete requires an existing text file.")
    if kind == "delete":
        return None, [{"old_start": 0, "old_end": len(raw), "new_start": 0, "new_end": 0}]
    if kind == "move" and not operation["hunks"]:
        if len(raw) > max_bytes:
            raise Fault("source_limit", "The moved source exceeds its remaining byte budget.")
        return raw, []
    return _update(raw, operation["hunks"], max_bytes)
