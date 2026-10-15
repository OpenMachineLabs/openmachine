"""Execution admission and mutations under original identity, rights and leases."""

from collections.abc import Callable
from contextlib import ExitStack, contextmanager
import hashlib
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

from . import control as controls, observation
from .process import boot_identity, session_members, supported
from .records import (CONTROL_PROTOCOL, CREATION_FORMAT, FORMAT, MAX_TIMEOUT, TERMINAL, control_request,
                      effective_request, is_remote, read_record, reconciled_record, record_directory,
                      require_cleanup_only_unknown, require_reconcilable,
                      resolve_terminal_size, termination_grace)

if TYPE_CHECKING:
    from openmachine.config import Settings

DEFAULT_TIMEOUT = 600


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
    directory = record_directory(state, execution_id)
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
            return observation.read(config, state, machine, execution_id)
        _admit(config, state, machine, execution_id, working, request,
               remote_worker=remote_worker, validate_new=validate_new)
    return observation.read(config, state, machine, execution_id)


def create_worktree(config: "Settings", state: Path, machine: str, workspace_name: str,
                    destination: str, base_ref: str, new_branch: str, execution_id: str,
                    expected_workspace_identity: str, worktree: str = "", task_id: str = "",
                    *, remote_worker: Path | None = None,
                    validate_new: Callable[[], object] | None = None) -> dict:
    """Create an optional checkout through the existing durable execution lifecycle."""
    before_admission = True
    try:
        directory = record_directory(state, execution_id)
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
            return observation.read(config, state, machine, execution_id)

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
        return observation.read(config, state, machine, execution_id)
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
    directory = record_directory(state, execution_id)
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
            try:
                owned = [leases.enter_context(lease(locks / f"workspace-{key}.lock", shared=workspace_access == "shared"))]
            except Fault as error:
                if error.code != "resource_busy" or creation_plan is not None:
                    raise
                raise Fault("resource_busy", "The selected workspace lock is busy. "
                            "For single-repository work, use its actual repository/worktree as cwd; "
                            "cd or -C inside argv does not change the lock. "
                            "Stay within the accepted task scope and do not bypass conflicts with shared access.",
                            {"cwd": str(working), "workspace_lock_root": observed["workspace_lock_root"]}) from error
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


def _activation_resources(config: "Settings", state: Path, record: dict) -> dict[Path, bool]:
    """Recover only existing, originally named local workspace/resource leases."""
    machine = record["request"]["machine"]
    if config.machine(machine).transport != "local":
        raise Fault("unsupported_operation", "Historical local cleanup is not an SSH scope.")
    request = effective_request(record)
    validate_start(request["cwd"], request["argv"], request["timeout_seconds"], request["expected_head_sha"],
                   request["workspace_access"], request["resource_keys"], request["task_id"],
                   request["termination_grace_seconds"], request["tty"], request["terminal_size"])
    root = record["context"].get("workspace_lock_root", record["context"].get("worktree"))
    if not isinstance(root, str) or not Path(root).is_absolute() or ".." in Path(root).parts:
        raise Fault("corrupt_state", "The original execution workspace identity is invalid.")
    targets = [Path(root)]
    if record["format_version"] == CREATION_FORMAT:
        targets.append(Path(record["context"]["creation_plan"]["source_root"]))
    if any(not any(target.is_absolute() and target.is_relative_to(Path(item.root).resolve())
                   for item in config.workspaces.values() if item.machine == machine) for target in targets):
        raise Fault("invalid_cwd", "Original execution workspaces no longer match the configured local grants.")
    resources = {state / "locks" / ("workspace-" + hashlib.sha256(root.encode()).hexdigest() + ".lock"): False}
    if record["format_version"] == CREATION_FORMAT:
        for parent in _creation_parent_roots(config, machine, Path(root)):
            if parent != root:
                path = state / "locks" / ("workspace-" + hashlib.sha256(parent.encode()).hexdigest() + ".lock")
                resources[path] = True
    for key in request["resource_keys"]:
        path = state / "locks" / ("resource-" + hashlib.sha256(key.encode()).hexdigest() + ".lock")
        resources[path] = False
    return resources


@contextmanager
def activation_guard(config: "Settings", state: Path, execution_ids: list[str]):
    """Hold settled cleanup-only records and original leases across service cutover.

    This guards controller replacement, not descendant termination or business success.
    Unknown execution and Task results stay unknown after the guard is released.
    """
    with ExitStack() as locks:
        records, resources = {}, {}
        for execution_id in sorted(set(execution_ids)):
            directory = record_directory(state, execution_id)
            locks.enter_context(lease(state / "locks" / f"{execution_id}.lock", create=False))
            locks.enter_context(lease(directory / "control.lock", create=False))
            locks.enter_context(lease(directory / "worker.lock", create=False))
            record = read_record(directory)
            require_cleanup_only_unknown(record)
            for path, shared in _activation_resources(config, state, record).items():
                # Shared histories reserve each original path once, exclusively
                # when any history requires exclusive access.
                resources[path] = resources.get(path, True) and shared
            records[execution_id] = record
        for path, shared in sorted(resources.items()):
            locks.enter_context(lease(path, shared=shared, create=False))
        yield records


