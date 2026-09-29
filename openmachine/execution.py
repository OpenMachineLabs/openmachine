"""Per-execution workers and durable receipts, using only the standard library."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING

from openmachine import __version__
from openmachine.sanitize import Fault, redact
from openmachine.workspace import context

if TYPE_CHECKING:
    from openmachine.config import Settings

MAX_LOG = 4 * 1024 * 1024
MAX_READ = 65536
MAX_TIMEOUT = 600
TERMINAL = {"succeeded", "failed", "timed_out"}


def private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise Fault("invalid_state", "The state directory must be an absolute path.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Fault("unsafe_state", "State directories must be owned by this user with mode 0700.")
    return path.resolve()


def _save(path: Path, value: dict) -> None:
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


def _load(directory: Path) -> dict:
    try:
        with (directory / "record.json").open() as stream:
            record = json.load(stream)
        if record["status"] not in TERMINAL | {"accepted", "preparing", "running"}:
            raise ValueError("invalid state")
        code = record["exit_code"]
        if code is not None and type(code) is not int:
            raise ValueError("invalid exit code")
        if record["status"] == "succeeded" and code != 0:
            raise ValueError("missing successful exit")
        if not isinstance(record["request"], dict) or not isinstance(record["context"], dict):
            raise ValueError("invalid receipt")
        request = record["request"]
        if not all(isinstance(request[key], str) for key in ("machine", "cwd", "expected_head_sha")):
            raise ValueError("invalid request identity")
        if not isinstance(request["argv"], list) or not request["argv"] or not all(isinstance(a, str) for a in request["argv"]):
            raise ValueError("missing command")
        if type(request["timeout_seconds"]) is not int or not 1 <= request["timeout_seconds"] <= MAX_TIMEOUT:
            raise ValueError("invalid timeout")
        if not isinstance(record["version"], str) or any(
            type(record[key]) is not bool for key in ("log_truncated", "output_incomplete")
        ):
            raise ValueError("invalid receipt metadata")
        if record["status"] in TERMINAL and not isinstance(record["finished_at"], (int, float)):
            raise ValueError("missing terminal receipt")
        return record
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Fault("corrupt_state", "The original execution receipt is missing or invalid; do not relaunch.") from error


@contextmanager
def _lease(path: Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Fault("resource_busy", "An owned operation already holds this execution or worktree.") from error
        # Close only: an inherited descriptor must keep its lock after launch.
        yield lease


def _directory(state: Path, execution_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", execution_id):
        raise Fault("invalid_execution_id", "Use a 32-character lowercase hexadecimal execution ID.")
    return state / "executions" / execution_id


def start(config: "Settings", state: Path, machine: str, execution_id: str, cwd: str,
          argv: list[str], timeout_seconds: int = MAX_TIMEOUT, expected_head_sha: str = "") -> dict:
    config.machine(machine, write=True)
    if not argv or not argv[0] or len(argv) > 100 or any("\0" in a or len(a) > 4096 for a in argv):
        raise Fault("invalid_command", "Supply a program and at most 99 arguments, each at most 4096 characters.")
    if not 1 <= timeout_seconds <= MAX_TIMEOUT:
        raise Fault("invalid_timeout", "This increment supports 1-600 seconds per command.")
    if expected_head_sha and not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
        raise Fault("invalid_head", "expected_head_sha must be a full lowercase Git commit SHA.")
    if not Path(cwd).is_absolute():
        raise Fault("invalid_cwd", "Select an existing absolute working directory.")
    working = Path(cwd).resolve(strict=True)
    if not working.is_dir() or not any(
        working.is_relative_to(Path(item.root).resolve())
        for item in config.workspaces.values() if item.machine == machine
    ):
        raise Fault("invalid_cwd", "The working directory must belong to a configured workspace on this machine.")
    state = private_directory(state)
    private_directory(state / "executions")
    locks = private_directory(state / "locks")
    directory = _directory(state, execution_id)
    request = {"machine": machine, "cwd": str(working), "argv": argv,
               "timeout_seconds": timeout_seconds, "expected_head_sha": expected_head_sha}
    with _lease(locks / f"{execution_id}.lock"):
        if directory.exists():
            if _load(directory)["request"] != request:
                raise Fault("execution_conflict", "This execution ID already belongs to a different request.")
            return read(config, state, machine, execution_id)
        observed = context(working)
        key = hashlib.sha256(observed["worktree"].encode()).hexdigest()
        with _lease(locks / f"workspace-{key}.lock") as workspace_lease:
            if expected_head_sha and observed["head_sha"] != expected_head_sha:
                raise Fault("context_mismatch", "Git HEAD changed or could not be verified; inspect it before proceeding.")
            private_directory(directory)
            record = {"request": request, "context": observed, "version": __version__,
                      "status": "accepted", "exit_code": None, "created_at": time.time(),
                      "log_truncated": False, "output_incomplete": False}
            _save(directory / "record.json", record)
            with _lease(directory / "worker.lock") as worker_lease:
                try:
                    process = subprocess.Popen(
                        [sys.executable, "-I", "-m", "openmachine.execution", str(directory),
                         str(workspace_lease.fileno()), str(worker_lease.fileno())],
                        cwd=state, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                        pass_fds=(workspace_lease.fileno(), worker_lease.fileno()),
                    )
                    threading.Thread(target=process.wait, daemon=True).start()
                except OSError as error:
                    record.update(status="failed", error="worker_launch_failed", finished_at=time.time())
                    _save(directory / "record.json", record)
                    raise Fault("worker_launch_failed", "The worker could not start; inspect this execution ID.") from error
    return read(config, state, machine, execution_id)


def read(config: "Settings", state: Path, machine: str, execution_id: str,
         offset: int = 0, max_bytes: int = MAX_READ) -> dict:
    config.machine(machine)
    directory = _directory(state, execution_id)
    if not directory.exists():
        raise Fault("execution_not_found", "No receipt exists for this execution ID on this store.")
    record = _load(directory)
    if record["request"]["machine"] != machine:
        raise Fault("context_mismatch", "Read the execution using its original machine identity.")
    status = record["status"]
    if status not in TERMINAL:
        try:
            with (directory / "worker.lock").open("rb") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status = "outcome_unknown"
        except BlockingIOError:
            pass
        except OSError:
            status = "outcome_unknown"
    if offset < 0 or not 1 <= max_bytes <= MAX_READ:
        raise Fault("invalid_range", "Use a nonnegative byte offset and at most 65536 bytes.")
    output = directory / "output.log"
    data, size = b"", 0
    if output.exists():
        with output.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if offset > size:
                raise Fault("invalid_range", "The offset is beyond retained output.")
            stream.seek(offset)
            data = stream.read(max_bytes)
    elif offset:
        raise Fault("invalid_range", "No output exists at that byte offset.")
    return {"execution_id": execution_id, "machine": machine, "status": status,
            "exit_code": record["exit_code"], "context": record["context"], "version": record["version"],
            "error": record.get("error"), "output": redact(data.decode("utf-8", errors="replace")),
            "offset": offset, "next_offset": offset + len(data), "retained_bytes": size,
            "has_more": offset + len(data) < size, "log_truncated": record["log_truncated"],
            "output_incomplete": record["output_incomplete"],
            "output_unavailable": not output.exists() and status in TERMINAL | {"running"}}


def _terminate(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def worker(directory: Path, workspace_fd: int, worker_fd: int) -> None:
    os.fstat(workspace_fd)
    os.fstat(worker_fd)
    record = _load(directory)
    request = record["request"]
    process = None
    terminated = False
    try:
        record.update(status="preparing", context=context(Path(request["cwd"])))
        _save(directory / "record.json", record)
        if request["expected_head_sha"] and record["context"]["head_sha"] != request["expected_head_sha"]:
            raise Fault("context_mismatch", "Git HEAD changed before command launch.")
        fd = os.open(directory / "output.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb", buffering=0) as output, selectors.DefaultSelector() as selector:
            process = subprocess.Popen(request["argv"], cwd=request["cwd"], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True, pass_fds=(workspace_fd,))
            record.update(status="running", started_at=time.time())
            _save(directory / "record.json", record)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + request["timeout_seconds"]
            drain_deadline = None
            retained = 0
            timed_out = False
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if drain_deadline is None and (process.poll() is not None or now >= deadline):
                    timed_out = process.poll() is None
                    _terminate(process)
                    terminated = True
                    drain_deadline = time.monotonic() + 1
                if drain_deadline is not None and now >= drain_deadline:
                    record["output_incomplete"] = bool(selector.get_map())
                    break
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, MAX_READ)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    kept = chunk[:MAX_LOG - retained]
                    output.write(kept)
                    retained += len(kept)
                    if len(kept) < len(chunk) and not record["log_truncated"]:
                        record["log_truncated"] = True
                        _save(directory / "record.json", record)
            code = process.wait()
            process.stdout.close()
            os.fsync(output.fileno())
        record.update(exit_code=code, status="timed_out" if timed_out else "succeeded" if code == 0 else "failed")
    except (OSError, ValueError, Fault, subprocess.SubprocessError) as error:
        record.update(status="failed", error=error.code if isinstance(error, Fault) else type(error).__name__)
    finally:
        if process is not None:
            if not terminated:
                _terminate(process)
            process.stdout.close()
    os.close(workspace_fd)
    record["finished_at"] = time.time()
    _save(directory / "record.json", record)


if __name__ == "__main__":
    worker(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]))
