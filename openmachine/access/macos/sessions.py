"""The restricted backend: one Seatbelt instance per lifetime, entered through its spawner."""

import errno
import fcntl
import json
import os
from pathlib import Path
import pwd
import select
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import termios
import uuid

from openmachine.access import rules, sessions
from openmachine.access.macos import seatbelt
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory

CLIENT_NAME = "client"  # Core's copy of the host's om-code lives in user_temp()/omc/CLIENT_NAME
MAX_FRAME = 512 * 1024
SPAWN_SECONDS = 5
# Each launcher step up to the spawner's acknowledgment; longer than one stalled launch the spawner waits out.
ACKNOWLEDGE_SECONDS = 2 * SPAWN_SECONDS


def persistent_home() -> Path:
    """The one owner-only restricted-mode home of this OS user; unconfined code never trusts its contents.

    Every restricted policy and lifetime shares it, so a later session may run what an earlier one left
    here, such as shell startup files or cached packages, but only inside the later session's own sandbox.
    """
    root = sessions.SHARED
    try:
        parent = root.lstat()
    except OSError as error:
        raise Fault("persistent_home_unsafe", f"{root} is unavailable.", effect="known_no_effect") from error
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
        raise Fault("persistent_home_unsafe", "The shared parent must be root-owned and protect its entries.",
                    effect="known_no_effect")
    home = root / sessions.HOME_NAME.format(uid=os.getuid())
    try:
        home.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        created = False
    for path in (home, *(home / name for name in ("home", "cache", "config", "data", "state"))):
        if path != home:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or seatbelt.has_acl(path):
            raise Fault("persistent_home_unsafe", "The persistent sandbox home must hold owner-only directories without "
                        "links or ACLs; move it aside to recreate it.", effect="known_no_effect")
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


def _runtime(lifetime: str) -> Path:
    """Per-lifetime runtime tree: per-session TMPDIRs and the lifetime's own tmux namespace.

    Names stay short because tmux appends /tmux-UID/default and AF_UNIX paths are limited to 104 bytes.
    """
    root = private_directory(private_directory(seatbelt.user_temp() / "omc") / seatbelt.digest(lifetime)[:12])
    for name in ("tmp", "tmux"):
        private_directory(root / name)
    return root


def _grant(request: sessions.SessionRequest) -> str:
    return seatbelt.digest(json.dumps({"policy": request.policy, "folders": request.folders},
                                      sort_keys=True, separators=(",", ":")))


def _restricted_profile(scope: "rules.Scope | None", state: Path, runtime: Path, home: Path) -> str:
    denied, exceptions = seatbelt.read_denials(seatbelt.account_home())
    control = list(dict.fromkeys([*rules.control_paths(state), state]))
    return seatbelt.profile(home=home, folders=[] if scope is None else scope.grants(), private=runtime,
                            control=control, denied=denied, exceptions=exceptions, executable=_stable_client())


def _stable_client() -> Path:
    """Core's copy of the host's om-code, the one client every restricted instance may run. Restricted
    work cannot write here, and an update replaces the file in place, so it needs no new instance."""
    return seatbelt.user_temp() / "omc" / CLIENT_NAME / "om-code"


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


