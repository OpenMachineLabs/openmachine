"""Owned Linux systemd user units; native identity checks, never a second lifecycle owner."""

import ctypes
import os
from pathlib import Path
import platform
import re
import shlex
import signal
import stat
import time

from openmachine.foundation.sanitize import Fault
from . import processes


def supported() -> bool:
    # Check the required binaries and runtime ABI, not a distribution name.
    if platform.machine() != "x86_64":
        return False
    libc, version = platform.libc_ver()
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)*", version)
    return (libc == "glibc" and match is not None
            and (int(match[1]), int(match[2])) >= (2, 34)
            and all(os.path.isfile(path) and os.access(path, os.X_OK)
                    for path in ("/usr/bin/systemctl", "/usr/bin/loginctl")))


def unit_path(label: str) -> Path:
    # Bind to the native account's standard unit directory, not a caller's XDG override.
    return Path.home() / ".config/systemd/user" / (label + ".service")


def check_path(path: Path, *, allow_missing: bool = False) -> None:
    """No symlink components or writable foreign ancestors of the owned unit."""
    home = Path.home()
    for parent in (*reversed(path.parent.parents), path.parent):
        owned = parent.is_relative_to(home)
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if allow_missing and owned and parent != home:
                continue
            raise
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in ({os.getuid()} if owned else {0, os.getuid()})
                or info.st_mode & 0o022 and (owned or not info.st_mode & stat.S_ISVTX)):
            raise Fault("service_definition_mismatch", "The unit directory chain is not owned and safe; it was not changed.")


def prepare_unit_directory(path: Path) -> None:
    """Create only missing owned unit ancestors with fixed safe directory modes."""
    check_path(path, allow_missing=True)
    home = Path.home()
    for parent in (*reversed(path.parent.parents), path.parent):
        if parent == home or not parent.is_relative_to(home):
            continue
        # Path.mkdir(parents=True) uses 0777 for intermediate directories,
        # which can create group-writable systemd ancestors under umask 0002.
        parent.mkdir(mode=0o755, exist_ok=True)
        check_path(path, allow_missing=True)
    check_path(path)


