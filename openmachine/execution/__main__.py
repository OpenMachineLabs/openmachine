"""Retain the installed per-execution process entry point."""

from pathlib import Path
import sys

from .worker import worker

if __name__ == "__main__":
    worker(Path(sys.argv[1]), *(int(fd) for fd in sys.argv[2:]))
