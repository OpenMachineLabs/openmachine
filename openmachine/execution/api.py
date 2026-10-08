"""Execution admission, receipt observation and cancellation."""

from collections.abc import Callable
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
from openmachine.workspace.git import context, lock_root
from openmachine.workspace import worktrees

from . import control as controls, logs
from .logs import MAX_READ
from .process import supported
from .records import (CONTROL_PROTOCOL, CREATION_FORMAT, FORMAT, MAX_TIMEOUT, TERMINAL, control_request,
                      effective_request, is_remote, read_record, resolve_terminal_size, termination_grace)

if TYPE_CHECKING:
    from openmachine.config import Settings

DEFAULT_TIMEOUT = 600
MAX_WAIT = 5
MAX_WAITERS = 16


def _directory(state: Path, execution_id: str) -> Path:
    if not isinstance(execution_id, str) or not re.fullmatch(r"[0-9a-f]{32}", execution_id):
        raise Fault("invalid_execution_id", "Use a 32-character lowercase hexadecimal execution ID.")
    return state / "executions" / execution_id


def validate_start(cwd: str, argv: list[str], timeout_seconds: int, expected_head_sha: str,
                   workspace_access: str, resource_keys: list[str] | None, task_id: str,
                   termination_grace_seconds: float | None = None, tty: bool = False,
                   terminal_size: dict | None = None) -> list[str]:
    """Check the shared start request before controller routing or target admission."""
    if task_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
        raise Fault("invalid_task_id", "Supply the original bounded task ID, or leave the reference empty.")
    if not argv or not argv[0] or len(argv) > 100 or any("\0" in a or len(a) > 4096 for a in argv):
        raise Fault("invalid_command", "Supply a program and at most 99 arguments, each at most 4096 characters.")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT:
        raise Fault("invalid_timeout", "Use 1-86400 seconds; the default remains 600 seconds.")
    if workspace_access not in ("exclusive", "shared"):
        raise Fault("invalid_access", "Use exclusive, or shared only for cooperating read-only commands.")
    termination_grace(termination_grace_seconds)
    resolve_terminal_size(tty, terminal_size)
    keys = resource_keys or []
    if len(keys) > 16 or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", k) for k in keys):
        raise Fault("invalid_resources", "Use at most 16 stable resource names of 1-128 letters, digits, _, ., :, or -.")
    if expected_head_sha and not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
        raise Fault("invalid_head", "expected_head_sha must be a full lowercase Git commit SHA.")
    if not Path(cwd).is_absolute():
        raise Fault("invalid_cwd", "Select an existing absolute working directory.")
    return keys


def start(config: "Settings", state: Path, machine: str, execution_id: str, cwd: str,
          argv: list[str], timeout_seconds: int = DEFAULT_TIMEOUT, expected_head_sha: str = "",
          workspace_access: str = "exclusive", resource_keys: list[str] | None = None, task_id: str = "",
          termination_grace_seconds: float | None = None, tty: bool = False, terminal_size: dict | None = None,
          *, remote_worker: Path | None = None, validate_new: Callable[[], object] | None = None) -> dict:
    target = config.machine(machine, write=True)
    keys = validate_start(cwd, argv, timeout_seconds, expected_head_sha, workspace_access, resource_keys, task_id,
                          termination_grace_seconds, tty, terminal_size)
    if target.transport != "local":
        raise Fault("remote_path", "Use application routing for an execution on a remote machine.")
    try:
        working = Path(cwd).resolve(strict=True)
    except OSError as error:
        raise Fault("context_unavailable", "The original cwd is unavailable; read the original execution directly.") from error
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
               "workspace_access": workspace_access, "resource_keys": sorted(set(keys)), "task_id": task_id,
               "termination_grace_seconds": termination_grace(termination_grace_seconds),
               "tty": tty, "terminal_size": resolve_terminal_size(tty, terminal_size)}
    with lease(locks / f"{execution_id}.lock"):
        if directory.exists():
            original = effective_request(read_record(directory))
            if termination_grace_seconds is None:
                request["termination_grace_seconds"] = original["termination_grace_seconds"]
            if original != request:
                raise Fault("execution_conflict", "This execution ID already belongs to a different request.")
            return read(config, state, machine, execution_id)
        _admit(config, state, machine, execution_id, working, request,
               remote_worker=remote_worker, validate_new=validate_new)
    return read(config, state, machine, execution_id)


