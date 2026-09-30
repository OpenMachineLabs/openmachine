"""Identity-checked target dispatch into the same local capability APIs."""

import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import subprocess
import sys
from types import SimpleNamespace

from openmachine import __version__, execution, workspace
from openmachine.foundation.sanitize import Fault

from . import MAX_MESSAGE, PROTOCOL
from .bundle import _archive_bytes


class _RemoteConfig:
    """One controller-selected target view for the existing dependency-free owners."""

    transport = "local"

    def __init__(self, identity: str, roots: dict, enabled: bool):
        if type(enabled) is not bool or not isinstance(roots, dict):
            raise Fault("invalid_request", "Invalid remote workspace configuration.")
        self.identity = identity
        self.execution_enabled = enabled
        self.workspaces = {}
        for name, root in roots.items():
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)
                    or not isinstance(root, str) or not root.startswith("/") or "\0" in root):
                raise Fault("invalid_request", "Remote workspaces require names and absolute target paths.")
            self.workspaces[name] = SimpleNamespace(machine=identity, root=root)

    def machine(self, name: str, *, write: bool = False):
        if name != self.identity:
            raise Fault("identity_mismatch", "The configured target identity does not match this account and host.")
        if write and not self.execution_enabled:
            raise Fault("execution_disabled", "Explicitly enable this target in controller configuration.")
        return self


def _identity() -> tuple[str, Path]:
    if sys.platform != "linux" or sys.version_info < (3, 12):
        raise Fault("unsupported_target", "The SSH worker requires Linux and Python 3.12 or newer.")
    machine_id = Path("/etc/machine-id").read_bytes().strip()
    if not re.fullmatch(rb"[0-9a-f]{32}", machine_id):
        raise Fault("identity_unavailable", "The target has no valid machine identity.")
    # The account database, not an SSH alias's HOME environment, owns shared state.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    identity = hashlib.sha256(machine_id + b"\0" + str(os.getuid()).encode() + b"\0" + os.fsencode(home)).hexdigest()
    return identity, home


def _scope_ready() -> str:
    try:
        result = subprocess.run(["systemctl", "--user", "--no-ask-password", "show", "--property=Version", "--value"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        version = result.stdout.strip()
        match = re.match(r"([0-9]+)", version)
        if result.returncode or match is None or int(match[1]) < 255:
            raise ValueError
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Fault("scope_unavailable", "A reachable systemd 255+ user manager is required; no privileges or session settings were changed.") from error
    return version


def _dispatch(request: dict, archive: Path) -> dict:
    if (not isinstance(request, dict) or set(request) != {"protocol", "version", "identity", "execution_enabled", "operation", "arguments", "workspaces"}
            or request["protocol"] != PROTOCOL or request["version"] != __version__):
        raise Fault("protocol_mismatch", "The controller and worker protocol/version must match.")
    identity, home = _identity()
    expected = request["identity"]
    if expected and expected != identity:
        raise Fault("identity_mismatch", "The pinned target identity changed; no operation was performed.")
    config = _RemoteConfig(identity, request["workspaces"], request["execution_enabled"])
    operation, arguments = request["operation"], request["arguments"]
    if not isinstance(arguments, dict):
        raise Fault("invalid_request", "Operation arguments must be an object.")
    state = home / ".local/state/openmachine"
    if operation == "probe":
        result = {"identity_verified": bool(expected), "python": platform.python_version(), "systemd": _scope_ready(),
                  "state_directory": str(state), "version": __version__,
                  "unavailable_workspaces": [name for name, item in config.workspaces.items() if not Path(item.root).is_dir()]}
    elif not expected:
        raise Fault("identity_required", "Probe and explicitly pin this identity before workspace access.")
    elif operation == "start_execution":
        _scope_ready()
        result = execution.start(config, state, identity, **arguments, remote_worker=archive)
    elif operation == "read_execution":
        result = execution.read(config, state, identity, **arguments)
    elif operation == "list_executions":
        result = execution.list_records(config, state, identity, **arguments)
    elif operation == "cancel_execution":
        result = execution.cancel(config, state, identity, **arguments)
    elif operation == "inspect_workspace":
        result = workspace.inspect(config, identity, **arguments)
    elif operation == "read_workspace_file":
        result = workspace.read_text_file(config, identity, **arguments)
    elif operation == "write_workspace_file":
        result = workspace.write_text_file(config, state, identity, **arguments)
    else:
        raise Fault("unsupported_operation", "Select a documented remote workspace or execution operation.")
    return {"protocol": PROTOCOL, "version": __version__, "identity": identity,
            "worker_sha256": archive.stem, "result": result}


def main() -> None:
    try:
        # Retain the requested filename: resolving a final link first changes the pin.
        archive = Path(sys.argv[0]).absolute()
        _archive_bytes(archive)
        if sys.argv[1:] and sys.argv[1] == "worker":
            from openmachine.execution.worker import worker
            worker(Path(sys.argv[2]), *(int(value) for value in sys.argv[3:]))
            return
        if sys.argv[1:] != ["rpc"]:
            raise Fault("invalid_request", "Use the bounded remote request entry point.")
        raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            raise Fault("request_limit", "Remote requests are bounded to one MiB.")
        result = _dispatch(json.loads(raw), archive)
        encoded = json.dumps(result, ensure_ascii=True).encode()
        if len(encoded) + 1 > MAX_MESSAGE:
            raise Fault("outcome_unknown", "The response exceeded its bound; read the original execution or destination before retrying.")
    except Fault as error:
        encoded = json.dumps({"error": {"code": error.code, "message": str(error)}}).encode()
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        encoded = b'{"error":{"code":"source_unavailable","message":"Remote source or request unavailable; no success was verified."}}'
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()
