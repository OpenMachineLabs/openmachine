"""The restricted backend: one bubblewrap instance per lifetime, entered through its spawner."""

import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys

from openmachine.access import restricted, rules, sessions
from openmachine.access.linux import sandbox
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

SPAWNER = "from openmachine.access.linux import sessions; sessions._spawner()"


def persistent_home(key: str | None) -> tuple[Path, list[Path]]:
    """This OS user's sandbox home for a session and where every other one is (restricted.persistent_home)."""
    home = sandbox.home(rules.account_home())
    try:
        home.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Fault("persistent_home_unsafe", f"{home.parent} cannot be created; nothing was launched.",
                    effect="known_no_effect") from error
    return restricted.persistent_home(home, key)


def _admit_restricted(request: sessions.SessionRequest, account, state: Path) -> tuple:
    bubblewrap = sandbox.bubblewrap()
    if bubblewrap is None:
        raise sandbox.unavailable()
    program, argv = sessions._argv(request, account)
    scope = restricted._scope(request, state)
    client = sessions._client(request)
    stable = restricted._install(client) if client else None
    lifetime = sessions.Lifetime(state, request.lifetime)
    with lifetime.lock():
        record = restricted._record(lifetime, request)
        home, others = persistent_home(request.home)  # Made only for a request its lifetime takes.
        # The lifetime's directory in the runtime tree, bound into its instance: the spawner listens in it.
        private = private_directory(sandbox.runtime(rules.account_home()) / restricted.digest(request.lifetime)[:12])
        # Every instance binds the lifetime's one spawner socket, so only the newest is reachable.
        newest = record["instances"][-1] if record["instances"] else None
        instance = newest if newest and restricted._spawner_alive(newest) else None
        connection = None
        if instance:
            try:
                connection = restricted._connect(private, instance)
            except Fault as fault:
                # Its own work can write the socket: with none there, or another process's, the instance is
                # ended. A spawner that is only busy keeps its lifetime's work.
                if not isinstance(fault.__cause__, (FileNotFoundError, ConnectionRefusedError, type(None))):
                    raise
                _end(instance)
                instance = None
        if instance is None:
            # Folders, worktrees and what is protected in them are fixed when an instance starts.
            instance, release = _start_instance(bubblewrap, scope, state, home, others, private)
            with release:  # Closed unanswered, however this process ends: no instance runs that no record names.
                try:
                    record["instances"].append(instance)
                    lifetime.save(record)
                    release.sendall(b"\0")
                except OSError as error:
                    raise Fault("lifetime_unavailable", "The lifetime's record could not be saved; nothing was "
                                "launched.", effect="known_no_effect") from error
            # Connect before another admission can start a newer instance, which rebinds the socket; a
            # connection made first still reaches its own spawner.
            connection = restricted._connect(private, instance)
        environment = restricted.environment(sessions._environment(request, account, stable), home) | request.environment | (
            request.github.environment if request.github else {}) | {
            "TMPDIR": "/tmp", "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}  # The instance's own.
    cwd = request.folders[0] if request.folders else str(home / "home")  # Workspace starts in its first folder.
    body = json.dumps({"program": program, "argv": argv, "env": environment, "cwd": cwd,
                       "tty": request.terminal is not None}).encode()
    try:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        socket.send_fds(connection, [struct.pack("!I", len(body))], [0, 1, 2])
        connection.sendall(body)
        # The command's own byte, or the start of the spawner's report that nothing started.
        first, ancillary, _, _ = connection.recvmsg(1, socket.CMSG_SPACE(struct.calcsize("3i")))
    except OSError:
        first = b""
    if not first:  # A command executes only once it reads this supervisor's answer, which was not sent.
        raise Fault("lifetime_unavailable", "The lifetime's spawner did not answer; nothing was launched.",
                    effect="known_no_effect")
    replies = connection.makefile("rb")
    vouched = None
    if first == b"\0":
        vouched = restricted.birth(sandbox.sender(ancillary) or 0)
        if vouched is None or sandbox.member(vouched.pid, instance["marker"]) is not True:
            raise Fault("lifetime_unverifiable", "The command is not in its lifetime's instance; nothing was launched.",
                        effect="known_no_effect")
        connection.sendall(b"\0")  # Only now does the command execute.
        first = b""
    child = restricted.started(restricted._frame(replies, first))  # A stopped or wedged spawner times out: effect unknown.
    if vouched is None or vouched.start != child.start:
        raise Fault("launch_unverified", "The launch acknowledgment names another process; the command may be running.",
                    effect="effect_unknown")
    connection.settimeout(None)  # The exit report comes whenever the command ends.
    return vouched.pid, vouched, instance["marker"], replies


def _end(instance: dict) -> None:
    """Kill an instance through its spawner, its first process, while that still is the recorded one."""
    if restricted._spawner_alive(instance):
        try:
            os.kill(instance["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def _remove(known: dict) -> None:
    """Remove the recorded placeholders that are still empty. Only while no instance lives."""
    for path in known["placeholders"]:
        try:
            os.rmdir(path)
        except OSError:
            pass
    known["placeholders"] = []


def _start_instance(bubblewrap: Path, scope: "rules.Scope | None", state: Path, home: Path, others: list[Path],
                    private: Path) -> tuple[dict, socket.socket]:
    """Start one instance, which serves once its release is sent a byte. Its placeholders, the empty
    directories under the protected names that do not exist, so that none can be created later, are created
    first and removed once no instance lives.

    Instances of other lifetimes mount over the same directories, and removing one from under a mount
    frees its name there. So one record for this user holds every live instance and every placeholder.
    """
    client = private_directory(restricted._stable_client().parent)
    roots = {}
    for index, root in enumerate([] if scope is None else scope.grants()):
        try:
            added = roots | {root: sandbox.git_entries(root)}
            sandbox.protection(added)
        except (sandbox.TooLarge, Fault) as refusal:
            if index >= len(scope.folders):
                continue  # A worktree that the folder rule refuses is not granted.
            if isinstance(refusal, Fault):
                raise
            raise rules.FolderFault("folder_too_large", index, str(root)) from None
        roots = added
    record = state / "code" / "sandbox.json"  # Beside the lifetimes, whose names hold no dot.
    with lease(record.with_suffix(".lock"), wait=True):
        try:
            known = json.loads(record.read_text())
            known = {"instances": [item for item in known["instances"] if restricted._spawner_alive(item)],
                     "placeholders": list(map(str, known["placeholders"]))}
        except (OSError, ValueError, KeyError, TypeError):
            known = {"instances": [], "placeholders": []}  # A missing or unreadable record removes nothing.
        if not known["instances"]:
            _remove(known)
        placeholders = [root / name for root in roots for name in sandbox.PROTECTED if not os.path.lexists(root / name)]
        known["placeholders"] = list(dict.fromkeys([*known["placeholders"], *map(str, placeholders)]))
        release = None
        try:
            save_json(record, known)  # Before any exists, so that none is left unrecorded.
            for path in placeholders:
                try:
                    path.mkdir()
                except FileExistsError:
                    pass  # Made meanwhile: held as what it is, or refused as a link.
                except OSError as error:  # The folder's owner can restore its mode, and so can restricted work.
                    raise Fault("scope_unsupported", f"{path} cannot be created to protect it. Make the folder "
                                "writable, or change it in the Console.", effect="known_no_effect") from error
            hidden = [*sandbox.read_denials(rules.account_home()), *rules.control_paths(state), state, *others]
            arguments = sandbox.arguments(home=home, roots=roots, hidden=hidden, private=private, client=client)
            instance, release = _start_spawner(
                [str(bubblewrap), *arguments, "--", sys.executable, "-I", "-c", SPAWNER], private)
            known["instances"].append(instance)
            save_json(record, known)
        except BaseException as error:
            if release:
                release.close()  # No instance runs that this record does not name.
            elif not known["instances"]:
                _remove(known)
                try:
                    save_json(record, known)
                except OSError:
                    pass  # The next start removes nothing that is not there.
            if isinstance(error, (OSError, sandbox.TooLarge)):  # The record, or a folder that changed meanwhile.
                raise Fault("lifetime_unavailable", "The lifetime's sandbox could not be started; nothing was "
                            "launched.", effect="known_no_effect") from error
            raise
    return instance, release


def _start_spawner(command: list[str], private: Path) -> tuple[dict, socket.socket]:
    """Start bubblewrap with the spawner as the instance's first process; fail closed unless its identity,
    its membership and the instance's mounts verify. The spawner serves once the returned socket is sent a
    byte and ends, with its instance, if the socket closes first."""
    try:
        machine = sandbox.mounts()
    except (OSError, IndexError) as error:
        raise Fault("lifetime_unverifiable", "This machine's mounts cannot be read; nothing was launched.",
                    effect="known_no_effect") from error
    release, ready = socket.socketpair()
    try:
        with ready:
            process = subprocess.Popen([*command, str(private), str(ready.fileno())], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={},
                                       pass_fds=[ready.fileno()], start_new_session=True)
    except OSError as error:
        release.close()
        raise Fault("lifetime_unverifiable", "The lifetime's sandbox did not start; nothing was launched.",
                    effect="known_no_effect") from error
    try:
        # Bounded: this runs under the lifetime lock that every admission needs.
        reported = (release.recv(64)  # The spawner's start time once it listens; nothing if it ended.
                    if select.select([release], [], [], restricted.ACKNOWLEDGE_SECONDS)[0] else b"")
        if not reported.isdigit():
            raise Fault("lifetime_unverifiable", "The lifetime's sandbox did not start; nothing was launched.",
                        effect="known_no_effect")
        try:
            # A PID inside the instance means nothing here. The kernel names the listener in this namespace.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(restricted.SPAWN_SECONDS)
                with restricted._Inside(private):
                    connection.connect("spawner")
                pid = sandbox.peer(connection)
            marker = sandbox.namespace(pid)
            moved = sandbox.foreign(sandbox.mounts(pid), command, machine, sandbox.mounts())
        except (OSError, ValueError, IndexError) as error:
            raise Fault("lifetime_unverifiable", "The lifetime's spawner is not reachable; nothing was launched.",
                        effect="known_no_effect") from error
        if moved:
            raise Fault("lifetime_unverifiable", f"The lifetime's sandbox mounted something else at {moved[0]}; "
                        "nothing was launched.", effect="known_no_effect")
        spawner = restricted._confirm_spawner(f"{pid} {int(reported)}".encode(), marker)
    except BaseException:
        release.close()
        os.killpg(process.pid, signal.SIGKILL)  # Still an unreaped child, so its PID is its own.
        process.wait()
        raise
    return {"marker": marker, "pid": spawner.pid, "start": spawner.start}, release


def _spawner() -> None:
    """The instance's first process: listen in the lifetime's directory, report this process's start time
    and serve launches once the supervisor has recorded the instance."""
    directory, ready = Path(sys.argv[1]), int(sys.argv[2])
    sandbox.scope()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with restricted._Inside(directory):
        try:
            Path("spawner").unlink(missing_ok=True)
        except OSError:
            shutil.rmtree("spawner")  # A directory, which the lifetime's earlier work left at the name.
        listener.bind("spawner")
    listener.listen(128)  # A burst of sessions waits here; a full queue refuses the next at once.
    os.write(ready, str(sandbox.start(os.getpid())).encode())
    if os.read(ready, 1):  # Nothing: the supervisor ended first, and with this process ends the instance.
        os.close(ready)
        restricted._serve(listener, vouch=True)
