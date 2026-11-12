"""What the restricted backends share: the persistent homes' layout, Core's copy of the om-code client,
process identity, and an instance's spawner with its launch frames, relay and exit report."""

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
from typing import NamedTuple
import uuid

from openmachine.access import rules, sessions
from openmachine.access.linux import sandbox
from openmachine.access.macos import seatbelt
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory

# The platform's start(pid), member(pid, marker) and peer(connection).
backend = sandbox if sys.platform == "linux" else seatbelt
CLIENT_NAME = "client"  # Core's copy of the host's om-code lives in this directory of the runtime tree.
MAX_FRAME = 512 * 1024
SPAWN_SECONDS = 5
# Each launcher step up to the spawner's acknowledgment; longer than one stalled launch the spawner waits out.
ACKNOWLEDGE_SECONDS = 2 * SPAWN_SECONDS
KEPT_ENVIRONMENT = ("PATH", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "SSH_CONNECTION", "SSH_CLIENT")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def environment(base: dict, private: Path) -> dict:
    """Construct the command environment; nothing else from the service is inherited."""
    kept = {key: value for key, value in base.items() if key in KEPT_ENVIRONMENT or key.startswith("LC_")}
    return kept | {"HOME": str(private / "home"), "TMPDIR": str(private / "tmp") + "/",
                   "XDG_CACHE_HOME": str(private / "cache"), "XDG_CONFIG_HOME": str(private / "config"),
                   "XDG_DATA_HOME": str(private / "data"), "XDG_STATE_HOME": str(private / "state"),
                   "npm_config_cache": str(private / "cache" / "npm")}


def persistent_home(default: Path, key: str | None) -> tuple[Path, list[Path]]:
    """Create or check this OS user's persistent sandbox homes. Returns the one a session uses, the default
    one or the one of the key its request names, made on first use, and where every other one is: the
    default home and the directory of the keyed ones, which may hold the session's own.

    Every restricted session of one home shares it, whatever its policy and lifetime, so a later session
    may run what an earlier one left there, such as shell startup files or cached packages, but only
    inside its own sandbox. A sandbox reads and writes no home but its own, so nothing left in one home
    reaches a session of another. The others exist before any sandbox starts, so that a mount can hide them.
    """
    keyed = sessions.keyed_homes(default)
    private_home(default)
    _owner_only(keyed)
    return (default, [keyed]) if key is None else (private_home(keyed / key), [default, keyed])


def _owner_only(path: Path) -> bool:
    """Create or check one owner-only directory that is no link and carries no ACL; whether it was created."""
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        created = False
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or seatbelt.has_acl(path):
        raise Fault("persistent_home_unsafe", "The persistent sandbox home must hold owner-only directories without "
                    "links or ACLs; move it aside to recreate it.", effect="known_no_effect")
    return created


