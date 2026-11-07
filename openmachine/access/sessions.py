"""Code SSH sessions: confined launch inside a persistent lifetime, and lifetime termination.

No Tasks, Executions or receipts. Lifetime records hold only supervision metadata, never commands,
output or context, and nothing here restarts a command.
"""

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
import sys
import termios
import time
from typing import Annotated, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openmachine.access import macos, rules
from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

PROTOCOL = 1
MAX_REQUEST = 256 * 1024
MAX_FRAME = 512 * 1024
SHARED = Path("/Users/Shared")
HOME_NAME = "openmachine-code-{uid}"
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
SFTP_SERVERS = ("/usr/libexec/sftp-server", "/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server")
FORWARDED = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2,
             signal.SIGALRM, signal.SIGABRT)
SPAWN_SECONDS = 5
# Each launcher step up to the spawner's acknowledgment; longer than one stalled launch the spawner waits out.
ACKNOWLEDGE_SECONDS = 2 * SPAWN_SECONDS
TERMINATE_SECONDS = 10
LifetimeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class Terminal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    term: Annotated[str, Field(pattern=r"^[A-Za-z0-9._+-]{1,64}$")]


class SessionRequest(BaseModel):
    """One SSH channel after Go verified Cloud authority; Core verifies paths and applies confinement."""
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[1]
    user: Annotated[str, Field(min_length=1, max_length=255)]
    lifetime: LifetimeId
    policy: Literal["read_only", "selected_projects", "full_access"]
    machine: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    scope: dict | None = None
    kind: Literal["shell", "exec", "sftp"]
    command: Annotated[str, Field(min_length=1, max_length=65536)] | None = None
    terminal: Terminal | None = None
    environment: Annotated[dict[Annotated[str, Field(pattern=r"^(LANG|LC_[A-Z]{1,32})$")],
                                Annotated[str, Field(pattern=r"^[A-Za-z0-9._@-]{0,128}$")]],
                           Field(max_length=32)] = {}

    @model_validator(mode="after")
    def _consistent(self):
        if (self.policy == "selected_projects") != (self.scope is not None) or (self.scope is None) != (self.machine is None):
            raise ValueError("selected_projects requires exactly one machine and scope")
        if (self.kind == "exec") != (self.command is not None) or "\0" in (self.command or ""):
            raise ValueError("exec requires exactly one command")
        return self


class TerminateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[1]
    lifetime: LifetimeId


def capabilities() -> dict:
    reason = macos.lifetime_unavailable_reason()
    restricted = "available" if reason is None else reason
    return {"protocol": PROTOCOL, "kinds": ["shell", "exec", "sftp"],
            "policies": {"full_access": "available", "read_only": restricted, "selected_projects": restricted}}


def parse(model, raw: bytes):
    if len(raw) > MAX_REQUEST:
        raise Fault("request_invalid", "The access request exceeds its bound.", effect="known_no_effect")
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        raise Fault("request_invalid", "Use access protocol 1 with a valid session or terminate request.",
                    effect="known_no_effect") from error


def persistent_home() -> Path:
    """The one owner-only restricted-mode home of this OS user; unconfined code never trusts its contents.

    Every restricted policy and lifetime shares it, so a later session may run what an earlier one left
    here, such as shell startup files or cached packages, but only inside the later session's own sandbox.
    """
    root = SHARED
    try:
        parent = root.lstat()
    except OSError as error:
        raise Fault("persistent_home_unsafe", f"{root} is unavailable.", effect="known_no_effect") from error
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
        raise Fault("persistent_home_unsafe", "The shared parent must be root-owned and protect its entries.",
                    effect="known_no_effect")
    home = root / HOME_NAME.format(uid=os.getuid())
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
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or macos.has_acl(path):
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
                                    env={"HOME": pwd.getpwuid(os.getuid()).pw_dir, "PATH": DEFAULT_PATH})
        except (OSError, subprocess.SubprocessError):
            continue
        value = result.stdout.strip()
        if result.returncode == 0 and value and not any(ord(char) < 32 for char in value):
            found[key] = value
    return found


