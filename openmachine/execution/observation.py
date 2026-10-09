"""Bounded original receipt/output observation; never admit or replay execution."""

from contextlib import ExitStack
import heapq
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease

from . import control as controls, logs
from .logs import MAX_READ
from .records import TERMINAL, effective_request, is_remote, read_record, record_directory

if TYPE_CHECKING:
    from openmachine.config import Settings

MAX_WAIT = 5
MAX_WAITERS = 16


def receipt(directory: Path, machine: str, *, wait: bool = True, deadline: float | None = None,
             cancelled: threading.Event | None = None) -> tuple[dict, str]:
    if not directory.exists():
        raise Fault("execution_not_found", "No receipt exists for this execution ID on this store.")
    with ExitStack() as stack:
        if (directory / "control.lock").exists():
            stack.enter_context(lease(directory / "control.lock", shared=True, wait=wait, create=False))
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
    # The immutable terminal record no longer needs the publication lock while
    # a potentially slow user-manager query observes its separate cleanup boundary.
    if status in TERMINAL and is_remote(record):
        try:
            cleaned = _scope_state(record["scope_unit"], deadline=deadline, cancelled=cancelled) in {"inactive", "not-found"}
            if not cleaned and status != "outcome_unknown":
                status = "finishing"
            if record.get("format_version", 1) >= 4:
                record = record | {"cleanup": record["cleanup"] | {
                    "status": "verified" if cleaned else "pending", "source": "systemd_scope"}}
        except Fault as error:
            if error.code == "observation_cancelled":
                raise
            status = "outcome_unknown"
            if record.get("format_version", 1) >= 4:
                record = record | {"cleanup": record["cleanup"] | {
                    "status": "unverified", "source": "systemd_scope", "error": "scope_unavailable"}}
    return record, status


def _scope_state(unit: str, *, deadline: float | None = None,
                 cancelled: threading.Event | None = None) -> str:
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + MAX_WAIT)
    try:
        with subprocess.Popen(["systemctl", "--user", "--no-ask-password", "show", unit,
                               "--property=LoadState", "--property=ActiveState"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True) as process:
            try:
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise Fault("observation_cancelled", "Only this observation was cancelled; the command is unchanged.")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        output, _ = process.communicate(timeout=min(0.05, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
    except (OSError, TimeoutError) as error:
        raise Fault("scope_unavailable", "The original user scope cannot be verified.") from error
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if fields.get("LoadState") == "not-found":
        return "not-found"
    if process.returncode or fields.get("ActiveState") not in {"active", "activating", "deactivating", "inactive", "failed"}:
        raise Fault("scope_unavailable", "The original user scope cannot be verified.")
    return fields["ActiveState"]


def validate_read(offset: int, max_bytes: int, wait_seconds: float) -> None:
    """Validate observation inputs before local IO or an SSH exchange."""
    if type(offset) is not int or offset < 0 or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_READ:
        raise Fault("invalid_range", "Use a nonnegative byte offset and at most 65536 bytes.")
    if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= MAX_WAIT:
        raise Fault("invalid_wait", "Use a finite wait_seconds from 0 to 5; zero reads immediately.")


def _snapshot(directory: Path, machine: str, execution_id: str, offset: int, max_bytes: int,
              deadline: float, cancelled: threading.Event) -> dict:
    record, status = receipt(directory, machine, wait=False, deadline=deadline, cancelled=cancelled)
    output = logs.read(directory, offset, max_bytes, legacy=record.get("format_version", 1) == 1, wait=False)
    return {"execution_id": execution_id, "machine": machine, "status": status,
            "exit_code": record["exit_code"], "context": record["context"], "version": record["version"],
            "created_at": record["created_at"], "finished_at": record.get("finished_at"),
            "task_id": record["request"].get("task_id", ""),
            "format_version": record.get("format_version", 1), "io_mode": record.get("io_mode", "pipe"),
            "command": record.get("command") if record.get("format_version", 1) >= 4 else None,
            "stop": record.get("stop") if record.get("format_version", 1) >= 4 else None,
            "cleanup": record.get("cleanup") if record.get("format_version", 1) >= 4 else None,
            "observed_at": time.time(), "control": controls.observed(record, status),
            "control_ready": bool(record.get("control_ready") and status == "running" and not record.get("error")),
            "control_actions": ([] if record.get("control") is None else
                                ["write_stdin", "interrupt", "resize"] if record.get("io_mode") == "pty" else ["interrupt"]),
            "terminal_size": record.get("terminal_size"),
            "termination_grace_seconds": effective_request(record)["termination_grace_seconds"],
            "workspace_access": record["request"].get("workspace_access", "exclusive"),
            "resource_keys": record["request"].get("resource_keys", []),
            "cancel_requested": (directory / "cancel.request").is_file(),
            "error": record.get("error"), **output,
            "log_truncated": record["log_truncated"] or output["log_start_offset"] > 0,
            "output_incomplete": record["output_incomplete"]}


def read(config: "Settings", state: Path, machine: str, execution_id: str,
         offset: int = 0, max_bytes: int = MAX_READ, wait_seconds: float = 0,
         *, cancelled: threading.Event | None = None) -> dict:
    validate_read(offset, max_bytes, wait_seconds)
    if config.machine(machine).transport != "local":
        raise Fault("remote_path", "Use application routing for an execution on a remote machine.")
    directory = record_directory(state, execution_id)
    cancelled = cancelled if cancelled is not None else threading.Event()
    started = time.monotonic()
    wait_deadline = started + wait_seconds
    # Immediate reads add no observation wait, but retain a bounded IO budget.
    io_deadline = started + (wait_seconds or MAX_WAIT)
    previous = None
    while True:
        if cancelled.is_set():
            raise Fault("observation_cancelled", "Only this observation was cancelled; the command is unchanged.")
        try:
            result = _snapshot(directory, machine, execution_id, offset, max_bytes, io_deadline, cancelled)
        except Fault as error:
            if error.code != "resource_busy":
                raise
            remaining = io_deadline - time.monotonic()
            if remaining <= 0:
                raise Fault("observation_busy", "A consistent observation was unavailable within its budget; read the original ID again.") from error
            cancelled.wait(min(0.05, remaining))
            continue
        # Timestamps such as observed_at change on every read, not on progress.
        facts = tuple(result[key] for key in ("status", "exit_code", "command", "stop", "cleanup",
                      "cancel_requested", "finished_at", "error", "output_incomplete", "control", "control_ready", "terminal_size"))
        wake = (result["next_offset"] != offset or result["output_gap"] or result["status"] in TERMINAL
                or (result["output_unavailable"] and result["status"] not in {"accepted", "preparing"})
                or (previous is not None and facts != previous))
        if not wait_seconds or wake:
            return result | {"wait_expired": False}
        remaining = wait_deadline - time.monotonic()
        if remaining <= 0:
            return result | {"wait_expired": True}
        previous = facts
        cancelled.wait(min(0.05, remaining))


def list_records(config: "Settings", state: Path, machine: str, cursor: str = "", limit: int = 20) -> dict:
    """Page stable IDs, not guessed logical tasks; inspect at most limit receipts."""
    if config.machine(machine).transport != "local":
        raise Fault("remote_path", "Use application routing for an execution on a remote machine.")
    if cursor:
        record_directory(state, cursor)
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
            record, status = receipt(root / name, machine)
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