def _admit_restricted(request: sessions.SessionRequest, account, state: Path) -> tuple:
    if seatbelt.lifetime_unavailable_reason() is not None:
        raise Fault("scope_unsupported", "Restricted Code sessions need the macOS Seatbelt lifetime backend.",
                    effect="known_no_effect")
    program, argv = sessions._argv(request, account)
    scope = None
    if request.folders is not None:  # Re-checked on each admission: a folder whose path changed grants nothing.
        control = rules.control_paths(state)
        scope = rules.Scope(tuple(rules.folders(request.folders, control, admission=True)), control)
    client = sessions._client(request)
    stable = _install(client) if client else None
    home = persistent_home()
    runtime = _runtime(request.lifetime)
    lifetime = sessions.Lifetime(state, request.lifetime)
    grant = _grant(request)
    with lifetime.lock():
        record = lifetime.read() or {"format": 1, "policy": request.policy, "grant_sha256": grant, "instances": []}
        if record["policy"] != request.policy or record.get("grant_sha256") != grant:
            raise Fault("lifetime_changed", "This lifetime's policy or folders changed; start sessions in a new "
                        "lifetime.", effect="known_no_effect")
        # Every instance binds the lifetime's one spawner socket, so only the newest is reachable. Each
        # instance may run Core's copy of om-code, except instances started before that copy existed: a
        # routed session starts a new instance once there, which cannot see the earlier one's jobs.
        newest = record["instances"][-1] if record["instances"] else None
        usable = newest and _spawner_alive(newest) and (stable is None or newest.get("client") == str(stable))
        instance = newest if usable else None
        if instance is None:
            # Folders and worktrees are fixed when an instance starts; later sessions reuse them.
            text = _restricted_profile(scope, state, runtime, home)
            # Publish the marker before anything starts, so a failed save, a lost launcher or a stalled
            # spawner never leaves a running instance that no record names.
            instance = {"marker": uuid.uuid4().hex, "pid": None, "start": None, "client": str(_stable_client())}
            record["instances"].append(instance)
            lifetime.save(record)
            instance.update(_start_spawner(lifetime, text, runtime, instance["marker"]))
            lifetime.save(record)
        session_tmp = runtime / "tmp" / uuid.uuid4().hex[:8]
        session_tmp.mkdir(mode=0o700)
        environment = seatbelt.environment(sessions._environment(request, account, stable), home) | request.environment | (
            request.github.environment if request.github else {}) | {
            # zsh writes here-documents under TMPPREFIX (default /tmp/zsh), not TMPDIR.
            "TMPDIR": str(session_tmp) + "/", "TMPPREFIX": str(session_tmp / "zsh"), "TMUX_TMPDIR": str(runtime / "tmux")}
        # Connect before another admission can start a newer instance, which rebinds the socket; a
        # connection made first still reaches its own spawner.
        connection = _connect(lifetime, instance)
    cwd = request.folders[0] if request.folders else str(home / "home")  # Workspace starts in its first folder.
    body = json.dumps({"program": program, "argv": argv, "env": environment, "cwd": cwd,
                       "tty": request.terminal is not None}).encode()
    socket.send_fds(connection, [struct.pack("!I", len(body))], [0, 1, 2])
    connection.sendall(body)
    replies = connection.makefile("rb")
    child = started(_frame(replies))  # A stopped or wedged spawner times out here: effect unknown.
    connection.settimeout(None)  # The exit report comes whenever the command ends.
    return child.pid, child, instance["marker"], replies


def started(frame: dict) -> "seatbelt.Birth":
    """The spawner's launch acknowledgment. Only an explicit pre-exec errno proves nothing ran."""
    if isinstance(frame.get("pid"), int) and isinstance(frame.get("start"), int):
        return seatbelt.Birth(frame["pid"], frame["start"])
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