class Lifetime:
    """Protected supervision metadata under Core state, which every sandbox denies.

    Admission holds the lifetime lock while it checks the record and selects or starts an instance, then
    hands its launch to that instance's spawner. Termination holds it throughout and kills every member,
    the spawner included, so a launch still in flight either becomes a member that is killed or fails.
    Terminating retires the id for good: the record stays as a tombstone that refuses later admissions and
    replays the termination result.
    """

    def __init__(self, state: Path, lifetime: str):
        self.directory = private_directory(private_directory(state / "code") / lifetime)
        self.path = self.directory / "record.json"

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None

    def admit(self, fresh: dict) -> dict:
        """The record a new session joins; call with the lock held."""
        record = self.read() or fresh
        if record.get("retired"):
            raise Fault("lifetime_terminated", "This lifetime was terminated; start sessions in a new lifetime.",
                        effect="known_no_effect")
        return record

    def save(self, record: dict) -> None:
        save_json(self.path, record)

    def lock(self):
        return lease(self.directory / "lock", wait=True)


def _runtime(lifetime: str) -> Path:
    """Per-lifetime runtime tree: per-session TMPDIRs and the lifetime's own tmux namespace.

    Names stay short because tmux appends /tmux-UID/default and AF_UNIX paths are limited to 104 bytes.
    """
    root = private_directory(private_directory(macos.user_temp() / "omc") / macos.digest(lifetime)[:12])
    for name in ("tmp", "tmux"):
        private_directory(root / name)
    return root


def _account(user: str) -> pwd.struct_passwd:
    account = pwd.getpwuid(os.getuid())
    if user != account.pw_name or os.getuid() == 0:
        raise Fault("user_rejected", "Sessions run only as the non-root OS user running Open Machine.",
                    effect="known_no_effect")
    return account


def _argv(request: SessionRequest, account: pwd.struct_passwd) -> tuple[str, list[str]]:
    shell = account.pw_shell or "/bin/sh"
    if request.kind == "shell":
        return shell, ["-" + Path(shell).name]
    if request.kind == "exec":
        return shell, [shell, "-c", request.command]
    server = next((path for path in SFTP_SERVERS if os.access(path, os.X_OK)), None)
    if server is None:
        raise Fault("sftp_unavailable", "No sftp-server is installed on this machine.", effect="known_no_effect")
    return server, [server]


def _system_path() -> str:
    """The PATH macOS login shells get from path_helper: /etc/paths, then /etc/paths.d in name order."""
    sources = [Path("/etc/paths")]
    try:
        sources += sorted(Path("/etc/paths.d").iterdir())
    except OSError:
        pass
    entries = []
    for source in sources:
        try:
            entries += [line.strip() for line in source.read_text().splitlines() if line.strip()]
        except OSError:
            continue
    return ":".join(dict.fromkeys(entries)) or DEFAULT_PATH


def _environment(request: SessionRequest, account: pwd.struct_passwd) -> dict:
    environment = {"PATH": _system_path() if sys.platform == "darwin" else DEFAULT_PATH, "USER": account.pw_name,
                   "LOGNAME": account.pw_name, "SHELL": account.pw_shell or "/bin/sh", "HOME": account.pw_dir}
    if request.terminal is not None:
        environment["TERM"] = request.terminal.term
    return environment | request.environment


def _reply(control: int, value: dict) -> None:
    data = json.dumps(value, sort_keys=True).encode() + b"\n"
    while data:
        data = data[os.write(control, data):]


