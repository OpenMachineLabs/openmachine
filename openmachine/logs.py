"""Four bounded segments, absolute byte cursors and explicit retention gaps."""

from contextlib import ExitStack
import fcntl
import os
import re
from pathlib import Path

from openmachine.foundation.sanitize import Fault, redact

SEGMENT_BYTES = 1024 * 1024
SEGMENTS = 4
MAX_BYTES = SEGMENT_BYTES * SEGMENTS
MAX_READ = 65536


class Writer:
    """One worker owns this stream; readers never change its files."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.total = 0
        self.stream = None
        self.lock = None

    def __enter__(self):
        fd = os.open(self.directory / "output.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        self.lock = os.fdopen(fd, "r+b")
        try:
            self._segment()
        except OSError:
            self.lock.close()
            raise
        return self

    def _segment(self) -> None:
        if self.stream is not None:
            os.fsync(self.stream.fileno())
            self.stream.close()
        # Remove the expired segment first: disk retention never exceeds four.
        if self.total >= MAX_BYTES:
            (self.directory / f"output-{self.total - MAX_BYTES:020d}.log").unlink()
        path = self.directory / f"output-{self.total:020d}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(fd, "wb", buffering=0)

    def write(self, data: bytes) -> None:
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            remaining = memoryview(data)
            while remaining:
                if self.total and self.total % SEGMENT_BYTES == 0:
                    self._segment()
                count = min(len(remaining), SEGMENT_BYTES - self.total % SEGMENT_BYTES)
                written = self.stream.write(remaining[:count])
                if not written:
                    raise OSError("Log write made no progress")
                self.total += written
                remaining = remaining[written:]
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)

    def __exit__(self, *_):
        try:
            if self.stream is not None and not self.stream.closed:
                try:
                    os.fsync(self.stream.fileno())
                finally:
                    self.stream.close()
        finally:
            self.lock.close()


def read(directory: Path, offset: int, max_bytes: int, *, legacy: bool = False) -> dict:
    if offset < 0 or not 1 <= max_bytes <= MAX_READ:
        raise Fault("invalid_range", "Use a nonnegative byte offset and at most 65536 bytes.")
    with ExitStack() as stack:
        if legacy:
            paths = [(0, directory / "output.log")] if (directory / "output.log").exists() else []
        elif (directory / "output.lock").exists():
            lock = stack.enter_context((directory / "output.lock").open("rb"))
            fcntl.flock(lock, fcntl.LOCK_SH)
            paths = []
            for path in directory.glob("output-*.log"):
                match = re.fullmatch(r"output-([0-9]{20})\.log", path.name)
                if match is None or len(paths) == SEGMENTS:
                    raise Fault("corrupt_output", "Unexpected log segments; preserve the original execution.")
                paths.append((int(match[1]), path))
            paths.sort()
        else:
            paths = []
        streams = [(start, stack.enter_context(path.open("rb"))) for start, path in paths]
        sizes = [os.fstat(stream.fileno()).st_size for _, stream in streams]
        if not legacy and streams and (any(start % SEGMENT_BYTES for start, _ in streams)
                or any(size > SEGMENT_BYTES for size in sizes)
                or any(b[0] != a[0] + SEGMENT_BYTES for a, b in zip(streams, streams[1:]))
                or any(size != SEGMENT_BYTES for size in sizes[:-1])):
            raise Fault("corrupt_output", "Non-contiguous retained output; do not claim complete logs.")
        start = streams[0][0] if streams else 0
        end = streams[-1][0] + sizes[-1] if streams else 0
        if offset > end:
            raise Fault("invalid_range", "The offset is beyond current output.")
        position = max(offset, start)
        data = bytearray()
        for (base, stream), size in zip(streams, sizes):
            if position >= base + size:
                continue
            stream.seek(position - base)
            chunk = stream.read(min(max_bytes - len(data), base + size - position))
            data.extend(chunk)
            position += len(chunk)
            if len(data) == max_bytes:
                break
    return {"output": redact(data.decode("utf-8", errors="replace")), "offset": offset,
            "next_offset": position, "log_start_offset": start, "log_bytes": end,
            "retained_bytes": sum(sizes), "has_more": position < end,
            "output_gap": offset < start, "output_unavailable": not streams,
            "log_retention": "prefix_only" if legacy else "rotating"}