def _frame(stream) -> dict:
    try:
        value = json.loads(stream.readline(MAX_FRAME) or b"{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _spawner_alive(instance: dict) -> bool:
    if instance["pid"] is None:
        return False  # Published but never confirmed started.
    current = seatbelt.birth(instance["pid"])
    return (current is not None and current.start == instance["start"]
            and seatbelt.member(instance["pid"], instance["marker"]) is True)


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


def _connect(lifetime: sessions.Lifetime, instance: dict) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(ACKNOWLEDGE_SECONDS)  # Kept until the launch is acknowledged.
        with _Inside(lifetime.directory):
            connection.connect("spawner")
        peer = connection.getsockopt(0, 2)  # SOL_LOCAL, LOCAL_PEERPID
    except OSError as error:
        connection.close()
        raise Fault("lifetime_unavailable", "The lifetime's spawner is not reachable.", effect="known_no_effect") from error
    if peer != instance["pid"] or not _spawner_alive(instance):
        connection.close()
        raise Fault("lifetime_unavailable", "The lifetime's spawner identity changed.", effect="known_no_effect")
    return connection


def _start_spawner(lifetime: sessions.Lifetime, text: str, runtime: Path, marker: str) -> dict:
    """Start one Seatbelt instance whose first process is the spawner; fail closed unless membership verifies."""
    profile = text + seatbelt.lifetime_rules(marker, runtime)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with _Inside(lifetime.directory):
        Path("spawner").unlink(missing_ok=True)
        listener.bind("spawner")  # In Core state: sandboxed work can neither reach nor replace it.
    listener.listen(16)
    ready_read, ready_write = os.pipe()
    first = os.fork()
    if first == 0:
        try:
            os.close(ready_read)
            os.setsid()
            if os.fork() == 0:
                _become_spawner(listener, ready_write, profile)
        finally:
            os._exit(0)
    os.close(ready_write)
    listener.close()
    try:
        # Bounded: this runs under the lifetime lock that every admission needs. A spawner that reports
        # too late is never used: its instance stays recorded but unconfirmed.
        reported = (os.read(ready_read, 64)  # One short write: atomic on a pipe.
                    if select.select([ready_read], [], [], SPAWN_SECONDS)[0] else None)
    finally:
        os.close(ready_read)
        # The intermediate only forks and exits, so it is done either way. It is still an unreaped child,
        # so its PID cannot have been reused, and SIGKILL ends it even if it is stopped.
        os.kill(first, signal.SIGKILL)
        os.waitpid(first, 0)
    if reported is None:
        raise Fault("lifetime_unverifiable", "The lifetime's spawner did not start in time; nothing was launched.",
                    effect="known_no_effect")
    spawner = _confirm_spawner(reported, marker)
    return {"pid": spawner.pid, "start": spawner.start}


def _confirm_spawner(reported: bytes, marker: str) -> "seatbelt.Birth":
    """The spawner's self-reported birth, once the native self-test classifies it as a member and this
    supervisor as not one. A failed self-test kills the spawner only while that same birth still holds."""
    fields = reported.split()
    claimed = (seatbelt.Birth(int(fields[0]), int(fields[1]))
               if len(fields) == 2 and all(field.isdigit() for field in fields) else None)

    def same() -> bool:
        current = seatbelt.birth(claimed.pid)
        return current is not None and current.start == claimed.start

    if claimed is None:
        raise Fault("lifetime_unverifiable", "The lifetime boundary could not be applied; nothing was launched.",
                    effect="known_no_effect")
    if (same() and seatbelt.member(claimed.pid, marker) is True and seatbelt.member(os.getpid(), marker) is False
            and same()):
        return claimed
    if same():
        try:
            os.kill(claimed.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    raise Fault("lifetime_unverifiable", "The lifetime boundary failed its native self-test; nothing was launched.",
                effect="known_no_effect")


def _become_spawner(listener: socket.socket, ready: int, profile: str) -> None:
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    keep = {0, 1, 2, listener.fileno(), ready}
    for fd in [int(name) for name in os.listdir("/dev/fd")]:
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass
    own = seatbelt.birth(os.getpid())  # The supervisor checks and signals only this identity, never a bare PID.
    try:
        if own is None:
            raise seatbelt.SandboxError("own identity unavailable")
        seatbelt.apply(profile)
    except seatbelt.SandboxError:
        os._exit(1)
    os.write(ready, f"{own.pid} {own.start}".encode())
    os.close(ready)
    _serve(listener)


def _serve(listener: socket.socket) -> None:
    """The in-sandbox spawner: start each session's command in this instance and report its exit."""
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
                        pid = _spawn(connection)
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


def _spawn(connection: socket.socket) -> int:
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
            _exec(program, argv, environment, cwd, tty, fds, failure_write, gate_read)
        os.close(failure_write)
        os.close(gate_read)
    finally:
        for fd in fds:
            os.close(fd)
    # The child waits at the gate, so this reads a live, unexec'd child's birth even for a command that
    # exits at once. Without one, the gate stays shut and nothing runs.
    current = seatbelt.birth(pid)
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


def _exec(program, argv, environment, cwd, tty, fds, failure: int, gate: int) -> None:
    try:
        signal.set_wakeup_fd(-1)
        sessions.default_signals()
        if os.read(gate, 1) != b"\0":
            raise OSError(errno.ECANCELED, "launch withdrawn")
        os.close(gate)
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
