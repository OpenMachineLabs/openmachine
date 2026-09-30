"""Owned POSIX pipe/PTY mechanics; no records, routing or task policy."""

import errno
import os
from pathlib import Path
import pty
import select
import selectors
import signal
import struct
import subprocess
import sys
import termios
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
    """Own descriptors and keep the direct child unreaped through all signaling."""

    def __init__(self):
        supported()
        self.child = None
        self.pid = self.session = self.output_fd = None
        self.tty = self.spawned = self.launch_uncertain = False
        self.exited = self.reaped = self.watched = False
        self.events = select.kqueue() if sys.platform == "darwin" else None

    def launch(self, argv: list[str], cwd: str, owned_fds: tuple[int, ...], *,
               tty: bool = False, terminal_size: dict | None = None, deadline: float | None = None) -> None:
        self.tty = tty
        if tty:
            self._launch_tty(argv, cwd, owned_fds, terminal_size, deadline or time.monotonic() + 15)
        else:
            # Popen acknowledges exec failure separately from a program exiting 127.
            self.child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          start_new_session=True, pass_fds=owned_fds)
            self.pid = self.session = self.child.pid
            self.output_fd = self.child.stdout.fileno()
            self.spawned = True
            self.started_at = time.time()
            self.started_monotonic = time.monotonic()
            os.set_blocking(self.output_fd, False)
            self._watch()

    def _launch_tty(self, argv: list[str], cwd: str, owned_fds: tuple[int, ...],
                    size: dict, deadline: float) -> None:
        # Only the fresh single-threaded worker calls this, never the MCP server.
        error_read, error_write = os.pipe()  # CLOEXEC: EOF acknowledges successful exec.
        try:
            pid, master = pty.fork()
        except BaseException:
            os.close(error_read)
            os.close(error_write)
            raise
        if pid == 0:
            os.close(error_read)
            try:
                for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGPIPE, signal.SIGCHLD):
                    signal.signal(signum, signal.SIG_DFL)
                os.chdir(cwd)
                termios.tcsetwinsize(0, (size["rows"], size["cols"]))
                for fd in owned_fds:
                    os.set_inheritable(fd, True)
                os.execvpe(argv[0], argv, os.environ)
            except BaseException as error:
                code = error.errno if isinstance(error, OSError) and error.errno else errno.EINVAL
                try:
                    os.write(error_write, struct.pack("!i", code))
                finally:
                    os._exit(127)
        self.pid = self.session = pid
        self.output_fd = master
        self.launch_uncertain = True
        os.close(error_write)
        try:
            os.set_blocking(master, False)
            os.set_blocking(error_read, False)
            self._watch()
            reported = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(error_read, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Fault("launch_unverified", "The original PTY launch acknowledgement is unavailable.")
                    if not selector.select(min(0.05, remaining)):
                        continue
                    chunk = os.read(error_read, 5)
                    if not chunk:
                        break
                    reported.extend(chunk)
                    if len(reported) > 4:
                        raise Fault("launch_unverified", "The original PTY launch acknowledgement is invalid.")
            if reported:
                if len(reported) != 4:
                    raise Fault("launch_unverified", "The original PTY launch acknowledgement is incomplete.")
                self.launch_uncertain = False
                code = struct.unpack("!i", reported)[0]
                raise OSError(code, os.strerror(code))
            self.spawned = True
            self.launch_uncertain = False
            self.started_at = time.time()
            self.started_monotonic = time.monotonic()
        finally:
            os.close(error_read)

    def _watch(self) -> None:
        if self.watched:
            return
        if self.events is not None:
            event = select.kevent(self.pid, filter=select.KQ_FILTER_PROC,
                                 flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                 fflags=select.KQ_NOTE_EXIT)
            try:
                self._exit_events(self.events.control([event], 1, 0))
            except ProcessLookupError:
                self._exited_before_watch()
        self.watched = True

    def _exited_before_watch(self) -> None:
        # ESRCH alone is not exit evidence; the unreaped child must still be visible.
        states = _mac_states(time.monotonic() + 1, self.pid)
        if not any(pid == self.pid and state.startswith("Z") for pid, state in states):
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
        if self.pid is None or self.exited:
            return self.exited
        if self.reaped or (self.child is not None and self.child.returncode is not None):
            raise Fault("process_observation_unavailable", "The original child was reaped outside its owner.")
        self._watch()
        if self.events is not None:
            self._exit_events(self.events.control(None, 1, 0))
        else:
            result = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.exited = result is not None
        return self.exited

    def read_output(self, size: int) -> bytes:
        try:
            return os.read(self.output_fd, size)
        except OSError as error:
            if self.tty and error.errno == errno.EIO:
                return b""  # Linux PTY hangup, not a general IO-error suppression.
            raise

    def write_stdin(self, data: bytes) -> int:
        if not self.tty:
            raise Fault("unsupported_operation", "Pipe stdin remains closed.")
        return os.write(self.output_fd, data)

    def resize(self, size: dict) -> None:
        if not self.tty:
            raise Fault("unsupported_operation", "Only an original PTY can be resized.")
        termios.tcsetwinsize(self.output_fd, (size["rows"], size["cols"]))

    def interrupt(self, deadline: float) -> None:
        if self.reaped or self.observe_exit():
            raise Fault("execution_finished", "The original command has exited.")
        group = os.tcgetpgrp(self.output_fd) if self.tty else self.session
        members = self.members(deadline)
        selected = {pid: pgid for pid, pgid in members.items() if pgid == group}
        if not selected or (self.tty and os.tcgetpgrp(self.output_fd) != group):
            raise Fault("foreground_unavailable", "The original foreground group cannot be verified.")
        if time.monotonic() >= deadline:
            raise Fault("control_expired", "The bounded control deadline expired before signaling.")
        if group not in self.signal_groups(selected, signal.SIGINT):
            raise Fault("foreground_unavailable", "No verified original foreground group was signaled.")

    def members(self, deadline: float) -> dict[int, int]:
        """Return live members of this session, not an arbitrary process tree."""
        if self.pid is None:
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
                if group > 1 and os.getsid(pid) == self.session and os.getpgid(pid) == group:
                    os.killpg(group, signum)
                    sent.add(group)
            except ProcessLookupError:
                continue
        return sent

    def reap(self) -> int:
        if not self.exited or self.reaped:
            raise Fault("process_observation_unavailable", "Reap only the confirmed original child once.")
        pid, status = os.waitpid(self.pid, os.WNOHANG)
        if pid != self.pid:
            raise Fault("process_observation_unavailable", "The original child exit was not waitable.")
        self.reaped = True
        code = os.waitstatus_to_exitcode(status)
        if self.child is not None:
            self.child.returncode = code
        return code

    def close(self) -> None:
        if self.child is not None:
            self.child.stdout.close()
        elif self.output_fd is not None:
            os.close(self.output_fd)
        if self.events is not None:
            self.events.close()