def reconcile(config: "Settings", state: Path, machine: str, execution_id: str) -> dict:
    """Append original-ID cleanup evidence; never launch, signal or rewrite history."""
    if config.machine(machine).transport != "local":
        raise Fault("unsupported_operation", "Native cleanup reconciliation is local-only; retain the original SSH scope route.")
    directory = record_directory(state, execution_id)
    if not directory.exists():
        raise Fault("execution_not_found", "No receipt exists for this execution ID on this store.")
    _, status = observation.receipt(directory, machine, wait=False)
    if status in TERMINAL - {"outcome_unknown"}:
        return observation.read(config, state, machine, execution_id)
    with ExitStack() as locks:
        # Match launch/publication ordering, without waiting on a live worker.
        locks.enter_context(lease(state / "locks" / f"{execution_id}.lock", create=False))
        locks.enter_context(lease(directory / "control.lock", create=False))
        locks.enter_context(lease(directory / "worker.lock", create=False))
        record = read_record(directory)
        if record["request"]["machine"] != machine:
            raise Fault("context_mismatch", "Use the original machine identity for this execution.")
        if record["status"] == "outcome_unknown":
            require_reconcilable(record)
            if boot_identity() != record["cleanup"]["boot_id"]:
                raise Fault("cleanup_identity_unavailable", "The original native boot identity is no longer available.")
            root = record["context"].get("workspace_lock_root", record["context"]["worktree"])
            targets = [Path(root)]
            if record["format_version"] == CREATION_FORMAT:
                targets.append(Path(record["context"]["creation_plan"]["source_root"]))
            if any(not any(target.is_relative_to(Path(item.root).resolve())
                           for item in config.workspaces.values() if item.machine == machine) for target in targets):
                raise Fault("invalid_cwd", "The original cleanup target is no longer in a configured workspace.")
            path = state / "locks" / ("workspace-" + hashlib.sha256(root.encode()).hexdigest() + ".lock")
            locks.enter_context(lease(path, create=False))
            if record["format_version"] == CREATION_FORMAT:
                for parent in _creation_parent_roots(config, machine, Path(root)):
                    if parent == root:
                        continue
                    path = state / "locks" / ("workspace-" + hashlib.sha256(parent.encode()).hexdigest() + ".lock")
                    locks.enter_context(lease(path, shared=True, create=False))
            for key in record["request"].get("resource_keys", []):
                path = state / "locks" / ("resource-" + hashlib.sha256(key.encode()).hexdigest() + ".lock")
                locks.enter_context(lease(path, create=False))
            try:
                deadline = time.monotonic() + 1
                while True:
                    try:
                        members = session_members(record["cleanup"]["session_id"], deadline, leader_exited=True)
                        break
                    except Fault as error:
                        if error.code != "cleanup_query_raced" or time.monotonic() >= deadline:
                            raise
            except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
                raise Fault("cleanup_unverified", "The original session could not be observed; no recovery was published.") from error
            if members:
                raise Fault("cleanup_unverified", "The original session still has live members; no signals or recovery were issued.")
            raw = (directory / "record.json").read_bytes()
            evidence = {"format": 1, "execution_id": execution_id, "receipt_sha256": hashlib.sha256(raw).hexdigest(),
                        "observed_at": max(time.time(), record["finished_at"]), "created_worktree": None}
            if record["format_version"] == CREATION_FORMAT and record["exit_code"] == 0 and record["stop"]["reason"] is None:
                evidence["created_worktree"] = worktrees.verify_creation(record["context"]["creation_plan"])
            reconciled_record(record, evidence, evidence["receipt_sha256"], execution_id)
            save_json(directory / "reconciliation.json", evidence)
        elif record["status"] not in TERMINAL:
            raise Fault("execution_running", "Observe or cancel the original live execution before cleanup reconciliation.")
    return observation.read(config, state, machine, execution_id)


def cancel(config: "Settings", state: Path, machine: str, execution_id: str) -> dict:
    if config.machine(machine).transport != "local":
        raise Fault("remote_path", "Use application routing for an execution on a remote machine.")
    directory = record_directory(state, execution_id)
    record, status = observation.receipt(directory, machine)
    if status == "finishing" and is_remote(record) and record["status"] in TERMINAL:
        # Only a mutation tool may request cleanup again; reads never stop a scope.
        try:
            subprocess.run(["systemctl", "--user", "--no-ask-password", "--no-block", "stop", record["scope_unit"]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise Fault("outcome_unknown", "Scope cleanup is unverified; read the original execution.") from error
        return observation.read(config, state, machine, execution_id)
    if status == "outcome_unknown":
        raise Fault("outcome_unknown", "Worker ownership is unverified; cancellation cannot claim termination.")
    if status in TERMINAL:
        return observation.read(config, state, machine, execution_id)
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
    return observation.read(config, state, machine, execution_id)


def control_execution(config: "Settings", state: Path, machine: str, execution_id: str,
            expected_control_seq: int, action: str, text: str | None = None,
            terminal_size: dict | None = None) -> dict:
    request = control_request(expected_control_seq, action, text, terminal_size)
    if config.machine(machine, write=action in {"write_stdin", "resize"}).transport != "local":
        raise Fault("remote_path", "Use application routing for a remote control.")
    directory = record_directory(state, execution_id)
    record, status = observation.receipt(directory, machine, wait=False, deadline=time.monotonic() + observation.MAX_WAIT)
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
