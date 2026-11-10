"""python -I -m openmachine.access: the one Code launch boundary for the Go host."""

import argparse
import json
import os
from pathlib import Path
import signal
import sys

from pydantic import BaseModel, ConfigDict, ValidationError

from openmachine import __version__
from openmachine.access import rules, sessions
from openmachine.access.macos import seatbelt
from openmachine.access.macos import sessions as restricted
from openmachine.config import DEFAULT_STATE
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory

FORWARDED = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2,
             signal.SIGALRM, signal.SIGABRT)


class FolderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    folders: rules.Folders


def folders_reply(state: Path, raw: bytes) -> dict:
    """One folders request in; the canonical folders in order, or {"error": {"code": C, "index": i}}, out."""
    try:
        request = FolderRequest.model_validate_json(raw)
    except ValidationError:
        return {"error": {"code": "invalid_request"}}
    try:
        found = rules.folders(request.folders, rules.control_paths(state), admission=False)
    except rules.FolderFault as error:
        return {"error": {"code": error.code, "index": error.index}}
    return {"folders": [str(path) for path in found]}


def capabilities() -> dict:
    reason = seatbelt.lifetime_unavailable_reason()
    confined = "available" if reason is None else reason
    # github names the routing form Core takes, so a host and Core that disagree on it route nothing.
    value = {"version": __version__, "protocol": sessions.PROTOCOL, "kinds": ["shell", "exec", "sftp"], "github": "https",
             "policies": {"full_access": "available", "read_only": confined, "workspace": confined}}
    if reason is None:
        value["github_client"] = str(restricted._stable_client())  # what restricted sessions' Git routing runs
    return value


def _reply(control: int, value: dict) -> None:
    data = json.dumps(value, sort_keys=True).encode() + b"\n"
    while data:
        data = data[os.write(control, data):]


def _read_line(fd: int) -> bytes:
    data = bytearray()
    while not data.endswith(b"\n") and len(data) <= sessions.MAX_REQUEST:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def session(control: int, state: Path) -> None:
    """Admit one channel. fds 0-2 are its IO (a PTY slave when terminal is set); control carries JSON lines.

    Exactly one reply follows the request: denied, or admitted once the command is running.
    """
    try:
        request = sessions.parse(_read_line(control))
        account = sessions._account(request.user)
        state = private_directory(state)
        if request.policy == "full_access":
            launch = sessions._admit_full(request, account, state)
        else:
            launch = restricted._admit_restricted(request, account, state)
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
            signal.signal(signum, lambda number, _: seatbelt.signal_group(child, marker, number))
    _reply(control, {"status": "admitted", "policy": request.policy, "pid": launch[0]})
    os.close(control)
    if request.policy == "full_access":
        _, program, argv, environment = launch
        try:
            os.execve(program, argv, environment)
        finally:
            os._exit(127)
    restricted._relay(launch[3])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -I -m openmachine.access")
    parser.add_argument("--config", type=Path)  # om-code passes it; access reads no configuration.
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="Print the version, supported protocol, kinds and policies")
    commands.add_parser("folders", help="Check Workspace folders: a JSON object on stdin and on stdout")
    channel = commands.add_parser("session", help="Admit one channel: request and reply on --control-fd, IO on 0-2")
    channel.add_argument("--control-fd", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        print(json.dumps(capabilities(), sort_keys=True))
        return 0
    if args.command == "folders":
        print(json.dumps(folders_reply(args.state_dir, sys.stdin.buffer.read())))
        return 0
    os.set_inheritable(args.control_fd, False)
    session(args.control_fd, args.state_dir)
    return 1  # session() replaces or exits this process.


if __name__ == "__main__":
    sys.exit(main())