def _read_line(fd: int) -> bytes:
    data = bytearray()
    while not data.endswith(b"\n") and len(data) <= MAX_REQUEST:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def session(control: int, config_path: Path, state: Path) -> None:
    """Admit one channel. fds 0-2 are its IO (a PTY slave when terminal is set); control carries JSON lines.

    Exactly one reply follows the request: denied, or admitted once the command is running.
    """
    try:
        request = parse(SessionRequest, _read_line(control))
        account = _account(request.user)
        state = private_directory(state)
        if request.policy == "full_access":
            launch = _admit_full(request, account, state)
        else:
            launch = _admit_restricted(request, account, config_path, state)
    except (Fault, OSError, ValueError) as error:
        failure = (error if isinstance(error, Fault) else
                   Fault("launch_unverified", "The session did not start cleanly; terminate its lifetime if unsure.",
                         effect="effect_unknown"))
        _reply(control, {"status": "denied", "error": {"code": failure.code, "message": str(failure),
                                                       "effect": failure.effect}})
        os._exit(1)
    if request.policy != "full_access":
        _, child, marker, _ = launch
        # Like sshd: the command's whole process group (its own PID after setsid), never detached sessions.
        # Installed before the reply, so no forwarded signal can arrive first and end this relay instead.
        for signum in FORWARDED:
            signal.signal(signum, lambda number, _: macos.signal_group(child, marker, number))
    _reply(control, {"status": "admitted", "policy": request.policy, "pid": launch[0]})
    os.close(control)
    if request.policy == "full_access":
        _, program, argv, environment = launch
        try:
            os.execve(program, argv, environment)
        finally:
            os._exit(127)
    _relay(launch[3])


# ---- Full Access: ordinary shell, best-effort cleanup -------------------------------------------


def _admit_full(request: SessionRequest, account, state: Path) -> tuple:
    """Record this process as a session leader; the caller execs the command in place."""
    program, argv = _argv(request, account)
    environment = _environment(request, account)
    lifetime = Lifetime(state, request.lifetime)
    with lifetime.lock():
        record = lifetime.admit({"format": 1, "policy": "full_access", "sessions": []})
        if record["policy"] != "full_access":
            raise Fault("lifetime_changed", "This lifetime's policy changed; terminate it before admitting sessions.",
                        effect="known_no_effect")
        record["sessions"] = [item for item in record["sessions"] if _session_members(*item)]
        if os.getsid(0) != os.getpid():
            os.setsid()
        record["sessions"].append([os.getpid(), _birth(os.getpid())])
        lifetime.save(record)
    if request.terminal is not None:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.chdir(account.pw_dir)
    return os.getpid(), program, argv, environment


def _birth(pid: int) -> int | None:
    if sys.platform == "darwin":
        current = macos.birth(pid)
        return None if current is None else current.start
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None
    return None if fields[0] == "Z" else int(fields[19])


def _pids() -> list[int]:
    if sys.platform == "darwin":
        return macos.pids()
    return [int(name) for name in os.listdir("/proc") if name.isdecimal()]


def _session_members(leader: int, start: int) -> list[tuple[int, int]]:
    """(PID, birth) of each process of a recorded session, found only while its leader still verifies.

    A session id is a PID number. Once the leader is gone, nothing proves the session never ended and the
    number was not reused by another session, so its remaining processes are left alone: Full Access
    cleanup never claims work it cannot attribute, and it stays unverified.
    """
    if _birth(leader) != start:
        return []
    return [(pid, born) for pid in _pids() if pid > 1 and (born := _owned(pid, leader, start)) is not None]


def _owned(pid: int, leader: int, start: int) -> int | None:
    """pid's birth if it is in the verified leader's session right now, observed with an unchanged birth."""
    try:
        born = _birth(pid)
        if born is None or os.getsid(pid) != leader or _birth(pid) != born or _birth(leader) != start:
            return None
    except (ProcessLookupError, PermissionError):
        return None
    return born


def _end_full(sessions: list) -> None:
    for leader, start in sessions:
        # The leader goes last: it anchors every other target's session.
        for pid, born in sorted(_session_members(leader, start), key=lambda item: item[0] == leader):
            try:
                if _owned(pid, leader, start) == born:  # Re-verify the actual target right before signalling.
                    os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue  # Gone, or elevated (sudo) work this user cannot signal: best effort goes on.


# ---- Restricted: one Seatbelt instance per lifetime, entered through its spawner ----------------


