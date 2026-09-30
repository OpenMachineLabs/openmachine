"""Sole post-admission writer: command lifecycle, draining and final publication."""

import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, save_json
from openmachine.workspace.git import context

from . import logs
from .logs import MAX_BYTES as MAX_LOG, MAX_READ
from .process import Process
from .records import FORMAT, STOP_STATUS, is_remote, read_record

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


def _run(directory: Path, record: dict, process: Process, output, interrupted) -> None:
    """Advance stop deadlines while consuming bounded output on every iteration."""
    command, stop, cleanup = record["command"], record["stop"], record["cleanup"]
    deadline = time.monotonic() + record["request"]["timeout_seconds"]
    term_deadline = force_deadline = drain_deadline = None
    term_groups = set()
    logging_failed = False
    observation_failed = False
    with selectors.DefaultSelector() as selector:
        selector.register(process.child.stdout, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            if not observation_failed:
                try:
                    exited = process.observe_exit()
                    if exited and not command["exited"]:
                        command.update(exited=True, exited_at=time.time())
                        record["status"] = "finishing"
                        _publish(directory, record)
                    # A late request cannot relabel an already observed natural exit.
                    if not exited and stop["reason"] is None:
                        reason = ("worker_signal" if interrupted() else "cancel" if (directory / "cancel.request").exists()
                                  else "timeout" if now >= deadline else None)
                        if reason:
                            stop.update(reason=reason, requested_at=time.time())
                    if term_deadline is None and (exited or stop["reason"] or record.get("error")):
                        record["status"] = "finishing" if exited else "stopping"
                        term_deadline = now + stop["grace_seconds"]
                        force_deadline = term_deadline + FORCE_SECONDS
                        _publish(directory, record)
                    if term_deadline is not None and drain_deadline is None:
                        members = process.members(time.monotonic() + min(1, max(0.05, force_deadline - now)))
                        if not members and exited:
                            cleanup["status"] = "verified"
                            drain_deadline = time.monotonic() + DRAIN_SECONDS
                        elif now >= force_deadline:
                            cleanup.update(status="unverified", error="owned_processes_remain")
                            drain_deadline = time.monotonic() + DRAIN_SECONDS
                        elif now >= term_deadline:
                            if process.signal_groups(members, signal.SIGKILL):
                                if not stop["force_used"]:
                                    stop["force_used"] = True
                                    _publish(directory, record)
                        else:
                            fresh = {pid: group for pid, group in members.items() if group not in term_groups}
                            term_groups.update(process.signal_groups(fresh, signal.SIGTERM))
                except (OSError, ValueError, IndexError, Fault, subprocess.SubprocessError) as error:
                    cleanup.update(status="unverified", error=error.code if isinstance(error, Fault) else "process_observation_failed")
                    # Lost ownership is not permission to broaden signaling or adopt PIDs.
                    drain_deadline = time.monotonic() + DRAIN_SECONDS
                    observation_failed = True
            if drain_deadline is not None and (not selector.get_map() or time.monotonic() >= drain_deadline):
                record["output_incomplete"] |= bool(selector.get_map())
                break
            for key, _ in selector.select(0.05):
                try:
                    chunk = os.read(key.fd, MAX_READ)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif not logging_failed:
                        output.write(chunk)
                except (OSError, ValueError):
                    # Keep draining during failure cleanup, but never claim retained output.
                    logging_failed = True
                    record.update(error="output_io_failed", output_incomplete=True)
        if selector.get_map():
            record["output_incomplete"] = True
    if process.exited:
        try:
            code = process.reap()
            record["exit_code"] = code
            command["exit_signal"] = -code if code < 0 else None
        except (OSError, Fault):
            cleanup.update(status="unverified", error="exit_reap_unverified")
    if cleanup["status"] != "verified":
        record["status"] = "outcome_unknown"
    else:
        record["status"] = STOP_STATUS.get(stop["reason"]) or (
            "succeeded" if record["exit_code"] == 0 and not record.get("error") else "failed")


def worker(directory: Path, worker_fd: int, *owned_fds: int) -> None:
    for fd in (worker_fd, *owned_fds):
        os.fstat(fd)
    record = read_record(directory)
    if record.get("format_version") != FORMAT:
        raise Fault("unsupported_state_version", "Only the original runtime may start a legacy worker.")
    request = record["request"]
    original_worktree = record["context"]["worktree"]
    process = None
    interrupted = False
    scope_owned = False
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
        record.update(status="preparing", context=record["context"] | context(Path(request["cwd"])))
        if not _publish(directory, record):
            raise Fault("receipt_io_failed", "Preparation could not be persisted.")
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
                try:
                    process.launch(request["argv"], request["cwd"], owned_fds)
                except (OSError, ValueError, Fault, subprocess.SubprocessError) as error:
                    record["error"] = error.code if isinstance(error, Fault) else type(error).__name__
                if process.child is not None:
                    record["command"].update(spawned=True, started_at=time.time())
                    record.update(status="running", started_at=record["command"]["started_at"])
                    if time.monotonic() >= preparation_deadline:
                        record["error"] = "preparation_timeout"
                    _publish(directory, record)
                    _run(directory, record, process, output, lambda: interrupted)
                else:
                    record["status"] = "failed"
                    record["cleanup"]["status"] = "verified"
            record["log_truncated"] = output.total > MAX_LOG
    except (OSError, ValueError, Fault, subprocess.SubprocessError) as error:
        record.update(error=error.code if isinstance(error, Fault) else type(error).__name__)
        if process is None or process.child is None:
            record["cleanup"]["status"] = "verified"
        elif record["cleanup"]["status"] != "verified":
            record["cleanup"].update(status="unverified", error="worker_cleanup_unverified")
        record["status"] = "failed" if record["cleanup"]["status"] == "verified" else "outcome_unknown"
        record["output_incomplete"] |= record["command"]["spawned"]
    finally:
        if process is not None:
            process.close()
    if is_remote(record) and scope_owned:
        # The worker cannot observe its own cgroup disappearance. The reader checks it.
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
        # Keep inherited leases until systemd removes the entire original scope.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            subprocess.run(["systemctl", "--user", "--no-ask-password", "--no-block", "stop", record["scope_unit"]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            with lease(directory / "control.lock", wait=True, create=False):
                record.update(status="outcome_unknown", error="scope_cleanup_unverified")
                _publish(directory, record)
        # RuntimeMaxSec is the outer bound if the stop request was lost.
        while True:
            signal.pause()
