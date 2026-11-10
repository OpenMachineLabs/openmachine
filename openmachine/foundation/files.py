"""Bounded directory traversal shared by the folder catalog."""

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path

from openmachine.foundation.sanitize import Fault


MAX_SCAN_ENTRIES = 2000
SECRET_NAMES = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".netrc", ".pypirc",
                "credentials", "credentials.json", "secrets.toml", "id_rsa", "id_ed25519"}


def excluded(path: Path) -> bool:
    return any(part in SECRET_NAMES or part == ".env" or part.startswith(".env.")
               or part.endswith((".pem", ".key", ".p12", ".pfx")) for part in path.parts)


def file_parts(path: str) -> tuple[str, ...]:
    """Validate one relative path below a selected directory."""
    try:
        if not isinstance(path, str):
            raise ValueError
        path.encode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise Fault("path_denied", "Use a valid UTF-8 relative file path.") from error
    parts = path.split("/")
    if (not path or "\\" in path or any(ord(c) < 32 for c in path)
            or any(p in {"", ".", ".."} for p in parts)
            or excluded(Path(*(p.casefold() for p in parts)))
            or any(p.casefold() == ".git" for p in parts)):
        raise Fault("path_denied", "Use a relative non-credential file path without traversal or Git control files.")
    return tuple(parts)


@contextmanager
def file_parent(root: Path, parts: tuple[str, ...], *, identities: list):
    """Keep parent directories open, record their identities and refuse every descendant filesystem link."""
    with ExitStack() as stack:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        identities.append((info.st_dev, info.st_ino))
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            identities.append((info.st_dev, info.st_ino))
        yield fd