def _grant(request: SessionRequest) -> str:
    return macos.digest(json.dumps({"policy": request.policy, "machine": request.machine, "scope": request.scope},
                                   sort_keys=True, separators=(",", ":")))


def _restricted_profile(scope: "rules.Scope | None", state: Path, runtime: Path, home: Path) -> str:
    denied, exceptions = macos.read_denials(macos.account_home())
    control = list(dict.fromkeys([*rules.control_paths(state), state]))
    readable, writable = [], [home]
    if scope is not None:
        mapped = scope.process_request()
        for item in [*mapped["roots"], *mapped["worktrees"]]:
            (writable if item["mode"] == "full_access" else readable).append(Path(item["path"]))
    return macos.profile(readable=readable, writable=writable, private=runtime, control=control,
                         denied=denied, exceptions=exceptions)


def _admit_restricted(request: SessionRequest, account, config_path: Path, state: Path) -> tuple:
    if macos.lifetime_unavailable_reason() is not None:
        raise Fault("scope_unsupported", "Restricted Code sessions need the macOS Seatbelt lifetime backend.",
                    effect="known_no_effect")
    program, argv = _argv(request, account)
    # Re-verify every selected binding on each admission: a replaced directory is never rebound.
    scope = None if request.scope is None else rules.compile(load(config_path), state, request.machine, request.scope)
    home = persistent_home()
    runtime = _runtime(request.lifetime)
    lifetime = Lifetime(state, request.lifetime)
    grant = _grant(request)
    with lifetime.lock():
        record = lifetime.admit({"format": 1, "policy": request.policy, "grant_sha256": grant,
                                 "runtime": str(runtime), "instances": []})
        if record["policy"] != request.policy or record.get("grant_sha256") != grant:
            raise Fault("lifetime_changed", "This lifetime's policy or scope changed; terminate it before admitting "
                        "sessions.", effect="known_no_effect")
        instance = next((item for item in reversed(record["instances"]) if _spawner_alive(item)), None)
        if instance is None:
            # Selected roots and worktrees are fixed when an instance starts; later sessions reuse them.
            text = _restricted_profile(scope, state, runtime, home)
            # Publish the marker before anything starts: terminate finds an instance by marker alone, so a
            # failed save or a lost launcher never leaves a running instance that no record names.
            instance = {"marker": uuid.uuid4().hex, "pid": None, "start": None}
            record["instances"].append(instance)
            lifetime.save(record)
            instance.update(_start_spawner(lifetime, text, runtime, instance["marker"]))
            lifetime.save(record)
    session_tmp = runtime / "tmp" / uuid.uuid4().hex[:8]
    session_tmp.mkdir(mode=0o700)
    environment = macos.environment(_environment(request, account), home) | request.environment | {
        # zsh writes here-documents under TMPPREFIX (default /tmp/zsh), not TMPDIR.
        "TMPDIR": str(session_tmp) + "/", "TMPPREFIX": str(session_tmp / "zsh"), "TMUX_TMPDIR": str(runtime / "tmux")}
    connection = _connect(lifetime, instance)
    body = json.dumps({"program": program, "argv": argv, "env": environment, "cwd": str(home / "home"),
                       "tty": request.terminal is not None}).encode()
    socket.send_fds(connection, [struct.pack("!I", len(body))], [0, 1, 2])
    connection.sendall(body)
    replies = connection.makefile("rb")
    child = started(_frame(replies))  # A stopped or wedged spawner times out here: effect unknown.
    connection.settimeout(None)  # The exit report comes whenever the command ends.
    return child.pid, child, instance["marker"], replies


def started(frame: dict) -> "macos.Birth":
    """The spawner's launch acknowledgment. Only an explicit pre-exec errno proves nothing ran."""
    if isinstance(frame.get("pid"), int) and isinstance(frame.get("start"), int):
        return macos.Birth(frame["pid"], frame["start"])
    if isinstance(frame.get("errno"), int):
        raise Fault("launch_failed", f"The confined command did not start: {os.strerror(frame['errno'])}.",
                    effect="known_no_effect")
    raise Fault("launch_unverified", "The launch acknowledgment was lost; the command may be running. Terminate "
                "the lifetime before retrying.", effect="effect_unknown")


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
    current = macos.birth(instance["pid"])
    return (current is not None and current.start == instance["start"]
            and macos.member(instance["pid"], instance["marker"]) is True)


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


