"""Code SSH sessions: confined launch inside a persistent lifetime.

No Tasks, Executions or receipts. Lifetime records hold only supervision metadata, never commands,
output or context, and nothing here restarts a command.
"""

import errno
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
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
from typing import Annotated, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openmachine import __version__
from openmachine.access import macos, rules
from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

PROTOCOL = 1
MAX_REQUEST = 256 * 1024
MAX_FRAME = 512 * 1024
SHARED = Path("/Users/Shared")
HOME_NAME = "openmachine-code-{uid}"
CLIENT_NAME = "client"  # Core's copy of the host's om-code lives in user_temp()/omc/CLIENT_NAME
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
SFTP_SERVERS = ("/usr/libexec/sftp-server", "/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server")
FORWARDED = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2,
             signal.SIGALRM, signal.SIGABRT)
SPAWN_SECONDS = 5
# Each launcher step up to the spawner's acknowledgment; longer than one stalled launch the spawner waits out.
ACKNOWLEDGE_SECONDS = 2 * SPAWN_SECONDS
LifetimeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class Terminal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    term: Annotated[str, Field(pattern=r"^[A-Za-z0-9._+-]{1,64}$")]


# A session's routing to its host's GitHub broker: these exact variables, the broker on loopback over TLS
# (the routing pins its key, so whatever else holds the port after the broker fails Git closed), Git keys
# scoped to that broker, and values bounded to printable text.
BROKER = r"https://127\.0\.0\.1:[1-9][0-9]{0,4}"
GITHUB_COUNT = 11


class GitHub(BaseModel):
    """The host's Git routing for one session, and its om-code client, which restricted sessions run as
    Core's copy (github_client in capabilities)."""
    model_config = ConfigDict(extra="forbid", strict=True)
    environment: Annotated[dict[str, Annotated[str, Field(pattern=r"^[\x20-\x7e]{0,1024}$")]],
                           Field(min_length=5, max_length=3 + 2 * GITHUB_COUNT)]
    client: Annotated[str, Field(min_length=2, max_length=1024)]

    @model_validator(mode="after")
    def _routing(self):
        environment, broker = self.environment, self.environment.get("OM_CODE_BROKER", "")
        count = environment.get("GIT_CONFIG_COUNT", "")
        if (not re.fullmatch(BROKER, broker) or int(broker.rsplit(":", 1)[1]) > 65535
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", environment.get("OM_CODE_CAPABILITY", ""))
                or not re.fullmatch(r"[1-9][0-9]?", count) or int(count) > GITHUB_COUNT):
            raise ValueError("github routing needs a loopback broker, a capability and a Git configuration count")
        pairs = [(f"GIT_CONFIG_KEY_{index}", f"GIT_CONFIG_VALUE_{index}") for index in range(int(count))]
        if set(environment) != {"OM_CODE_BROKER", "OM_CODE_CAPABILITY", "GIT_CONFIG_COUNT", *(name for pair in pairs for name in pair)}:
            raise ValueError("github routing carries exactly its own variables")
        scoped = re.compile(rf"url\.{re.escape(broker)}/[\x21-\x7e]*\.insteadOf|(credential|http)\.{re.escape(broker)}\.[A-Za-z]{{1,32}}")
        if not all(scoped.fullmatch(environment[key]) for key, _ in pairs):
            raise ValueError("github routing configures only its broker")
        if (not os.path.isabs(self.client) or os.path.normpath(self.client) != self.client
                or any(char in self.client for char in ':\0"\\') or any(ord(char) < 32 or ord(char) == 127 for char in self.client)):
            raise ValueError("github routing names its client by an exact absolute path")
        return self


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
    github: GitHub | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if (self.policy == "selected_projects") != (self.scope is not None) or (self.scope is None) != (self.machine is None):
            raise ValueError("selected_projects requires exactly one machine and scope")
        if (self.kind == "exec") != (self.command is not None) or "\0" in (self.command or ""):
            raise ValueError("exec requires exactly one command")
        return self


