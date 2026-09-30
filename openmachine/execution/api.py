"""Execution admission, receipt observation and cancellation."""

from contextlib import ExitStack
import hashlib
import heapq
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

from openmachine import __version__
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.workspace.git import context

from . import logs
from .logs import MAX_READ
from .records import MAX_TIMEOUT, TERMINAL, read_record

if TYPE_CHECKING:
    from openmachine.config import Settings

DEFAULT_TIMEOUT = 600


def _directory(state: Path, execution_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", execution_id):
        raise Fault("invalid_execution_id", "Use a 32-character lowercase hexadecimal execution ID.")
    return state / "executions" / execution_id


def start(config: "Settings", state: Path, machine: str, execution_id: str, cwd: str,
          argv: list[str], timeout_seconds: int = DEFAULT_TIMEOUT, expected_head_sha: str = "",
          workspace_access: str = "exclusive", resource_keys: list[str] | None = None, task_id: str = "",
          *, remote_worker: Path | None = None) -> dict:
    target = config.machine(machine, write=True)
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
    if target.transport == "ssh":
        from integrations.ssh import call
        return call(config, state, machine, "start_execution", execution_id=execution_id, cwd=cwd, argv=argv,
                    timeout_seconds=timeout_seconds, expected_head_sha=expected_head_sha,
                    workspace_access=workspace_access, resource_keys=keys, task_id=task_id)
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
    with lease(locks / f"{execution_id}.lock"):
        if directory.exists():
            original = {"workspace_access": "exclusive", "resource_keys": [], "task_id": "", **read_record(directory)["request"]}
            if original != request:
                raise Fault("execution_conflict", "This execution ID already belongs to a different request.")
            return read(config, state, machine, execution_id)
        if task_id and remote_worker is None:
            from openmachine.tasks import read as read_task
            read_task(state, task_id)  # Verify a reference, not permission or task completion.
        observed = context(working)
        if observed["git_status"] == "unavailable":
            raise Fault("context_unavailable", "Git context could not be checked; do not bypass workspace coordination.")
        key = hashlib.sha256(observed["worktree"].encode()).hexdigest()
        with ExitStack() as leases:
            owned = [leases.enter_context(lease(locks / f"workspace-{key}.lock", shared=workspace_access == "shared"))]
            for name in request["resource_keys"]:
                digest = hashlib.sha256(name.encode()).hexdigest()
                owned.append(leases.enter_context(lease(locks / f"resource-{digest}.lock")))
            if expected_head_sha and observed["head_sha"] != expected_head_sha:
                raise Fault("context_mismatch", "Git HEAD changed or could not be verified; inspect it before proceeding.")
            private_directory(directory)
            record = {"request": request, "context": observed, "version": __version__, "format_version": 2,
                      "status": "accepted", "exit_code": None, "created_at": time.time(),
                      "log_truncated": False, "output_incomplete": False}
            if remote_worker is not None:
                record.update(format_version=3, scope_unit=f"openmachine-{execution_id}.scope")
                observed.update(target_identity=machine, worker_archive=str(remote_worker),
                                worker_sha256=remote_worker.stem)
            with lease(directory / "control.lock"), lease(directory / "worker.lock") as worker_lease:
                save_json(directory / "record.json", record)
                fds = [lease.fileno() for lease in owned]
                launcher = [sys.executable, "-I", "-m", "openmachine.execution"]
                if remote_worker is not None:
                    launcher = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--no-ask-password",
                                "--expand-environment=no", "--unit=" + record["scope_unit"],
                                "--property=TimeoutStopSec=2s", f"--property=RuntimeMaxSec={timeout_seconds + 30}s",
                                sys.executable, "-I", str(remote_worker), "worker"]
                try:
                    process = subprocess.Popen(
                        [*launcher, str(directory), str(worker_lease.fileno()), *(str(fd) for fd in fds)],
                        cwd=state, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                        pass_fds=(*fds, worker_lease.fileno()),
                    )
                    threading.Thread(target=process.wait, daemon=True).start()
                except OSError as error:
                    record.update(status="failed", error="worker_launch_failed", finished_at=time.time())
                    save_json(directory / "record.json", record)
                    raise Fault("worker_launch_failed", "The worker could not start; inspect this execution ID.") from error
    return read(config, state, machine, execution_id)


def _receipt(directory: Path, machine: str) -> tuple[dict, str]:
    if not directory.exists():
        raise Fault("execution_not_found", "No receipt exists for this execution ID on this store.")
    with ExitStack() as stack:
        if (directory / "control.lock").exists():
            stack.enter_context(lease(directory / "control.lock", shared=True, wait=True, create=False))
        record = read_record(directory)
        if record.get("format_version", 1) >= 2 and not (directory / "control.lock").is_file():
            raise Fault("corrupt_state", "Execution control state is missing; preserve the original record.")
        if record["request"]["machine"] != machine:
            raise Fault("context_mismatch", "Use the original machine identity for this execution.")
        status = record["status"]
        if status not in TERMINAL:
            try:
                with lease(directory / "worker.lock", create=False):
                    status = "outcome_unknown"
            except Fault as error:
                if error.code != "resource_busy":
                    raise
            except OSError:
                status = "outcome_unknown"
        if status in TERMINAL and record.get("format_version") == 3:
            try:
                if _scope_state(record["scope_unit"]) not in {"inactive", "not-found"}:
                    status = "finishing"
            except Fault:
                status = "outcome_unknown"
        return record, status


def _scope_state(unit: str) -> str:
    try:
        result = subprocess.run(["systemctl", "--user", "--no-ask-password", "show", unit,
                                 "--property=LoadState", "--property=ActiveState"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Fault("scope_unavailable", "The original user scope cannot be verified.") from error
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields.get("LoadState") == "not-found":
        return "not-found"
    if result.returncode or fields.get("ActiveState") not in {"active", "activating", "deactivating", "inactive", "failed"}:
        raise Fault("scope_unavailable", "The original user scope cannot be verified.")
    return fields["ActiveState"]


def read(config: "Settings", state: Path, machine: str, execution_id: str,
         offset: int = 0, max_bytes: int = MAX_READ) -> dict:
    if config.machine(machine).transport == "ssh":
        from integrations.ssh import call
        return call(config, state, machine, "read_execution", execution_id=execution_id, offset=offset, max_bytes=max_bytes)
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
    if config.machine(machine).transport == "ssh":
        from integrations.ssh import call
        return call(config, state, machine, "list_executions", cursor=cursor, limit=limit)
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
    if config.machine(machine).transport == "ssh":
        from integrations.ssh import call
        return call(config, state, machine, "cancel_execution", execution_id=execution_id)
    directory = _directory(state, execution_id)
    record, status = _receipt(directory, machine)
    if status == "finishing":
        # Only a mutation tool may request cleanup again; reads never stop a scope.
        try:
            subprocess.run(["systemctl", "--user", "--no-ask-password", "--no-block", "stop", record["scope_unit"]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise Fault("outcome_unknown", "Scope cleanup is unverified; read the original execution.") from error
        return read(config, state, machine, execution_id)
    if status in TERMINAL:
        return read(config, state, machine, execution_id)
    if status == "outcome_unknown":
        raise Fault("outcome_unknown", "Worker ownership is unverified; cancellation cannot claim termination.")
    if record.get("format_version", 1) < 2:
        raise Fault("unsupported_operation", "This legacy worker does not support cancellation; preserve its runtime.")
    with lease(directory / "control.lock", wait=True, create=False):
        # Only the worker publishes execution status. A repeated request is a no-op.
        record = read_record(directory)
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