def _connect(lifetime: Lifetime, instance: dict) -> socket.socket:
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


def _start_spawner(lifetime: Lifetime, text: str, runtime: Path, marker: str) -> dict:
    """Start one Seatbelt instance whose first process is the spawner; fail closed unless membership verifies."""
    profile = text + macos.lifetime_rules(marker, runtime)
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
    os.waitpid(first, 0)
    try:
        # Bounded: this runs under the lifetime lock that terminate needs. A spawner that reports later
        # finds the pipe closed and exits; one stuck inside its sandbox is a member that terminate ends.
        if not select.select([ready_read], [], [], SPAWN_SECONDS)[0]:
            raise Fault("lifetime_unverifiable", "The lifetime's spawner did not start in time; nothing was "
                        "launched.", effect="known_no_effect")
        reported = os.read(ready_read, 64)  # One short write: atomic on a pipe.
    finally:
        os.close(ready_read)
    spawner = _confirm_spawner(reported, marker)
    return {"pid": spawner.pid, "start": spawner.start}


def _confirm_spawner(reported: bytes, marker: str) -> "macos.Birth":
    """The spawner's self-reported birth, once the native self-test classifies it as a member and this
    supervisor as not one. A failed self-test kills the spawner only while that same birth still holds."""
    fields = reported.split()
    claimed = (macos.Birth(int(fields[0]), int(fields[1]))
               if len(fields) == 2 and all(field.isdigit() for field in fields) else None)

    def same() -> bool:
        current = macos.birth(claimed.pid)
        return current is not None and current.start == claimed.start

    if claimed is None:
        raise Fault("lifetime_unverifiable", "The lifetime boundary could not be applied; nothing was launched.",
                    effect="known_no_effect")
    if (same() and macos.member(claimed.pid, marker) is True and macos.member(os.getpid(), marker) is False
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
    own = macos.birth(os.getpid())  # The supervisor checks and signals only this identity, never a bare PID.
    try:
        if own is None:
            raise macos.SandboxError("own identity unavailable")
        macos.apply(profile)
    except macos.SandboxError:
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
    current = macos.birth(pid)
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
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE, signal.SIGCHLD):
            signal.signal(signum, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, set())
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


# ---- Termination ----------------------------------------------------------------------------------


def terminate(state: Path, raw: bytes) -> dict:
    """End every process of one lifetime and retire its id. Restricted cleanup is verified; Full Access is
    best effort. The lock orders this against instance selection and start (see Lifetime), and a repeated
    call replays the result, or retries cleanup that did not verify.
    """
    request = parse(TerminateRequest, raw)
    lifetime = Lifetime(private_directory(state), request.lifetime)
    with lifetime.lock():
        record = lifetime.read() or {"format": 1, "policy": None}
        if "result" in record:
            return record["result"]
        record["retired"] = True
        lifetime.save(record)  # From here on no admission can start another command in this lifetime.
        if record["policy"] is None:
            result = {"status": "not_found"}
        elif record["policy"] == "full_access":
            _end_full(record["sessions"])
            result = {"status": "termination_unverified", "reason": "full_access_best_effort"}
        else:
            deadline = time.monotonic() + TERMINATE_SECONDS
            try:
                for instance in record["instances"]:
                    macos.end_lifetime(instance["marker"], deadline)
            except Fault as error:
                return {"status": "termination_unverified", "reason": error.code}  # Kept for a retry.
            shutil.rmtree(record["runtime"], ignore_errors=True)
            (lifetime.directory / "spawner").unlink(missing_ok=True)
            result = {"status": "verified"}
        record["result"] = result
        lifetime.save(record)
        return result
