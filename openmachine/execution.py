"""Per-execution workers and durable receipts, using only the standard library."""

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import heapq
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

from openmachine import __version__, logs
from openmachine.sanitize import Fault
from openmachine.workspace import context

if TYPE_CHECKING:
    from openmachine.config import Settings

MAX_LOG = logs.MAX_BYTES
MAX_READ = logs.MAX_READ
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 86400
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}


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
        if record.get("format_version", 1) not in (1, 2):
            raise Fault("unsupported_state_version", "This runtime cannot read the execution receipt version.")
        code = record["exit_code"]
        if code is not None and type(code) is not int:
            raise ValueError("invalid exit code")
        if record["status"] == "succeeded" and code != 0:
            raise ValueError("missing successful exit")
        if not isinstance(record["request"], dict) or not isinstance(record["context"], dict):
            raise ValueError("invalid receipt")
        request = record["request"]
        linked_task = request.get("task_id", "")
        if not isinstance(linked_task, str) or (linked_task and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", linked_task)):
            raise ValueError("invalid task reference")
        if not all(isinstance(request[key], str) for key in ("machine", "cwd", "expected_head_sha")):
            raise ValueError("invalid request identity")
        if not isinstance(request["argv"], list) or not request["argv"] or not all(isinstance(a, str) for a in request["argv"]):
            raise ValueError("missing command")
        if type(request["timeout_seconds"]) is not int or not 1 <= request["timeout_seconds"] <= MAX_TIMEOUT:
            raise ValueError("invalid timeout")
        if not isinstance(record["created_at"], (int, float)):
            raise ValueError("missing creation time")
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
def _lease(path: Path, *, shared: bool = False, wait: bool = False, create: bool = True):
    flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as lease:
        try:
            fcntl.flock(lease, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise Fault("resource_busy", "An owned operation already holds this execution or worktree.") from error
        # Close only: an inherited descriptor must keep its lock after launch.
        yield lease


def _directory(state: Path, execution_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", execution_id):
        raise Fault("invalid_execution_id", "Use a 32-character lowercase hexadecimal execution ID.")
    return state / "executions" / execution_id


def start(config: "Settings", state: Path, machine: str, execution_id: str, cwd: str,
          argv: list[str], timeout_seconds: int = DEFAULT_TIMEOUT, expected_head_sha: str = "",
          workspace_access: str = "exclusive", resource_keys: list[str] | None = None, task_id: str = "") -> dict:
    config.machine(machine, write=True)
    if task_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
        raise Fault("invalid_task_id", "Supply the original bounded task ID, or leave the reference empty.")
    if not argv or not argv[0] or len(argv) > 100 or any("\0" in a or len(a) > 4096 for a in argv):
        raise Fault("invalid_command", "Supply a program and at most 99 arguments, each at most 4096 characters.")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT:
        raise Fault("invalid_timeout", "Use 1-86400 seconds; the default remains 600 seconds.")
    if workspace_access not in ("exclusive", "shared"):
        raise Fault("invalid_access", "Use exclusive, or shared only for cooperating read-only commands.")
    keys = resource_keys or []
    if len(keys) > 16 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", k) for k in keys):
        raise Fault("invalid_resources", "Use at most 16 stable resource names of 1-128 letters, digits, _, ., :, or -.")
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
               "timeout_seconds": timeout_seconds, "expected_head_sha": expected_head_sha,
               "workspace_access": workspace_access, "resource_keys": sorted(set(keys)), "task_id": task_id}
    with _lease(locks / f"{execution_id}.lock"):
        if directory.exists():
            original = {"workspace_access": "exclusive", "resource_keys": [], "task_id": "", **_load(directory)["request"]}
            if original != request:
                raise Fault("execution_conflict", "This execution ID already belongs to a different request.")
            return read(config, state, machine, execution_id)
        if task_id:
            from openmachine.tasks import read as read_task
            read_task(state, task_id)  # Verify a reference, not permission or task completion.
        observed = context(working)
        if observed["git_status"] == "unavailable":
            raise Fault("context_unavailable", "Git context could not be checked; do not bypass workspace coordination.")
        key = hashlib.sha256(observed["worktree"].encode()).hexdigest()
        with ExitStack() as leases:
            owned = [leases.enter_context(_lease(locks / f"workspace-{key}.lock", shared=workspace_access == "shared"))]
            for name in request["resource_keys"]:
                digest = hashlib.sha256(name.encode()).hexdigest()
                owned.append(leases.enter_context(_lease(locks / f"resource-{digest}.lock")))
            if expected_head_sha and observed["head_sha"] != expected_head_sha:
                raise Fault("context_mismatch", "Git HEAD changed or could not be verified; inspect it before proceeding.")
            private_directory(directory)
            record = {"request": request, "context": observed, "version": __version__, "format_version": 2,
                      "status": "accepted", "exit_code": None, "created_at": time.time(),
                      "log_truncated": False, "output_incomplete": False}
            with _lease(directory / "control.lock"), _lease(directory / "worker.lock") as worker_lease:
                _save(directory / "record.json", record)
                fds = [lease.fileno() for lease in owned]
                try:
                    process = subprocess.Popen(
                        [sys.executable, "-I", "-m", "openmachine.execution", str(directory),
                         str(worker_lease.fileno()), *(str(fd) for fd in fds)],
                        cwd=state, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                        pass_fds=(*fds, worker_lease.fileno()),
                    )
                    threading.Thread(target=process.wait, daemon=True).start()
                except OSError as error:
                    record.update(status="failed", error="worker_launch_failed", finished_at=time.time())
                    _save(directory / "record.json", record)
                    raise Fault("worker_launch_failed", "The worker could not start; inspect this execution ID.") from error
    return read(config, state, machine, execution_id)


def _receipt(directory: Path, machine: str) -> tuple[dict, str]:
    if not directory.exists():
        raise Fault("execution_not_found", "No receipt exists for this execution ID on this store.")
    with ExitStack() as stack:
        if (directory / "control.lock").exists():
            stack.enter_context(_lease(directory / "control.lock", shared=True, wait=True, create=False))
        record = _load(directory)
        if record.get("format_version", 1) == 2 and not (directory / "control.lock").is_file():
            raise Fault("corrupt_state", "Execution control state is missing; preserve the original record.")
        if record["request"]["machine"] != machine:
            raise Fault("context_mismatch", "Use the original machine identity for this execution.")
        status = record["status"]
        if status not in TERMINAL:
            try:
                with _lease(directory / "worker.lock", create=False):
                    status = "outcome_unknown"
            except Fault as error:
                if error.code != "resource_busy":
                    raise
            except OSError:
                status = "outcome_unknown"
        return record, status


def read(config: "Settings", state: Path, machine: str, execution_id: str,
         offset: int = 0, max_bytes: int = MAX_READ) -> dict:
    config.machine(machine)
    directory = _directory(state, execution_id)
    record, status = _receipt(directory, machine)
    output = logs.read(directory, offset, max_bytes, legacy=record.get("format_version", 1) == 1)
    return {"execution_id": execution_id, "machine": machine, "status": status,
            "exit_code": record["exit_code"], "context": record["context"], "version": record["version"],
            "created_at": record["created_at"], "finished_at": record.get("finished_at"),
            "task_id": record["request"].get("task_id", ""),
            "workspace_access": record["request"].get("workspace_access", "exclusive"),
            "resource_keys": record["request"].get("resource_keys", []),
            "cancel_requested": (directory / "cancel.request").is_file(),
            "error": record.get("error"), **output,
            "log_truncated": record["log_truncated"] or output["log_start_offset"] > 0,
            "output_incomplete": record["output_incomplete"]}


def list_records(config: "Settings", state: Path, machine: str, cursor: str = "", limit: int = 20) -> dict:
    """Page stable IDs, not guessed logical tasks; inspect at most limit receipts."""
    config.machine(machine)
    if cursor:
        _directory(state, cursor)
    if not 1 <= limit <= 100:
        raise Fault("invalid_range", "Use a page size from 1 to 100.")
    root = state / "executions"
    if not root.exists():
        return {"machine": machine, "executions": [], "next_cursor": None, "unreadable": [], "ordering": "execution_id"}
    with os.scandir(root) as entries:
        names = heapq.nsmallest(limit + 1, (e.name for e in entries
                               if re.fullmatch(r"[0-9a-f]{32}", e.name) and e.name > cursor))
    items, unreadable = [], []
    for name in names[:limit]:
        try:
            record, status = _receipt(root / name, machine)
            items.append({"execution_id": name, "machine": machine, "status": status,
                          "context": record["context"], "created_at": record["created_at"],
                          "exit_code": record["exit_code"], "version": record["version"],
                          "task_id": record["request"].get("task_id", "")})
        except (Fault, OSError) as error:
            if isinstance(error, Fault) and error.code == "context_mismatch":
                continue
            unreadable.append({"execution_id": name, "error": error.code if isinstance(error, Fault) else "source_unavailable"})
    return {"machine": machine, "executions": items, "next_cursor": names[limit - 1] if len(names) > limit else None,
            "unreadable": unreadable, "ordering": "execution_id"}


def cancel(config: "Settings", state: Path, machine: str, execution_id: str) -> dict:
    config.machine(machine)
    directory = _directory(state, execution_id)
    record, status = _receipt(directory, machine)
    if status in TERMINAL:
        return read(config, state, machine, execution_id)
    if status == "outcome_unknown":
        raise Fault("outcome_unknown", "Worker ownership is unverified; cancellation cannot claim termination.")
    if record.get("format_version", 1) != 2:
        raise Fault("unsupported_operation", "This legacy worker does not support cancellation; preserve its runtime.")
    with _lease(directory / "control.lock", wait=True, create=False):
        # Only the worker publishes execution status. A repeated request is a no-op.
        record = _load(directory)
        if record["status"] not in TERMINAL:
            try:
                fd = os.open(directory / "cancel.request", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(b"cancel\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    return read(config, state, machine, execution_id)


def _terminate(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
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


def worker(directory: Path, worker_fd: int, *owned_fds: int) -> None:
    for fd in (worker_fd, *owned_fds):
        os.fstat(fd)
    record = _load(directory)
    request = record["request"]
    original_worktree = record["context"]["worktree"]
    process = None
    terminated = False
    interrupted = False

    def interrupt(*_):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        record.update(status="preparing", context=context(Path(request["cwd"])))
        _save(directory / "record.json", record)
        if record["context"]["git_status"] == "unavailable" or record["context"]["worktree"] != original_worktree:
            raise Fault("context_mismatch", "The canonical worktree changed before launch.")
        if request["expected_head_sha"] and record["context"]["head_sha"] != request["expected_head_sha"]:
            raise Fault("context_mismatch", "Git HEAD changed before command launch.")
        with logs.Writer(directory) as output, selectors.DefaultSelector() as selector:
            if interrupted or (directory / "cancel.request").exists():
                record["status"] = "interrupted" if interrupted else "cancelled"
            else:
                process = subprocess.Popen(request["argv"], cwd=request["cwd"], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           start_new_session=True, pass_fds=owned_fds)
                record.update(status="running", started_at=time.time())
                _save(directory / "record.json", record)
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + request["timeout_seconds"]
                drain_deadline = None
                reason = None
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if drain_deadline is None:
                        finished = process.poll() is not None
                        if not finished:
                            reason = ("interrupted" if interrupted else "cancelled" if (directory / "cancel.request").exists()
                                      else "timed_out" if now >= deadline else None)
                        if finished or reason:
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
                        output.write(chunk)
                code = process.wait()
                record.update(exit_code=code, status=reason or ("succeeded" if code == 0 else "failed"))
            record["log_truncated"] = output.total > MAX_LOG
    except (OSError, ValueError, Fault, subprocess.SubprocessError) as error:
        record.update(status="failed", error=error.code if isinstance(error, Fault) else type(error).__name__)
    finally:
        if process is not None:
            if not terminated:
                _terminate(process)
            process.stdout.close()
    # Readers cannot observe the lease-release/publication gap as a lost worker.
    with _lease(directory / "control.lock", wait=True, create=False):
        for fd in (*owned_fds, worker_fd):
            os.close(fd)
        record["finished_at"] = time.time()
        _save(directory / "record.json", record)


if __name__ == "__main__":
    worker(Path(sys.argv[1]), *(int(fd) for fd in sys.argv[2:]))