def create_worktree(config: "Settings", state: Path, machine: str, workspace_name: str,
                    destination: str, base_ref: str, new_branch: str, execution_id: str,
                    expected_workspace_identity: str, worktree: str = "", task_id: str = "",
                    *, remote_worker: Path | None = None,
                    validate_new: Callable[[], object] | None = None) -> dict:
    """Create an optional checkout through the existing durable execution lifecycle."""
    before_admission = True
    try:
        directory = _directory(state, execution_id)
        if any(not isinstance(value, str) or not 1 <= len(value) <= 128 or "\0" in value
               for value in (machine, workspace_name)):
            raise Fault("invalid_workspace", "Supply the configured machine and workspace names.")
        if not isinstance(task_id, str) or (task_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id)):
            raise Fault("invalid_task_id", "Supply the original bounded task ID, or leave the reference empty.")
        intent = {"workspace": workspace_name, **worktrees.normalize_creation(
            destination, base_ref, new_branch, expected_workspace_identity, worktree)}
        target = config.machine(machine)
        if target.transport != "local":
            raise Fault("remote_path", "Use application routing for an execution on a remote machine.")

        def original_result():
            original = read_record(directory)["request"]
            if (original.get("worktree_creation") != intent or original["machine"] != machine
                    or original.get("task_id", "") != task_id):
                raise Fault("execution_conflict", "This execution ID already belongs to a different request.")
            # Resolve no mutable repository, ref or destination on an original-ID retry.
            return read(config, state, machine, execution_id)

        if not target.execution_enabled and (directory.exists() or directory.is_symlink()):
            # Retained receipts remain readable without allocating new launch state.
            before_admission = False
            return original_result()
        # Check the grant before creating any state or ID lock for a fresh request.
        config.machine(machine, write=True)
        state = private_directory(state)
        private_directory(state / "executions")
        locks = private_directory(state / "locks")
        with lease(locks / f"{execution_id}.lock"):
            if directory.exists() or directory.is_symlink():
                before_admission = False
                return original_result()
            config.machine(machine, write=True)
            if validate_new is not None:
                validate_new()
            plan = worktrees.prepare_creation(config, machine, workspace_name, destination, base_ref,
                                              new_branch, expected_workspace_identity, worktree)
            request = {"machine": machine, "cwd": plan["source_root"], "argv": plan["argv"],
                       "timeout_seconds": DEFAULT_TIMEOUT, "expected_head_sha": "",
                       "workspace_access": "exclusive", "resource_keys": plan["resource_keys"],
                       "task_id": task_id, "termination_grace_seconds": termination_grace(None),
                       "tty": False, "terminal_size": None, "worktree_creation": intent}
            before_admission = False
            _admit(config, state, machine, execution_id, Path(plan["source_root"]), request,
                   remote_worker=remote_worker, creation_plan=plan)
        return read(config, state, machine, execution_id)
    except (Fault, OSError) as error:
        if before_admission:
            code = error.code if isinstance(error, Fault) else "execution_unavailable"
            message = str(error) if isinstance(error, Fault) else "No checkout creation was admitted."
            evidence = error.evidence if isinstance(error, Fault) else None
            raise Fault(code, message, evidence, effect="known_no_effect") from error
        # Missing receipts after dispatch or an original-ID read are not proof of no effect.
        if isinstance(error, OSError):
            raise Fault("execution_unavailable", "Read the original worktree execution; its outcome is unverified.",
                        effect="effect_unknown") from error
        if error.effect is None:
            raise Fault(error.code, str(error), error.evidence, effect="effect_unknown") from error
        raise


def _creation_parent_roots(config: "Settings", machine: str, destination: Path) -> list[str]:
    """Reserve existing non-Git grant scopes without serializing sibling creates."""
    roots = set()
    for item in config.workspaces.values():
        if item.machine != machine:
            continue
        try:
            root = Path(item.root).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if destination.is_relative_to(root):
            roots.add(str(root))
    if not roots:
        raise Fault("path_denied", "The destination must stay inside an existing configured root.")
    return sorted(roots)