def _quote(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Fault("invalid_service_path", "Native unit values cannot contain control characters.")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def definition_bytes(definition: dict) -> bytes:
    for path in (definition["WorkingDirectory"], definition["StandardErrorPath"]):
        _quote(path)
        if path != path.strip() or path.endswith("\\"):
            raise Fault("invalid_service_path", "Native service paths cannot end in whitespace or a backslash.")
    args = " ".join(_quote(arg) for arg in definition["ProgramArguments"])
    return ("[Unit]\nDescription=Open Machine owned runtime\nStartLimitIntervalSec=60\nStartLimitBurst=3\n\n"
            "[Service]\nType=exec\nExecStart=:" + args + "\nWorkingDirectory=" + definition["WorkingDirectory"].replace("%", "%%") +
            "\nEnvironment=" + _quote("PATH=" + definition["EnvironmentVariables"]["PATH"]) +
            "\nRestart=on-failure\nRestartSec=10\nKillMode=process\nTimeoutStopSec=15\nUMask=0077\n"
            "StandardOutput=null\nStandardError=append:" + definition["StandardErrorPath"].replace("%", "%%") +
            "\n\n[Install]\nWantedBy=default.target\n").encode()


def command(*args: str):
    # Never inherit a foreign bus address or remote systemctl destination.
    return processes.run(["/usr/bin/systemctl", "--user", *args], env={
        "HOME": str(Path.home()), "PATH": os.defpath, "LANG": "C", "LC_ALL": "C",
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus"})


def _show(path: Path) -> dict:
    keys = ("LoadState", "ActiveState", "SubState", "MainPID", "FragmentPath", "DropInPaths",
            "NeedDaemonReload", "ExecStart", "WorkingDirectory", "Environment", "KillMode",
            "Result", "ExecMainCode", "ExecMainStatus")
    result = command("show", path.name, "--property=" + ",".join(keys))
    if result.returncode and "LoadState=not-found" not in result.stdout.splitlines():
        raise Fault("user_service_unavailable", "The owning systemd user manager/bus is unavailable; no login or linger settings were changed.")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields.get("LoadState") == "not-found" and not fields.get("FragmentPath") and fields.get("MainPID", "0") == "0":
        return fields
    if not set(keys) <= fields.keys():
        raise Fault("service_command_unverified", "The user manager returned incomplete unit state.")
    return fields


def absent(path: Path) -> None:
    if _show(path)["LoadState"] != "not-found":
        raise Fault("service_identity_mismatch", "An existing native unit occupies this installation identity; it was not replaced.")


def native(state: Path, path: Path, definition: dict, record: dict) -> dict:
    fields = _show(path)
    if fields["LoadState"] == "not-found":
        return {"loaded": False, "pid": None, "registered": False}
    execution = re.fullmatch(r"\{ path=(.*?) ; argv\[\]=(.*?) ; ignore_errors=no ; .*\}", fields["ExecStart"])
    try:
        environment = shlex.split(fields["Environment"])
    except ValueError:
        environment = []
    if (fields["LoadState"] != "loaded" or fields["FragmentPath"] != str(path)
            or fields["DropInPaths"] or fields["NeedDaemonReload"] != "no"
            or not execution or execution[1] != definition["ProgramArguments"][0]
            or execution[2] != " ".join(definition["ProgramArguments"])
            or fields["WorkingDirectory"] != str(state)
            or environment != ["PATH=" + definition["EnvironmentVariables"]["PATH"]]
            or fields["KillMode"] != "process"):
        raise Fault("service_identity_mismatch", "The loaded unit differs from the owned definition; it was not controlled.")
    if not fields["MainPID"].isdigit():
        raise Fault("service_command_unverified", "The native process identity is unknown.")
    pid = int(fields["MainPID"]) or None
    if pid:
        process = Path("/proc") / str(pid)
        try:
            args = process.joinpath("cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            selections = {record["current"]}
            if record["pending"] is not None:
                selections.update((record["pending"]["from"], record["pending"]["to"]))
            supervisors = [[str(state / "service/versions" / identity / "bin/python"),
                            "-I", "-m", "openmachine.cli", "--config", record["config"],
                            "--state-dir", str(state), "serve", "--managed"] for identity in selections]
            # The stable launcher execs the selected interpreter in the same PID.
            if (process.stat().st_uid != os.getuid()
                    or args not in [[os.fsencode(arg) for arg in expected]
                                    for expected in (definition["ProgramArguments"], *supervisors)]):
                raise ValueError("foreign process")
        except (OSError, ValueError) as error:
            raise Fault("service_identity_mismatch", "The unit PID is not the owned launcher/supervisor; no signal was sent.") from error
    return {"loaded": fields["ActiveState"] not in {"inactive", "failed"}, "registered": True, "pid": pid,
            "process_state": fields["ActiveState"], "last_exit_code": fields["ExecMainStatus"],
            "exit_kind": fields["ExecMainCode"], "result": fields["Result"]}


def startup(path: Path) -> dict:
    result = command("is-enabled", path.name)
    value = result.stdout.strip()
    if value not in {"enabled", "disabled"} or result.returncode != (0 if value == "enabled" else 1):
        raise Fault("startup_unknown", "The unit's startup registration could not be verified.")
    try:
        linger = processes.run(["/usr/bin/loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"])
        continuity = linger.stdout.strip() if not linger.returncode else "unknown"
    except Fault:
        continuity = "unknown"
    if continuity not in {"yes", "no"}:
        continuity = "unknown"
    return {"enabled": value == "enabled", "source": "systemd", "linger": continuity,
            "logout_continuity": "supported" if continuity == "yes" else "unsupported" if continuity == "no" else "unknown",
            "boot_start": value == "enabled" and continuity == "yes", "observed_at_ms": time.time_ns() // 1000000}


def parent_death() -> None:
    """Only the MCP child dies with its supervisor; forked Execution workers do not inherit this."""
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if parent <= 1 or libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
        raise Fault("service_identity_mismatch", "The managed MCP child lost its supervisor before startup.")
