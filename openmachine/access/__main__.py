"""python -I -m openmachine.access: the one Code launch boundary for the Go host."""

import argparse
import json
import os
from pathlib import Path
import signal
import sys
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from openmachine import __version__
from openmachine.access import catalog, rules, sessions
from openmachine.access.macos import seatbelt
from openmachine.access.macos import sessions as restricted
from openmachine.config import DEFAULT_CONFIG, DEFAULT_STATE, load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory

# Precise outcomes the owner can act on; a changed or refused binding is target_unverified, the rest unavailable.
PASSED = {"desktop_missing", "desktop_redirected", "desktop_access_denied", "desktop_unsupported",
          "directory_access_denied"}
CHANGED = {"project_changed", "project_unavailable"}
FORWARDED = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2,
             signal.SIGALRM, signal.SIGABRT)


class Arguments(BaseModel):
    """A root, list or inspect request; an omitted directory asks for the Desktop root."""
    model_config = ConfigDict(extra="forbid", strict=True)
    machine: str | None = None
    workspace_name: str = ""
    action: Literal["root", "list", "inspect"] = "list"
    path: str = "."
    offset: int = 0
    limit: int = catalog.MAX_PAGE
    expected_snapshot: str = ""
    expected_target_id: str = ""


class Validation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["validate"]
    projects: Annotated[list[rules.ScopeRoot], Field(max_length=rules.MAX_ROOTS)]


def catalog_reply(config_path: Path, state: Path, raw: bytes) -> dict:
    """One catalog request in, one protocol 2 result or {"error": {"code": C}} out."""
    try:
        if len(raw) > catalog.MAX_RESULT:
            raise Fault("catalog_limit", "The request exceeds 64 KiB.")
        value = json.loads(raw)
        config = load(config_path)
        configured = next(iter(config.machines))
        if isinstance(value, dict) and value.get("action") == "validate":
            projects = Validation.model_validate(value).projects
            roots = rules.bind(config, configured, projects, rules.control_paths(state))
            if any(item.relative_path != "." and root.kind != "folder" for item, root in zip(projects, roots)):
                raise Fault("scope_repository_below_root", "Select a repository or worktree by its own directory.")
            result = {"protocol": 2, "validated": value["projects"]}
        else:
            arguments = Arguments.model_validate(value)
            action = "root" if arguments.action == "list" and not arguments.workspace_name else arguments.action
            machine = arguments.machine
            if machine is None and action == "root":
                machine = configured
            result = catalog.inspect(config, machine, arguments.workspace_name, action, arguments.path,
                                     arguments.offset, arguments.limit, arguments.expected_snapshot,
                                     arguments.expected_target_id)
        if len(json.dumps(result).encode()) >= catalog.MAX_RESULT:  # print adds a newline
            raise Fault("catalog_limit", "The result exceeds 64 KiB; use a smaller page.")
        return result
    except Fault as error:
        code = error.code
        return {"error": {"code": code if code in PASSED else "target_unverified"
                          if code in CHANGED or code.startswith("scope_") else "catalog_unavailable"}}
    except (ValueError, RecursionError, OSError, KeyError):
        # KeyError: the account has no passwd record.
        return {"error": {"code": "catalog_unavailable"}}


def capabilities() -> dict:
    reason = seatbelt.lifetime_unavailable_reason()
    confined = "available" if reason is None else reason
    # github names the routing form Core takes, so a host and Core that disagree on it route nothing.
    value = {"version": __version__, "protocol": sessions.PROTOCOL, "kinds": ["shell", "exec", "sftp"], "github": "https",
             "policies": {"full_access": "available", "read_only": confined, "selected_projects": confined}}
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


def session(control: int, config_path: Path, state: Path) -> None:
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
            launch = restricted._admit_restricted(request, account, config_path, state)
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
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="Print the version, supported protocol, kinds and policies")
    commands.add_parser("catalog", help="Answer one folder catalog request: a JSON object on stdin and on stdout")
    channel = commands.add_parser("session", help="Admit one channel: request and reply on --control-fd, IO on 0-2")
    channel.add_argument("--control-fd", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "capabilities":
        print(json.dumps(capabilities(), sort_keys=True))
        return 0
    if args.command == "catalog":
        print(json.dumps(catalog_reply(args.config, args.state_dir, sys.stdin.buffer.read(catalog.MAX_RESULT + 1))))
        return 0
    os.set_inheritable(args.control_fd, False)
    session(args.control_fd, args.config, args.state_dir)
    return 1  # session() replaces or exits this process.


if __name__ == "__main__":
    sys.exit(main())
