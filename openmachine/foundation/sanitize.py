"""Bounded, safe public errors and best-effort output redaction."""

import re


PRE_EFFECT_REQUEST_FAILURES = frozenset({
    "invalid_change_id", "invalid_content", "invalid_edits", "invalid_patch",
    "invalid_precondition", "path_conflict", "request_limit",
})


class Fault(Exception):
    def __init__(self, code: str, message: str, evidence: dict | None = None, *, effect: str | None = None):
        super().__init__(message)
        if effect not in {None, "known_no_effect", "effect_unknown"}:
            raise ValueError("invalid owner effect classification")
        self.code = code
        self.evidence = evidence
        self.effect = effect


_REDACTIONS = (
    (re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)", re.S),
     0, "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})"), 0, "[REDACTED]"),
    (re.compile(r"(?i)\b((?:password|secret|token|api[_-]?key)\s*[:=]\s*)([^\s,;]+)"), 2, "[REDACTED]"),
)


def redact(text: str) -> str:
    for pattern, group, replacement in _REDACTIONS:
        text = pattern.sub(lambda match: (match.group(1) if group else "") + replacement, text)
    return text


def redaction_spans(text: str) -> list[tuple[int, int, str]]:
    """Locate original character ranges once for repeated windows over one source."""
    return sorted((match.start(group), match.end(group), replacement)
                  for pattern, group, replacement in _REDACTIONS for match in pattern.finditer(text))


def redact_slice(text: str, start: int, end: int, *, spans: list | None = None) -> str:
    """Mask a character window using matches in the original bounded source."""
    if start == end:
        return ""
    result, cursor = [], start
    for first, last, replacement in redaction_spans(text) if spans is None else spans:
        if first >= end:
            break
        if last <= start:
            continue
        first, last = max(first, start), min(last, end)
        if first >= cursor:
            result.extend((text[cursor:first], replacement))
        cursor = max(cursor, last)
    result.append(text[cursor:end])
    return "".join(result)
