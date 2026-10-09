"""Sole post-admission writer: command/control lifecycle, drain and publication."""

import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, save_json
from openmachine.workspace.git import context
from openmachine.workspace import worktrees

from . import control, logs
from .logs import MAX_BYTES as MAX_LOG, MAX_READ
from .process import Process, boot_identity
from .records import CREATION_FORMAT, FORMAT, STOP_STATUS, is_remote, read_record

PREPARE_SECONDS = 15
FORCE_SECONDS = 2
DRAIN_SECONDS = 1


def _publish(directory: Path, record: dict) -> bool:
    try:
        save_json(directory / "record.json", record)
        return True
    except OSError:
        record["error"] = "receipt_io_failed"
        return False


def _run(directory: Path, record: dict, process: Process, output, interrupted,
         selector: selectors.BaseSelector, controls: control.Server | None) -> None:
    """Advance stop/control deadlines and consume bounded output each iteration."""
    command, stop, cleanup = record["command"], record["stop"], record["cleanup"]
    deadline = (process.started_monotonic if process.spawned else time.monotonic()) + record["request"]["timeout_seconds"]
    term_deadline = force_deadline = drain_deadline = None
    term_groups = set()
    logging_failed = observation_failed = False
    output_open = True
    selector.register(process.output_fd, selectors.EVENT_READ)
    while True:
        now = time.monotonic()
        if not observation_failed:
            try:
                exited = process.observe_exit()
                if exited and process.spawned and not command["exited"]:
                    command.update(exited=True, exited_at=time.time())
                    record.update(status="finishing", control_ready=False)
                    _publish(directory, record)
                # A late request cannot relabel an already observed natural exit.
                if not exited and stop["reason"] is None and term_deadline is None:
                    reason = ("worker_signal" if interrupted() else "cancel" if (directory / "cancel.request").exists()
                              else "timeout" if process.spawned and now >= deadline else None)
                    if reason:
                        stop.update(reason=reason, requested_at=time.time())
                if term_deadline is None and (exited or stop["reason"] or record.get("error")):
                    record.update(status="finishing" if exited else "stopping", control_ready=False)
                    term_deadline = now + stop["grace_seconds"]
                    force_deadline = term_deadline + FORCE_SECONDS
                    _publish(directory, record)
                if term_deadline is not None and drain_deadline is None:
                    try:
                        members = process.members(time.monotonic() + min(1, max(0.05, force_deadline - now)))
                    except Fault as error:
                        # Keep the original child unreaped while a bounded query
                        # can still recover. Never extend the cleanup deadline.
                        if error.code not in {"cleanup_unverified", "cleanup_query_timeout", "cleanup_query_limit",
                                              "cleanup_query_failed", "cleanup_query_invalid", "cleanup_query_raced"} or time.monotonic() >= force_deadline:
                            raise
                        cleanup["error"] = error.code
                        members = None
                    if members is None:
                        pass
                    elif not members and exited:
                        cleanup.update(status="verified", error=None)
                        drain_deadline = time.monotonic() + DRAIN_SECONDS
                    elif now >= force_deadline:
                        cleanup.update(status="unverified", error="owned_processes_remain")
                        drain_deadline = time.monotonic() + DRAIN_SECONDS
                    elif now >= term_deadline:
                        if process.signal_groups(members, signal.SIGKILL) and not stop["force_used"]:
                            stop["force_used"] = True
                            _publish(directory, record)
                    else:
                        fresh = {pid: group for pid, group in members.items() if group not in term_groups}
                        term_groups.update(process.signal_groups(fresh, signal.SIGTERM))
            except (OSError, ValueError, IndexError, Fault, subprocess.SubprocessError) as error:
                cleanup.update(status="unverified", error=error.code if isinstance(error, Fault) else "process_observation_failed")
                record["control_ready"] = False
                # Lost ownership never permits broader signaling or PID adoption.
                drain_deadline = time.monotonic() + DRAIN_SECONDS
                observation_failed = True
        if controls is not None:
            controls.advance(stopping=term_deadline is not None or observation_failed)
        # Control descriptors are not output: a listener cannot prevent output EOF.
        if drain_deadline is not None and (not output_open or time.monotonic() >= drain_deadline):
            record["output_incomplete"] |= output_open
            break
        for key, _ in selector.select(0.05):
            if key.fd != process.output_fd:
                if controls is not None:
                    controls.handle(key)
                continue
            try:
                chunk = process.read_output(MAX_READ)
                if not chunk:
                    selector.unregister(key.fileobj)
                    output_open = False
                elif not logging_failed:
                    output.write(chunk)
            except BlockingIOError:
                pass
            except (OSError, ValueError):
                logging_failed = True
                record.update(error="output_io_failed", output_incomplete=True)
    if process.exited:
        try:
            code = process.reap()
            if process.spawned:
                record["exit_code"] = code
                command["exit_signal"] = -code if code < 0 else None
        except (OSError, Fault):
            cleanup.update(status="unverified", error="exit_reap_unverified")
    if cleanup["status"] != "verified" or process.launch_uncertain:
        record["status"] = "outcome_unknown"
    else:
        record["status"] = STOP_STATUS.get(stop["reason"]) or (
            "succeeded" if record["exit_code"] == 0 and not record.get("error") else "failed")