def _admit(config: "Settings", state: Path, machine: str, execution_id: str,
           working: Path, request: dict, *, remote_worker: Path | None,
           validate_new: Callable[[], object] | None = None,
           creation_plan: dict | None = None) -> None:
    """Admit once under the original ID lease; creation plans are internal only."""
    directory = _directory(state, execution_id)
    locks = state / "locks"
    timeout_seconds = request["timeout_seconds"]
    expected_head_sha = request["expected_head_sha"]
    workspace_access = request["workspace_access"]
    tty = request["tty"]
    with ExitStack() as leases:
        try:
            supported()
            try:
                control_path = controls.socket_path(state, execution_id)
            except Fault as error:
                if tty or error.code != "control_path_too_long":
                    raise
                control_path = None
            if control_path is not None:
                private_directory(control_path.parent)
                if control_path.exists() or control_path.is_symlink():
                    raise Fault("control_owner_mismatch", "Preserve the existing control endpoint; no command was launched.")
            if validate_new is not None:
                validate_new()  # Only new admissions need controller reference validation.
            if creation_plan is not None:
                worktrees.ensure_local_objects(working)
            observed = context(working)
            if observed["git_status"] == "unavailable":
                raise Fault("context_unavailable", "Git context could not be checked; do not bypass workspace coordination.")
            if creation_plan is None:
                check_workspace_leases(config, state, scope=working)
                observed["workspace_lock_root"] = lock_root(config, machine, working, observed)
            else:
                # Creation writes the destination and shared Git metadata, not source files.
                check_workspace_leases(config, state, scope=Path(creation_plan["destination"]))
                observed.update(workspace_lock_root=creation_plan["destination"], creation_plan=creation_plan)
            key = hashlib.sha256(observed["workspace_lock_root"].encode()).hexdigest()
            owned = [leases.enter_context(lease(locks / f"workspace-{key}.lock", shared=workspace_access == "shared"))]
            if creation_plan is not None:
                for root in _creation_parent_roots(config, machine, Path(creation_plan["destination"])):
                    digest = hashlib.sha256(root.encode()).hexdigest()
                    # Block existing container writers from either side; distinct
                    # creations can share this reservation for their own lifetime.
                    owned.append(leases.enter_context(lease(locks / f"workspace-{digest}.lock", shared=True)))
            for name in request["resource_keys"]:
                digest = hashlib.sha256(name.encode()).hexdigest()
                owned.append(leases.enter_context(lease(locks / f"resource-{digest}.lock")))
            if expected_head_sha and observed["head_sha"] != expected_head_sha:
                raise Fault("context_mismatch", "Git HEAD changed or could not be verified; inspect it before proceeding.")
            if creation_plan is not None:
                worktrees.validate_creation(creation_plan)
        except (Fault, OSError) as error:
            if creation_plan is None:
                raise
            code = error.code if isinstance(error, Fault) else "execution_unavailable"
            message = str(error) if isinstance(error, Fault) else "No checkout creation was admitted."
            evidence = error.evidence if isinstance(error, Fault) else None
            raise Fault(code, message, evidence, effect="known_no_effect") from error
        private_directory(directory)
        record = {"request": request, "context": observed, "version": __version__,
                  "format_version": CREATION_FORMAT if creation_plan is not None else FORMAT,
                  "status": "accepted", "exit_code": None, "created_at": time.time(),
                  "log_truncated": False, "output_incomplete": False, "io_mode": "pty" if tty else "pipe",
                  "terminal_size": request["terminal_size"], "control_ready": False,
                  "control": {"protocol": CONTROL_PROTOCOL, "seq": 0, "last": None} if control_path else None,
                  "command": {"spawned": False, "exited": False, "started_at": None,
                              "exited_at": None, "exit_signal": None},
                  "stop": {"reason": None, "requested_at": None,
                           "grace_seconds": request["termination_grace_seconds"], "force_used": False},
                  "cleanup": {"containment": "systemd_scope" if remote_worker else "posix_session",
                              "status": "pending", "error": None}}
        if remote_worker is not None:
            record.update(scope_unit=f"openmachine-{execution_id}.scope")
            observed.update(target_identity=machine, worker_archive=str(remote_worker),
                            worker_sha256=remote_worker.stem)
        with lease(directory / "control.lock"), lease(directory / "worker.lock") as worker_lease:
            save_json(directory / "record.json", record)
            fds = [lease.fileno() for lease in owned]
            launcher = [sys.executable, "-I", "-m", "openmachine.execution"]
            if remote_worker is not None:
                launcher = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--no-ask-password",
                            "--expand-environment=no", "--unit=" + record["scope_unit"],
                            f"--property=TimeoutStopSec={request['termination_grace_seconds'] + 7}s",
                            f"--property=RuntimeMaxSec={timeout_seconds + request['termination_grace_seconds'] + 30}s",
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
                record["cleanup"]["status"] = "verified"
                save_json(directory / "record.json", record)
                raise Fault("worker_launch_failed", "The worker could not start; inspect this execution ID.") from error