def capabilities() -> dict:
    reason = macos.lifetime_unavailable_reason()
    restricted = "available" if reason is None else reason
    # github names the routing form Core takes, so a host and Core that disagree on it route nothing.
    value = {"version": __version__, "protocol": PROTOCOL, "kinds": ["shell", "exec", "sftp"], "github": "https",
             "policies": {"full_access": "available", "read_only": restricted, "selected_projects": restricted}}
    if reason is None:
        value["github_client"] = str(_stable_client())  # what restricted sessions' Git routing runs
    return value


def parse(raw: bytes) -> SessionRequest:
    if len(raw) > MAX_REQUEST:
        raise Fault("request_invalid", "The access request exceeds its bound.", effect="known_no_effect")
    try:
        return SessionRequest.model_validate_json(raw)
    except ValidationError as error:
        raise Fault("request_invalid", "Use access protocol 1 with a valid session request.",
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
    hands its launch to that instance's spawner.
    """

    def __init__(self, state: Path, lifetime: str):
        self.directory = private_directory(private_directory(state / "code") / lifetime)
        self.path = self.directory / "record.json"

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None

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


def _environment(request: SessionRequest, account: pwd.struct_passwd, client: Path | None = None) -> dict:
    path = _system_path() if sys.platform == "darwin" else DEFAULT_PATH
    if client is not None:
        path += ":" + str(client.parent)  # so the session finds om-code by name
    environment = {"PATH": path, "USER": account.pw_name,
                   "LOGNAME": account.pw_name, "SHELL": account.pw_shell or "/bin/sh", "HOME": account.pw_dir}
    if request.terminal is not None:
        environment["TERM"] = request.terminal.term
    return environment | request.environment


def _client(request: SessionRequest) -> Path | None:
    """The host's om-code client: an executable regular file at its own physical path. The file and its
    directory belong to root or this user, with no group or other write and no ACL, and lie outside the
    persistent home and every restricted runtime tree, where restricted work could replace what Full
    sessions run."""
    if request.github is None:
        return None
    client = Path(request.github.client)
    restricted = [SHARED / HOME_NAME.format(uid=os.getuid())] + ([macos.user_temp() / "omc"] if sys.platform == "darwin" else [])
    try:
        info, parent = client.lstat(), client.parent.lstat()
        valid = (stat.S_ISREG(info.st_mode) and os.path.realpath(client) == str(client) and os.access(client, os.X_OK)
                 and all(item.st_uid in (0, os.getuid()) and not item.st_mode & 0o022 for item in (info, parent))
                 and not macos.has_acl(client) and not macos.has_acl(client.parent) and not _within(client, restricted))
    except OSError as error:
        raise Fault("github_client_invalid", "The om-code client is unavailable.", effect="known_no_effect") from error
    if not valid:
        raise Fault("github_client_invalid", "The om-code client must be an executable file only its owner can change, "
                    "outside the persistent home and Core's runtime trees.", effect="known_no_effect")
    return client


def _within(client: Path, roots: list[Path]) -> bool:
    """Whether the client lies in one of roots, by physical identity, so no case or link spelling escapes.
    A root that is a link holds nothing: another user may plant one in /Users/Shared, and Core writes
    through none. A client that vanished meanwhile is refused with no effect."""
    identities = set()
    for root in roots:
        try:
            info = root.lstat()
        except OSError:
            continue  # An absent root holds nothing.
        if stat.S_ISDIR(info.st_mode):
            identities.add((info.st_dev, info.st_ino))
    try:
        ancestry = [os.stat(item) for item in [client, *client.parents]]
    except OSError as error:
        raise Fault("github_client_invalid", "The om-code client is unavailable.", effect="known_no_effect") from error
    return any((item.st_dev, item.st_ino) in identities for item in ancestry)


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
        request = parse(_read_line(control))
        account = _account(request.user)
        state = private_directory(state)
        if request.policy == "full_access":
            launch = _admit_full(request, account, state)
        else:
            launch = _admit_restricted(request, account, config_path, state)
    except (Fault, OSError, ValueError) as error:
        failure = (error if isinstance(error, Fault) else
                   Fault("launch_unverified", "The session did not start cleanly; its command may be running.",
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


# ---- Full Access: ordinary shell ----------------------------------------------------------------


def _admit_full(request: SessionRequest, account, state: Path) -> tuple:
    """Make this process a session leader; the caller execs the command in place."""
    program, argv = _argv(request, account)
    environment = _environment(request, account, _client(request)) | (request.github.environment if request.github else {})
    lifetime = Lifetime(state, request.lifetime)
    with lifetime.lock():
        record = lifetime.read()
        if record is not None and record["policy"] != "full_access":
            raise Fault("lifetime_changed", "This lifetime's policy changed; start sessions in a new lifetime.",
                        effect="known_no_effect")
        if os.getsid(0) != os.getpid():
            os.setsid()
        if record is None:
            lifetime.save({"format": 1, "policy": "full_access"})
    if request.terminal is not None:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.chdir(account.pw_dir)
    return os.getpid(), program, argv, environment


# ---- Restricted: one Seatbelt instance per lifetime, entered through its spawner ----------------


def _grant(request: SessionRequest) -> str:
    return macos.digest(json.dumps({"policy": request.policy, "machine": request.machine, "scope": request.scope},
                                   sort_keys=True, separators=(",", ":")))


def _restricted_profile(scope: "rules.Scope | None", state: Path, runtime: Path, home: Path) -> str:
    denied, exceptions = macos.read_denials(macos.account_home())
    control = list(dict.fromkeys([*rules.control_paths(state), state]))
    readable, writable = [], [home]
    if scope is not None:
        for path, mode in scope.grants():
            (writable if mode == "full_access" else readable).append(path)
    return macos.profile(readable=readable, writable=writable, private=runtime, control=control,
                         denied=denied, exceptions=exceptions, executable=_stable_client())


def _stable_client() -> Path:
    """Core's copy of the host's om-code, the one client every restricted instance may run. Restricted
    work cannot write here, and an update replaces the file in place, so it needs no new instance."""
    return macos.user_temp() / "omc" / CLIENT_NAME / "om-code"


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


def _admit_restricted(request: SessionRequest, account, config_path: Path, state: Path) -> tuple:
    if macos.lifetime_unavailable_reason() is not None:
        raise Fault("scope_unsupported", "Restricted Code sessions need the macOS Seatbelt lifetime backend.",
                    effect="known_no_effect")
    program, argv = _argv(request, account)
    # Re-verify every selected binding on each admission: a replaced directory is never rebound.
    scope = None if request.scope is None else rules.compile(load(config_path), state, request.machine, request.scope)
    client = _client(request)
    stable = _install(client) if client else None
    home = persistent_home()
    runtime = _runtime(request.lifetime)
    lifetime = Lifetime(state, request.lifetime)
    grant = _grant(request)
    with lifetime.lock():
        record = lifetime.read() or {"format": 1, "policy": request.policy, "grant_sha256": grant, "instances": []}
        if record["policy"] != request.policy or record.get("grant_sha256") != grant:
            raise Fault("lifetime_changed", "This lifetime's policy or scope changed; start sessions in a new "
                        "lifetime.", effect="known_no_effect")
        # Every instance binds the lifetime's one spawner socket, so only the newest is reachable. Each
        # instance may run Core's copy of om-code, except instances started before that copy existed: a
        # routed session starts a new instance once there, which cannot see the earlier one's jobs.
        newest = record["instances"][-1] if record["instances"] else None
        usable = newest and _spawner_alive(newest) and (stable is None or newest.get("client") == str(stable))
        instance = newest if usable else None
        if instance is None:
            # Selected roots and worktrees are fixed when an instance starts; later sessions reuse them.
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
        environment = macos.environment(_environment(request, account, stable), home) | request.environment | (
            request.github.environment if request.github else {}) | {
            # zsh writes here-documents under TMPPREFIX (default /tmp/zsh), not TMPDIR.
            "TMPDIR": str(session_tmp) + "/", "TMPPREFIX": str(session_tmp / "zsh"), "TMUX_TMPDIR": str(runtime / "tmux")}
        # Connect before another admission can start a newer instance, which rebinds the socket; a
        # connection made first still reaches its own spawner.
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