def worker(directory: Path, worker_fd: int, *owned_fds: int) -> None:
    for fd in (worker_fd, *owned_fds):
        os.fstat(fd)
    # The command inherits workspace/resource leases, never the worker's identity lease.
    os.set_inheritable(worker_fd, False)
    record = read_record(directory)
    if record.get("format_version") not in (FORMAT, CREATION_FORMAT):
        raise Fault("unsupported_state_version", "Only the original runtime may start a legacy worker.")
    request = record["request"]
    creation_plan = record["context"].get("creation_plan")
    if creation_plan is not None:
        # This dedicated worker must not inherit Git selectors from the controller.
        for name in list(os.environ):
            if name.startswith("GIT_"):
                del os.environ[name]
    original_worktree = record["context"]["worktree"]
    process = None
    interrupted = scope_owned = False
    preparation_deadline = time.monotonic() + PREPARE_SECONDS

    def interrupt(*_):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        if is_remote(record):
            groups = Path("/proc/self/cgroup").read_text().splitlines()
            if not any(line.endswith("/" + record["scope_unit"]) for line in groups):
                raise Fault("scope_mismatch", "The remote worker did not enter its owned user scope.")
            scope_owned = True
        if creation_plan is not None:
            worktrees.ensure_local_objects(Path(request["cwd"]))
        record.update(status="preparing", context=record["context"] | context(Path(request["cwd"])))
        if not _publish(directory, record):
            raise Fault("receipt_io_failed", "Preparation could not be persisted.")
        if creation_plan is not None:
            worktrees.validate_creation(creation_plan)
        else:
            if record["context"]["git_status"] == "unavailable" or record["context"]["worktree"] != original_worktree:
                raise Fault("context_mismatch", "The canonical worktree changed before launch.")
            if request["expected_head_sha"] and record["context"]["head_sha"] != request["expected_head_sha"]:
                raise Fault("context_mismatch", "Git HEAD changed before command launch.")
        if time.monotonic() >= preparation_deadline:
            raise Fault("preparation_timeout", "The worker exceeded its prelaunch budget.")
        with logs.Writer(directory) as output:
            if interrupted or (directory / "cancel.request").exists():
                reason = "worker_signal" if interrupted else "cancel"
                record["stop"].update(reason=reason, requested_at=time.time())
                record["cleanup"]["status"] = "verified"
                record["status"] = STOP_STATUS[reason]
            else:
                process = Process()
                with selectors.DefaultSelector() as selector:
                    controls = None
                    try:
                        if record["control"] is not None:
                            controls = control.Server(control.socket_path(directory.parent.parent, directory.name),
                                                      directory, record, process, selector, _publish)
                        try:
                            native_boot = boot_identity() if not is_remote(record) else None
                            process.launch(request["argv"], request["cwd"], owned_fds, tty=request["tty"],
                                           terminal_size=request["terminal_size"], deadline=preparation_deadline)
                        except (OSError, ValueError, Fault, subprocess.SubprocessError) as error:
                            record["error"] = ("launch_unverified" if process.launch_uncertain else
                                               error.code if isinstance(error, Fault) else type(error).__name__)
                        if process.pid is not None:
                            if native_boot is not None:
                                record["cleanup"].update(session_id=process.session, boot_id=native_boot)
                            if process.spawned:
                                record["command"].update(spawned=True, started_at=process.started_at)
                                record.update(status="running", started_at=process.started_at,
                                              control_ready=controls is not None and not record.get("error"))
                            if time.monotonic() >= preparation_deadline:
                                record["error"] = "preparation_timeout"
                            _publish(directory, record)
                            _run(directory, record, process, output, lambda: interrupted, selector, controls)
                        else:
                            record["status"] = "failed"
                            record["cleanup"]["status"] = "verified"
                    finally:
                        if controls is not None:
                            controls.advance(stopping=True)
                            controls.close()
            record["log_truncated"] = output.total > MAX_LOG
        if creation_plan is not None and record["status"] == "succeeded":
            try:
                record["context"]["created_worktree"] = worktrees.verify_creation(creation_plan)
            except (OSError, ValueError, Fault, subprocess.SubprocessError) as error:
                record.update(status="outcome_unknown",
                              error=error.code if isinstance(error, Fault) else "worktree_verification_failed")
    except (OSError, ValueError, Fault, subprocess.SubprocessError) as error:
        record.update(error=error.code if isinstance(error, Fault) else type(error).__name__)
        if process is None or process.pid is None:
            record["cleanup"]["status"] = "verified"
        elif record["cleanup"]["status"] != "verified":
            record["cleanup"].update(status="unverified", error="worker_cleanup_unverified")
        record["status"] = "failed" if record["cleanup"]["status"] == "verified" else "outcome_unknown"
        record["output_incomplete"] |= record["command"]["spawned"]
    finally:
        record["control_ready"] = False
        if process is not None:
            process.close()
    if is_remote(record) and scope_owned:
        record["cleanup"]["status"] = "pending"
    # Readers cannot observe the lease-release/publication gap as a lost worker.
    with lease(directory / "control.lock", wait=True, create=False):
        if not scope_owned:
            for fd in (*owned_fds, worker_fd):
                os.close(fd)
        record["finished_at"] = time.time()
        if not _publish(directory, record):
            record["status"] = "failed" if record["cleanup"]["status"] == "verified" else "outcome_unknown"
            _publish(directory, record)
    if scope_owned:
        # Retain inherited leases until systemd removes the original scope.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            subprocess.run(["systemctl", "--user", "--no-ask-password", "--no-block", "stop", record["scope_unit"]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            with lease(directory / "control.lock", wait=True, create=False):
                record.update(status="outcome_unknown", error="scope_cleanup_unverified")
                _publish(directory, record)
        while True:
            signal.pause()
