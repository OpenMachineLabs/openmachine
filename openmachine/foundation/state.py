"""Shared private-directory, durable JSON and cooperative file-lock mechanics."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

from openmachine.foundation.sanitize import Fault


def private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise Fault("invalid_state", "The state directory must be an absolute path.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Fault("unsafe_state", "State directories must be owned by this user with mode 0700.")
    return path.resolve()


def save_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def lease(path: Path, *, shared: bool = False, wait: bool = False, create: bool = True):
    flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as lease:
        try:
            fcntl.flock(lease, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise Fault("resource_busy", "An owned operation already holds this execution or worktree.") from error
        # Close only: an inherited descriptor must keep its lock after launch.
        yield lease
