"""Target-local observations; no project execution, state or controller Task access."""

import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import struct
import sys
import time

from openmachine import workspace
from openmachine.foundation.files import excluded
from openmachine.foundation.sanitize import Fault

from . import resources

OBSERVATION_VERSION = 1
TARGET_SECTIONS = ("environment", "project", "resources", "services")
MAX_SELECTORS = 20


def validate_observation_inputs(executables, ports, limit: int) -> tuple[list[str], list[int]]:
    executables = [] if executables is None else executables
    ports = [] if ports is None else ports
    if (type(limit) is not int or not 1 <= limit <= 50
            or type(executables) is not list or type(ports) is not list
            or len(executables) > min(limit, MAX_SELECTORS) or len(ports) > min(limit, MAX_SELECTORS)):
        raise Fault("invalid_context_request", "Use at most min(limit, 20) executable or TCP port selectors.")
    for name in executables:
        if (type(name) is not str or not 1 <= len(name) <= 512 or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or ".." in Path(name).parts or excluded(Path(name.casefold()))
                or "/" not in name and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}", name)):
            raise Fault("invalid_context_request", "Use executable names or non-credential paths without traversal.")
    if (any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
            or len(set(executables)) != len(executables) or len(set(ports)) != len(ports)):
        raise Fault("invalid_context_request", "Selectors must be distinct; TCP ports are strict integers from 1 to 65535.")
    return executables, ports


def _runtime(cwd: str, executables: list[str]) -> dict:
    uname = os.uname()
    path = os.environ.get("PATH", os.defpath)
    if len(path.encode()) > 16384 or len(path.split(os.pathsep)) > 128:
        raise Fault("observation_limit", "The observer PATH exceeds the bounded resolution scope.")
    # Relative/empty PATH entries refer to the selected cwd, never the server cwd.
    search = os.pathsep.join(os.path.abspath(os.path.join(cwd, part)) for part in path.split(os.pathsep))
    resolved = []
    for name in executables:
        command = os.path.join(cwd, name) if "/" in name else name
        found = shutil.which(command, path=search)
        found = os.path.abspath(found) if found else None
        resolved.append({"requested": name, "path": found,
                         "resolved_path": os.path.realpath(found) if found else None,
                         "status": "resolved" if found else "not_resolved", "executed": False})
    try:
        login_shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        login_shell = None
    return {"complete": bool(login_shell), "os": uname.sysname, "kernel": uname.release, "machine": uname.machine,
            "pointer_bits": struct.calcsize("P") * 8,
            "interpreter": {"path": sys.executable, "version": platform.python_version(),
                            "implementation": platform.python_implementation()},
            "login_shell": {"path": login_shell, "source": "account_database", "status": "observed" if login_shell else "unavailable", "executed": False},
            "command_mode": "argv_without_implicit_shell", "executables": resolved,
            "resolution_scope": "observer_process_PATH_with_selected_cwd",
            "limitations": ["Observer environment, not a measurement of a future command or activated project environment.",
                            "Path resolution does not execute wrappers or establish tool versions."]}


def inspect_environment(config, machine: str, workspace_name: str, section: str, worktree: str = "",
                        executables=None, ports=None, limit: int = 10, *, scope: str = "controller") -> dict:
    executables, ports = validate_observation_inputs(executables, ports, limit)
    if (section not in TARGET_SECTIONS or scope not in {"controller", "worker_endpoint"}
            or executables and section != "environment" or ports and section != "services"):
        raise Fault("invalid_context_request", "Use selectors only with their matching observation section.")
    selected = workspace.select_worktree(config, machine, workspace_name, worktree)
    before = workspace.read_text_file(selected, machine, workspace_name, "AGENTS.md", line_count=1)["context"]
    started = time.time_ns() // 1000000
    if section == "environment":
        data = resources.sample("uname/interpreter/account/PATH", scope, "named_fields",
                                lambda: _runtime(before["cwd"], executables))
        complete = data["complete"]
    elif section == "project":
        data = workspace.read_signals(selected, machine, workspace_name, limit)
        complete = data["complete"]
    elif section == "resources":
        data = resources.collect(Path(before["cwd"]), scope, limit)
        complete = all(item["complete"] for item in data.values())
    else:
        data = resources.services(ports)
        complete = bool(ports) and all(item["complete"] for item in data.values())
    after = workspace.read_text_file(selected, machine, workspace_name, "AGENTS.md", line_count=1)["context"]
    if any(before.get(key) != after.get(key) for key in ("cwd", "worktree", "workspace_identity", "head_sha", "branch")):
        raise Fault("source_changed", "The selected target changed during collection; inspect it again.")
    result = {"observation_version": OBSERVATION_VERSION, "section": section, "scope": scope,
            "context": after, "started_at_ms": started, "finished_at_ms": time.time_ns() // 1000000,
            "complete": complete, "data": data,
            "status": ("not_selected" if section == "services" and not ports else
                       "unavailable" if section == "environment" and data["status"] != "observed" else "observed")}
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode()) > 65536:
        raise Fault("context_response_limit", "Target facts exceed 64 KiB; reduce the requested limit.")
    return result
