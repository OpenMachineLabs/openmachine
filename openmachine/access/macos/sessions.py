"""The restricted backend: one Seatbelt instance per lifetime, entered through its spawner."""

import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import uuid

from openmachine.access import restricted, rules, sessions
from openmachine.access.macos import seatbelt
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory


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
    return restricted.private_home(root / sessions.HOME_NAME.format(uid=os.getuid()))


def _runtime(lifetime: str) -> Path:
    """Per-lifetime runtime tree: per-session TMPDIRs and the lifetime's own tmux namespace.

    Names stay short because tmux appends /tmux-UID/default and AF_UNIX paths are limited to 104 bytes.
    """
    root = private_directory(private_directory(seatbelt.user_temp() / "omc") / restricted.digest(lifetime)[:12])
    for name in ("tmp", "tmux"):
        private_directory(root / name)
    return root


def _restricted_profile(scope: "rules.Scope | None", state: Path, runtime: Path, home: Path) -> str:
    denied, exceptions = seatbelt.read_denials(rules.account_home())
    control = list(dict.fromkeys([*rules.control_paths(state), state]))
    return seatbelt.profile(home=home, folders=[] if scope is None else scope.grants(), private=runtime,
                            control=control, denied=denied, exceptions=exceptions, executable=restricted._stable_client())


def _admit_restricted(request: sessions.SessionRequest, account, state: Path) -> tuple:
    if seatbelt.lifetime_unavailable_reason() is not None:
        raise Fault("scope_unsupported", "Restricted Code sessions need the macOS Seatbelt lifetime backend.",
                    effect="known_no_effect")
    program, argv = sessions._argv(request, account)
    scope = restricted._scope(request, state)
    client = sessions._client(request)
    stable = restricted._install(client) if client else None
    home = persistent_home()
    runtime = _runtime(request.lifetime)
    lifetime = sessions.Lifetime(state, request.lifetime)
    with lifetime.lock():
        record = restricted._record(lifetime, request)
        # Every instance binds the lifetime's one spawner socket, so only the newest is reachable. Each
        # instance may run Core's copy of om-code, except instances started before that copy existed: a
        # routed session starts a new instance once there, which cannot see the earlier one's jobs.
        newest = record["instances"][-1] if record["instances"] else None
        usable = newest and restricted._spawner_alive(newest) and (stable is None or newest.get("client") == str(stable))
        instance = newest if usable else None
        if instance is None:
            # Folders and worktrees are fixed when an instance starts; later sessions reuse them.
            text = _restricted_profile(scope, state, runtime, home)
            # Publish the marker before anything starts, so a failed save, a lost launcher or a stalled
            # spawner never leaves a running instance that no record names.
            instance = {"marker": uuid.uuid4().hex, "pid": None, "start": None, "client": str(restricted._stable_client())}
            record["instances"].append(instance)
            lifetime.save(record)
            instance.update(_start_spawner(lifetime, text, runtime, instance["marker"]))
            lifetime.save(record)
        session_tmp = runtime / "tmp" / uuid.uuid4().hex[:8]
        session_tmp.mkdir(mode=0o700)
        environment = restricted.environment(sessions._environment(request, account, stable), home) | request.environment | (
            request.github.environment if request.github else {}) | {
            # zsh writes here-documents under TMPPREFIX (default /tmp/zsh), not TMPDIR.
            "TMPDIR": str(session_tmp) + "/", "TMPPREFIX": str(session_tmp / "zsh"), "TMUX_TMPDIR": str(runtime / "tmux")}
        # Connect before another admission can start a newer instance, which rebinds the socket; a
        # connection made first still reaches its own spawner.
        connection = restricted._connect(lifetime.directory, instance)
    cwd = request.folders[0] if request.folders else str(home / "home")  # Workspace starts in its first folder.
    body = json.dumps({"program": program, "argv": argv, "env": environment, "cwd": cwd,
                       "tty": request.terminal is not None}).encode()
    socket.send_fds(connection, [struct.pack("!I", len(body))], [0, 1, 2])
    connection.sendall(body)
    replies = connection.makefile("rb")
    child = restricted.started(restricted._frame(replies))  # A stopped or wedged spawner times out here: effect unknown.
    connection.settimeout(None)  # The exit report comes whenever the command ends.
    return child.pid, child, instance["marker"], replies


def _start_spawner(lifetime: sessions.Lifetime, text: str, runtime: Path, marker: str) -> dict:
    """Start one Seatbelt instance whose first process is the spawner; fail closed unless membership verifies."""
    profile = text + seatbelt.lifetime_rules(marker, runtime)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with restricted._Inside(lifetime.directory):
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
                    if select.select([ready_read], [], [], restricted.SPAWN_SECONDS)[0] else None)
    finally:
        os.close(ready_read)
        # The intermediate only forks and exits, so it is done either way. It is still an unreaped child,
        # so its PID cannot have been reused, and SIGKILL ends it even if it is stopped.
        os.kill(first, signal.SIGKILL)
        os.waitpid(first, 0)
    if reported is None:
        raise Fault("lifetime_unverifiable", "The lifetime's spawner did not start in time; nothing was launched.",
                    effect="known_no_effect")
    spawner = restricted._confirm_spawner(reported, marker)
    return {"pid": spawner.pid, "start": spawner.start}


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
    own = restricted.birth(os.getpid())  # The supervisor checks and signals only this identity, never a bare PID.
    try:
        if own is None:
            raise seatbelt.SandboxError("own identity unavailable")
        seatbelt.apply(profile)
    except seatbelt.SandboxError:
        os._exit(1)
    os.write(ready, f"{own.pid} {own.start}".encode())
    os.close(ready)
    restricted._serve(listener)