def private_home(home: Path) -> Path:
    """Create or check one owner-only sandbox home; unconfined code never trusts its contents."""
    created = _owner_only(home)
    for name in ("home", "cache", "config", "data", "state"):
        _owner_only(home / name)
    if created:
        identity = _git_identity()
        if identity:
            fd = os.open(home / "home" / ".gitconfig", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as config:
                config.write("[user]\n" + "".join(f"\t{key} = {value}\n" for key, value in identity.items()))
    return home


def _git_identity() -> dict[str, str]:
    """Only the user's global Git identity enters the sandbox home."""
    found = {}
    for key in ("name", "email"):
        try:
            result = subprocess.run(["git", "config", "--global", "--get", "user." + key], capture_output=True,
                                    text=True, timeout=5, stdin=subprocess.DEVNULL,
                                    env={"HOME": pwd.getpwuid(os.getuid()).pw_dir, "PATH": sessions.DEFAULT_PATH})
        except (OSError, subprocess.SubprocessError):
            continue
        value = result.stdout.strip()
        if result.returncode == 0 and value and not any(ord(char) < 32 for char in value):
            found[key] = value
    return found


def _scope(request: sessions.SessionRequest, state: Path) -> "rules.Scope | None":
    """A Workspace session's folders, checked on each admission: one whose path changed grants nothing."""
    if request.folders is None:
        return None
    control = rules.control_paths(state)
    return rules.Scope(tuple(rules.folders(request.folders, control, admission=True)), control)


def _record(lifetime: sessions.Lifetime, request: sessions.SessionRequest) -> dict:
    """The lifetime's record, read under its lock: new, or one of the request's policy, folders and home."""
    grant = digest(json.dumps({"policy": request.policy, "folders": request.folders, "home": request.home},
                              sort_keys=True, separators=(",", ":")))
    record = lifetime.read() or {"format": 1, "policy": request.policy, "grant_sha256": grant, "instances": []}
    if record["policy"] != request.policy or record.get("grant_sha256") != grant:
        raise Fault("lifetime_changed", "This lifetime's policy, folders or home changed; start sessions in a new "
                    "lifetime.", effect="known_no_effect")
    return record


def _stable_client() -> Path:
    """Core's copy of the host's om-code, the one client every restricted instance may run. Restricted
    work cannot write here, and an update replaces the file in place, so it needs no new instance."""
    runtime = sandbox.runtime(rules.account_home()) if sys.platform == "linux" else seatbelt.user_temp() / "omc"
    return runtime / CLIENT_NAME / "om-code"


def _install(client: Path) -> Path:
    """Refresh the stable copy when the host's client changed (an update installs a new file, so size or
    modification time differ): written whole and synced, then put in place atomically, so sessions
    running the old copy are unaffected."""
    stable = _stable_client()
    try:
        with open(client, "rb") as source:
            info = os.fstat(source.fileno())
            try:
                current = stable.lstat()
                if stat.S_ISREG(current.st_mode) and (current.st_size, current.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
                    return stable
            except FileNotFoundError:
                pass
            staged = private_directory(private_directory(stable.parent.parent) / stable.parent.name) / f".{uuid.uuid4().hex}"
            try:
                with open(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700), "wb") as copy:
                    shutil.copyfileobj(source, copy)
                    os.fchmod(copy.fileno(), 0o755)
                    copy.flush()
                    os.fsync(copy.fileno())
                os.utime(staged, ns=(info.st_atime_ns, info.st_mtime_ns))
                os.replace(staged, stable)
            finally:
                staged.unlink(missing_ok=True)
    except (OSError, Fault) as error:
        raise Fault("github_client_invalid", "Core could not keep its copy of the om-code client.",
                    effect="known_no_effect") from error
    return stable


class Birth(NamedTuple):
    """A process identity that a reused PID cannot match: PID plus kernel start time."""
    pid: int
    start: int


def birth(pid: int) -> Birth | None:
    """The live identity of pid, or None once it is gone or a zombie."""
    start = backend.start(pid)
    return None if start is None else Birth(pid, start)


def signal_member(target: Birth, marker: str, signum: int) -> bool:
    """Signal only the same birth identity, and only while it is still a member."""
    current = birth(target.pid)
    if current is None or current.start != target.start or backend.member(target.pid, marker) is not True:
        return False
    current = birth(target.pid)
    if current is None or current.start != target.start:
        return False
    try:
        os.kill(target.pid, signum)
    except ProcessLookupError:
        return False
    return True


def signal_group(leader: Birth, marker: str, signum: int) -> bool:
    """Signal a session command's process group, as sshd does, while its leader is verifiably unchanged.

    The group id is the leader's PID and every group member descends from it, so it stays in the lifetime.
    """
    if not signal_member(leader, marker, 0):
        return False
    try:
        os.killpg(leader.pid, signum)
    except ProcessLookupError:
        return False
    return True


def started(frame: dict) -> Birth:
    """The spawner's launch acknowledgment. Only an explicit pre-exec errno proves nothing ran."""
    if isinstance(frame.get("pid"), int) and isinstance(frame.get("start"), int):
        return Birth(frame["pid"], frame["start"])
    if isinstance(frame.get("errno"), int):
        raise Fault("launch_failed", f"The confined command did not start: {os.strerror(frame['errno'])}.",
                    effect="known_no_effect")
    raise Fault("launch_unverified", "The launch acknowledgment was lost; the command may be running.",
                effect="effect_unknown")


def _relay(replies) -> None:
    """Leave with the confined command's exit status; signals are already forwarded."""
    # The channel's EOF belongs to the confined command; never hold its IO open here.
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    os.close(null)
    status = _frame(replies).get("exit")
    if not isinstance(status, int):
        os._exit(255)  # The spawner was lost: the command's outcome is unknown.
    if os.WIFSIGNALED(status):
        signal.signal(os.WTERMSIG(status), signal.SIG_DFL)
        os.kill(os.getpid(), os.WTERMSIG(status))
    os._exit(os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 255)


def _frame(stream, first: bytes = b"") -> dict:
    try:
        value = json.loads(first + stream.readline(MAX_FRAME) or b"{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _spawner_alive(instance: dict) -> bool:
    if instance["pid"] is None:
        return False  # Published but never confirmed started.
    current = birth(instance["pid"])
    return (current is not None and current.start == instance["start"]
            and backend.member(instance["pid"], instance["marker"]) is True)


class _Inside:
    """Use short relative socket names: AF_UNIX paths are limited to 104 bytes."""

    def __init__(self, directory: Path):
        self.directory = directory

    def __enter__(self):
        self.saved = os.open(".", os.O_RDONLY)
        os.chdir(self.directory)

    def __exit__(self, *_):
        os.fchdir(self.saved)
        os.close(self.saved)


def _connect(directory: Path, instance: dict) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(ACKNOWLEDGE_SECONDS)  # Kept until the launch is acknowledged.
        with _Inside(directory):
            connection.connect("spawner")
        peer = backend.peer(connection)
    except OSError as error:
        connection.close()
        raise Fault("lifetime_unavailable", "The lifetime's spawner is not reachable.", effect="known_no_effect") from error
    if peer != instance["pid"] or not _spawner_alive(instance):
        connection.close()
        raise Fault("lifetime_unavailable", "The lifetime's spawner identity changed.", effect="known_no_effect")
    return connection


def _confirm_spawner(reported: bytes, marker: str) -> Birth:
    """The spawner's self-reported birth, once the native self-test classifies it as a member and this
    supervisor as not one. A failed self-test kills the spawner only while that same birth still holds."""
    fields = reported.split()
    claimed = (Birth(int(fields[0]), int(fields[1]))
               if len(fields) == 2 and all(field.isdigit() for field in fields) else None)

    def same() -> bool:
        current = birth(claimed.pid)
        return current is not None and current.start == claimed.start

    if claimed is None:
        raise Fault("lifetime_unverifiable", "The lifetime boundary could not be applied; nothing was launched.",
                    effect="known_no_effect")
    if (same() and backend.member(claimed.pid, marker) is True and backend.member(os.getpid(), marker) is False
            and same()):
        return claimed
    if same():
        try:
            os.kill(claimed.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise Fault("lifetime_unverifiable", "The lifetime boundary failed its native self-test; nothing was launched.",
                effect="known_no_effect")


def _serve(listener: socket.socket, vouch: bool = False) -> None:
    """The in-sandbox spawner: start each session's command in this instance and report its exit.

    Where a PID inside the instance means nothing outside it, each command vouches for itself: before it
    executes, it sends one byte on its launcher's connection, to which the kernel attaches its PID as the
    launcher sees it, and it runs only once the launcher answers."""
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
        signal.signal(signum, signal.SIG_IGN)
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    signal.set_wakeup_fd(wake_write)
    signal.signal(signal.SIGCHLD, lambda *_: None)
    children = {}
    with selectors.DefaultSelector() as selector:
        selector.register(listener, selectors.EVENT_READ)
        selector.register(wake_read, selectors.EVENT_READ)
        while True:
            for key, _ in selector.select():
                if key.fileobj is listener:
                    connection, _ = listener.accept()
                    try:
                        pid = _spawn(connection, vouch)
                        children[pid] = connection
                        selector.register(connection, selectors.EVENT_READ)
                    except (OSError, ValueError, KeyError, TypeError):
                        connection.close()
                elif key.fileobj == wake_read:
                    while True:
                        try:
                            if not os.read(wake_read, 512):
                                break
                        except BlockingIOError:
                            break
                else:
                    # Launcher gone: its command keeps running and is still reaped here.
                    selector.unregister(key.fileobj)
                    for pid, connection in children.items():
                        if connection is key.fileobj:
                            children[pid] = None
                    key.fileobj.close()
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                connection = children.pop(pid, None)
                if connection is not None:
                    try:
                        connection.sendall(json.dumps({"exit": status}).encode() + b"\n")
                    except OSError:
                        pass
                    selector.unregister(connection)
                    connection.close()


def _spawn(connection: socket.socket, vouch: bool = False) -> int:
    connection.settimeout(SPAWN_SECONDS)
    header, fds, _, _ = socket.recv_fds(connection, 4, 3)
    try:
        if len(header) != 4 or len(fds) != 3:
            raise ValueError("invalid launch frame")
        size = struct.unpack("!I", header)[0]
        if size > MAX_FRAME:
            raise ValueError("launch frame too large")
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise ValueError("truncated launch frame")
            data.extend(chunk)
        body = json.loads(data)
        program, argv, environment, cwd, tty = body["program"], body["argv"], body["env"], body["cwd"], body["tty"]
        if (not isinstance(program, str) or not all(isinstance(item, str) for item in argv)
                or not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items())
                or not isinstance(cwd, str) or not isinstance(tty, bool)):
            raise ValueError("invalid launch frame")
        failure_read, failure_write = os.pipe()  # Close-on-exec: EOF acknowledges a successful exec.
        gate_read, gate_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(gate_write)
            _exec(program, argv, environment, cwd, tty, fds, failure_write, gate_read, connection if vouch else None)
        os.close(failure_write)
        os.close(gate_read)
    finally:
        for fd in fds:
            os.close(fd)
    # The child waits at the gate, so this reads a live, unexec'd child's birth even for a command that
    # exits at once. Without one, the gate stays shut and nothing runs.
    current = birth(pid)
    try:
        if current is not None:
            os.write(gate_write, b"\0")
    except BrokenPipeError:
        pass  # Killed at the gate; its exit is reported like any other.
    finally:
        os.close(gate_write)
    with os.fdopen(failure_read, "rb") as failure:
        reported = failure.read(4)
    if reported or current is None:
        os.waitpid(pid, 0)
        code = struct.unpack("!i", reported)[0] if reported else errno.ESRCH
        connection.sendall(json.dumps({"errno": code}).encode() + b"\n")
        raise OSError("launch failed")
    connection.sendall(json.dumps({"pid": pid, "start": current.start}).encode() + b"\n")
    connection.settimeout(None)
    return pid


def _exec(program, argv, environment, cwd, tty, fds, failure: int, gate: int, vouch: socket.socket | None) -> None:
    try:
        signal.set_wakeup_fd(-1)
        sessions.default_signals()
        if os.read(gate, 1) != b"\0":
            raise OSError(errno.ECANCELED, "launch withdrawn")
        os.close(gate)
        if vouch is not None:
            vouch.sendall(b"\0")
            if vouch.recv(1) != b"\0":
                raise OSError(errno.ECANCELED, "launch withdrawn")
        os.setsid()
        for target, fd in enumerate(fds):
            os.dup2(fd, target)
        for fd in fds:
            if fd > 2:
                os.close(fd)  # Descriptors received over SCM_RIGHTS are inheritable on macOS.
        if tty:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.chdir(cwd)
        os.execve(program, argv, environment)
    except BaseException as error:
        code = error.errno if isinstance(error, OSError) and error.errno else errno.EINVAL
        try:
            os.write(failure, struct.pack("!i", code))
        finally:
            os._exit(127)