def check_workspace_leases(config, state: Path, *, legacy_candidate: bool = False, scope: Path | None = None) -> None:
    """Verify non-Git lease mapping in either direction without replacing a lock."""
    root = state / "executions"
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise Fault("corrupt_state", "The execution store is not an ordinary directory.")
    for directory in root.iterdir():
        record = read_record(directory)
        observed = record["context"]
        if observed.get("git_status") != "not_a_worktree":
            continue
        cwd = Path(record["request"]["cwd"])
        related = [item for item in config.workspaces.values()
                   if config.machine(item.machine).transport == "local" and cwd.is_relative_to(Path(item.root).resolve())]
        if not related:
            continue
        if scope is not None and not any(
            scope.is_relative_to(Path(item.root).resolve()) or Path(item.root).resolve().is_relative_to(scope)
            for item in related
        ):
            continue
        original = observed.get("workspace_lock_root", observed["worktree"])
        try:
            selected = observed["worktree"] if legacy_candidate else lock_root(config, related[0].machine, cwd, observed)
        except Fault as error:
            if error.code != "ambiguous_workspace":
                raise
            # Historical roots need not admit new work, but their original lease
            # must be verifiably free before a transition or another admission.
            selected = None
        if selected == original:
            continue
        old_key = hashlib.sha256(original.encode()).hexdigest()
        try:
            with lease(state / "locks" / f"workspace-{old_key}.lock", create=False):
                pass
        except (Fault, OSError) as error:
            raise Fault("legacy_workspace_busy", "An original non-Git cwd lease is busy or unavailable; let its owner finish before changing the lock domain.") from error


def _receipt(directory: Path, machine: str, *, wait: bool = True, deadline: float | None = None,
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
    record, status = _receipt(directory, machine, wait=False, deadline=deadline, cancelled=cancelled)
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
    directory = _directory(state, execution_id)
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
    if config.machine(machine).transport != "local":
        raise Fault("remote_path", "Use application routing for an execution on a remote machine.")
    directory = _directory(state, execution_id)
    record, status = _receipt(directory, machine)
    if status == "finishing" and is_remote(record) and record["status"] in TERMINAL:
        # Only a mutation tool may request cleanup again; reads never stop a scope.
        try:
            subprocess.run(["systemctl", "--user", "--no-ask-password", "--no-block", "stop", record["scope_unit"]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise Fault("outcome_unknown", "Scope cleanup is unverified; read the original execution.") from error
        return read(config, state, machine, execution_id)
    if status == "outcome_unknown":
        raise Fault("outcome_unknown", "Worker ownership is unverified; cancellation cannot claim termination.")
    if status in TERMINAL:
        return read(config, state, machine, execution_id)
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


def control_execution(config: "Settings", state: Path, machine: str, execution_id: str,
            expected_control_seq: int, action: str, text: str | None = None,
            terminal_size: dict | None = None) -> dict:
    request = control_request(expected_control_seq, action, text, terminal_size)
    if config.machine(machine, write=action in {"write_stdin", "resize"}).transport != "local":
        raise Fault("remote_path", "Use application routing for a remote control.")
    directory = _directory(state, execution_id)
    record, status = _receipt(directory, machine, wait=False, deadline=time.monotonic() + MAX_WAIT)
    previous = controls.retained(record, status, request)
    if previous is not None:
        return controls.reply(record, execution_id, status, previous)
    if status == "outcome_unknown":
        raise Fault("outcome_unknown", "The original worker ownership is unavailable; no input was replayed.")
    if status in TERMINAL or status in {"stopping", "finishing"}:
        raise Fault("execution_finished", "The original execution is no longer accepting controls.")
    if not record.get("control_ready") or status != "running":
        raise Fault("control_not_ready", "Observe the original worker; no sequence was reserved.")
    if action != "interrupt" and record["io_mode"] != "pty":
        raise Fault("unsupported_operation", "Input and resize require an explicitly started PTY.")
    result = controls.exchange(controls.socket_path(state, execution_id), execution_id, request)
    if result.get("machine") != machine:
        raise Fault("outcome_unknown", "The original control target identity was not verified.")
    return result
