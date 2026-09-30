"""Owned POSIX process/session mechanics; no records, routing or task policy."""

import errno
import os
from pathlib import Path
import select
import selectors
import subprocess
import sys
import time

from openmachine.foundation.sanitize import Fault

MAX_PROCESSES = 65536


def supported() -> None:
    if sys.platform == "darwin":
        available = hasattr(select, "kqueue")
    else:
        available = sys.platform == "linux" and all(
            hasattr(os, name) for name in ("waitid", "WNOWAIT", "WEXITED", "P_PID"))
    if not available:
        raise Fault("unsupported_process_host", "Execution requires macOS kqueue or Linux waitid/WNOWAIT.")


def _mac_states(deadline: float, pid: int | None = None) -> list[tuple[int, str]]:
    """Bound a PID/state-only query; never capture command lines or environments."""
    argv = ["/bin/ps", "-p", str(pid)] if pid is not None else ["/bin/ps", "-A"]
    data = bytearray()
    with subprocess.Popen([*argv, "-o", "pid=,stat="], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as query:
        try:
            os.set_blocking(query.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(query.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Fault("cleanup_unverified", "The owned-session query exceeded its deadline.")
                    for key, _ in selector.select(min(0.05, remaining)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            data.extend(chunk)
                            if len(data) > MAX_PROCESSES * 32:
                                raise Fault("cleanup_unverified", "The process query exceeded its bound.")
            code = query.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code not in ((0, 1) if pid is not None else (0,)):
                raise Fault("cleanup_unverified", "The owned-session query failed.")
        finally:
            if query.poll() is None:
                query.kill()
            query.wait()
    lines = data.splitlines()
    if len(lines) > MAX_PROCESSES:
        raise Fault("cleanup_unverified", "The process query exceeded its entry bound.")
    try:
        return [(int(parts[0]), parts[1].decode("ascii")) for line in lines if (parts := line.split())]
    except (ValueError, IndexError, UnicodeError) as error:
        raise Fault("cleanup_unverified", "The process query returned invalid state.") from error


class Process:
    """Keep the direct child unreaped until all session signaling is finished."""

    def __init__(self):
        supported()
        self.child = None
        self.session = None
        self.exited = False
        self.reaped = False
        self.events = select.kqueue() if sys.platform == "darwin" else None

    def launch(self, argv: list[str], cwd: str, owned_fds: tuple[int, ...]) -> None:
        # Popen acknowledges exec failures separately from a program exiting 127.
        # Keep this object available to the worker even if later observation fails.
        self.child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      start_new_session=True, pass_fds=owned_fds)
        self.session = self.child.pid
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        os.set_blocking(self.child.stdout.fileno(), False)
        if self.events is not None:
            event = select.kevent(self.child.pid, filter=select.KQ_FILTER_PROC,
                                 flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                 fflags=select.KQ_NOTE_EXIT)
            try:
                self._exit_events(self.events.control([event], 1, 0))
            except ProcessLookupError:
                self._exited_before_watch()

    def _exited_before_watch(self) -> None:
        # On macOS a child may already be a zombie before kqueue registration.
        # ESRCH alone is not proof of exit; the unreaped child must still be visible.
        states = _mac_states(time.monotonic() + 1, self.child.pid)
        if not any(pid == self.child.pid and state.startswith("Z") for pid, state in states):
            raise Fault("process_observation_unavailable", "The original child exit cannot be verified.")
        self.exited = True

    def _exit_events(self, events) -> None:
        for event in events:
            if event.flags & select.KQ_EV_ERROR:
                if event.data != errno.ESRCH:
                    raise Fault("process_observation_unavailable", "The original child watch failed.")
                self._exited_before_watch()
            elif event.fflags & select.KQ_NOTE_EXIT:
                self.exited = True

    def observe_exit(self) -> bool:
        if self.child is None or self.exited:
            return self.exited
        if self.reaped or self.child.returncode is not None:
            raise Fault("process_observation_unavailable", "The original child was reaped outside its owner.")
        if self.events is not None:
            self._exit_events(self.events.control(None, 1, 0))
        else:
            result = os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.exited = result is not None
        return self.exited

    def members(self, deadline: float) -> dict[int, int]:
        """Return live members of this session, not an arbitrary process tree."""
        if self.child is None:
            return {}
        if self.reaped:
            raise Fault("cleanup_unverified", "Session identity has already been released.")
        if sys.platform == "darwin":
            candidates = _mac_states(deadline)
        else:
            with os.scandir("/proc") as entries:
                candidates = []
                for entry in entries:
                    if time.monotonic() >= deadline or len(candidates) >= MAX_PROCESSES:
                        raise Fault("cleanup_unverified", "Session enumeration exceeded its bound.")
                    if entry.name.isdecimal():
                        candidates.append((int(entry.name), ""))
        members = {}
        for pid, state in candidates:
            if time.monotonic() >= deadline:
                raise Fault("cleanup_unverified", "Session enumeration exceeded its deadline.")
            if state.startswith("Z"):
                continue
            try:
                if os.getsid(pid) != self.session:
                    continue
                if sys.platform == "linux":
                    # Read state only for verified session members; comm may contain ')'.
                    state = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
                    if state == "Z":
                        continue
                members[pid] = os.getpgid(pid)
            except (ProcessLookupError, FileNotFoundError):
                continue
        return members

    def signal_groups(self, members: dict[int, int], signum: int) -> set[int]:
        if self.reaped:
            raise Fault("cleanup_unverified", "Never signal a reaped execution identity.")
        sent = set()
        for pid, group in members.items():
            if group in sent:
                continue
            try:
                # Revalidate a live member immediately before targeting its group.
                if group > 1 and os.getsid(pid) == self.session and os.getpgid(pid) == group:
                    os.killpg(group, signum)
                    sent.add(group)
            except ProcessLookupError:
                continue
        return sent

    def reap(self) -> int:
        if not self.exited or self.reaped:
            raise Fault("process_observation_unavailable", "Reap only the confirmed original child once.")
        pid, status = os.waitpid(self.child.pid, os.WNOHANG)
        if pid != self.child.pid:
            raise Fault("process_observation_unavailable", "The original child exit was not waitable.")
        self.reaped = True
        self.child.returncode = os.waitstatus_to_exitcode(status)
        return self.child.returncode

    def close(self) -> None:
        if self.child is not None:
            self.child.stdout.close()
        if self.events is not None:
            self.events.close()
