"""Bounded, safe public errors and best-effort output redaction."""

import re


class Fault(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def redact(text: str) -> str:
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                  "[REDACTED PRIVATE KEY]", text, flags=re.S)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})",
                  "[REDACTED]", text)
    return re.sub(r"(?i)\b((?:password|secret|token|api[_-]?key)\s*[:=]\s*)[^\s,;]+",
                  r"\1[REDACTED]", text)
