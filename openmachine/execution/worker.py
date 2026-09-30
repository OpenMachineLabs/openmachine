"""Owned command lifecycle, output and terminal receipt publication."""

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
from .records import read_record


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
    record = read_record(directory)
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
        if record.get("format_version") == 3:
            groups = Path("/proc/self/cgroup").read_text().splitlines()
            if not any(line.endswith("/" + record["scope_unit"]) for line in groups):
                raise Fault("scope_mismatch", "The remote worker did not enter its owned user scope.")
        record.update(status="preparing", context=record["context"] | context(Path(request["cwd"])))
        save_json(directory / "record.json", record)
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
                save_json(directory / "record.json", record)
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
    with lease(directory / "control.lock", wait=True, create=False):
        if record.get("format_version") != 3:
            for fd in (*owned_fds, worker_fd):
                os.close(fd)
        record["finished_at"] = time.time()
        save_json(directory / "record.json", record)
    if record.get("format_version") == 3:
        # Keep inherited leases until systemd removes this entire owned cgroup.
        # The durable receipt is observable as terminal only after scope cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            subprocess.run(["systemctl", "--user", "--no-ask-password", "--no-block", "stop", record["scope_unit"]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            with lease(directory / "control.lock", wait=True, create=False):
                record.update(status="failed", error="scope_cleanup_unverified")
                save_json(directory / "record.json", record)
        # RuntimeMaxSec also bounds the scope if its explicit stop is lost.
        while True:
            signal.pause()
