"""Shared bounded text resources; callers retain workspace and Skill policy."""

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import stat

from openmachine.foundation.sanitize import Fault, redact


MAX_SCAN_ENTRIES = 2000
MAX_TEXT_FILE = 131072
TEXT_PAGE = 32768
SECRET_NAMES = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".netrc", ".pypirc",
                "credentials", "credentials.json", "secrets.toml", "id_rsa", "id_ed25519"}


def excluded(path: Path) -> bool:
    return any(part in SECRET_NAMES or part == ".env" or part.startswith(".env.")
               or part.endswith((".pem", ".key", ".p12", ".pfx")) for part in path.parts)


def file_parts(path: str) -> tuple[str, ...]:
    """Validate a single reader/editor resource without broadening workspace scope."""
    parts = path.split("/")
    if (not path or "\\" in path or any(ord(c) < 32 for c in path)
            or any(p in {"", ".", ".."} for p in parts)
            or excluded(Path(*(p.casefold() for p in parts)))
            or any(p.casefold() == ".git" for p in parts)):
        raise Fault("path_denied", "Use a relative non-credential file path without traversal or Git control files.")
    return tuple(parts)


@contextmanager
def file_parent(root: Path, parts: tuple[str, ...], *, identities: list | None = None):
    """Keep parent directories open and refuse every descendant filesystem link."""
    with ExitStack() as stack:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        if identities is not None:
            info = os.fstat(fd)
            identities.append((info.st_dev, info.st_ino))
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, fd)
            if identities is not None:
                info = os.fstat(fd)
                identities.append((info.st_dev, info.st_ino))
        yield fd


def file_bytes(parent: int, name: str) -> tuple[bytes | None, os.stat_result | None]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_TEXT_FILE:
            raise Fault("invalid_file", "Expected a bounded regular text file without hardlinks.")
        raw = stream.read(MAX_TEXT_FILE + 1)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise Fault("source_changed", "The file changed while reading; inspect its current version.")
    if len(raw) > MAX_TEXT_FILE or b"\0" in raw:
        raise Fault("invalid_file", "Expected at most 128 KiB of UTF-8 text without NUL bytes.")
    try:
        raw.decode("utf-8")
    except UnicodeError as error:
        raise Fault("invalid_file", "The selected file is not UTF-8 text.") from error
    return raw, after


def text_page(raw: bytes, offset: int = 0) -> dict:
    if (type(offset) is not int or not 0 <= offset <= len(raw)
            or (offset < len(raw) and raw[offset] & 0xC0 == 0x80)):
        raise Fault("invalid_range", "Use the returned next_offset at a UTF-8 character boundary.")
    end = min(offset + TEXT_PAGE, len(raw))
    while end < len(raw) and raw[end] & 0xC0 == 0x80:
        end -= 1
    text = raw[offset:end].decode("utf-8")
    clean = redact(text)
    return {"text": clean, "content_redacted": clean != text, "offset": offset,
            "next_offset": end, "has_more": end < len(raw), "size_bytes": len(raw)}
