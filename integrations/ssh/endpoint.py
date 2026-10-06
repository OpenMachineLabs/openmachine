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

    def __init__(self, identity: str, roots: dict, enabled: bool, workspace_write_enabled: bool):
        if type(enabled) is not bool or type(workspace_write_enabled) is not bool or not isinstance(roots, dict):
            raise Fault("invalid_request", "Invalid remote workspace configuration.")
        self.identity = identity
        self.execution_enabled = enabled
        self.workspace_write_enabled = workspace_write_enabled
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


    def workspace_machine(self, name: str):
        self.machine(name)
        if not self.workspace_write_enabled:
            raise Fault("workspace_write_disabled", "Typed target workspace writes are disabled.")
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
    if (not isinstance(request, dict) or set(request) != {"protocol", "version", "identity", "execution_enabled", "workspace_write_enabled", "operation", "arguments", "workspaces"}
            or request["protocol"] != PROTOCOL or request["version"] != __version__):
        raise Fault("protocol_mismatch", "The controller and worker protocol/version must match.")
    identity, home = _identity()
    expected = request["identity"]
    if expected and expected != identity:
        raise Fault("identity_mismatch", "The pinned target identity changed; no operation was performed.")
    config = _RemoteConfig(identity, request["workspaces"], request["execution_enabled"], request["workspace_write_enabled"])
    operation, arguments = request["operation"], request["arguments"]
    if not isinstance(arguments, dict):
        raise Fault("invalid_request", "Operation arguments must be an object.")
    state = home / ".local/state/openmachine"
    if operation == "probe":
        execution.check_workspace_leases(config, state)
        change_state = workspace.check_change_state(state)
        legacy_compatible = True
        try:
            execution.check_workspace_leases(config, state, legacy_candidate=True)
        except Fault as error:
            if error.code != "legacy_workspace_busy":
                raise
            legacy_compatible = False
        result = {**change_state, "legacy_workspace_compatible": legacy_compatible, "identity_verified": bool(expected), "python": platform.python_version(), "systemd": _scope_ready() if config.execution_enabled else None,
                  "state_directory": str(state), "version": __version__,
                  "os": platform.system(), "architecture": platform.machine(),
                  "execution": {"receipt_format": execution.FORMAT, "io_modes": ["pipe", "pty"],
                                "controls": sorted(execution.CONTROL_ACTIONS), "control_protocol": execution.CONTROL_PROTOCOL},
                  "unavailable_workspaces": [name for name, item in config.workspaces.items() if not Path(item.root).is_dir()]}
    elif not expected:
        raise Fault("identity_required", "Probe and explicitly pin this identity before workspace access.")
    elif operation == "start_execution":
        if set(arguments) - {"execution_id", "cwd", "argv", "timeout_seconds", "expected_head_sha",
                             "workspace_access", "resource_keys", "task_id", "termination_grace_seconds", "tty", "terminal_size"}:
            raise Fault("invalid_request", "Use only the documented execution-start fields.")
        _scope_ready()
        result = execution.start(config, state, identity, **arguments, remote_worker=archive)
    elif operation == "read_execution":
        if set(arguments) - {"execution_id", "offset", "max_bytes", "wait_seconds"}:
            raise Fault("invalid_request", "Use only the documented execution-read fields.")
        result = execution.read(config, state, identity, **arguments)
    elif operation == "list_executions":
        result = execution.list_records(config, state, identity, **arguments)
    elif operation == "cancel_execution":
        result = execution.cancel(config, state, identity, **arguments)
    elif operation == "control_execution":
        if set(arguments) - {"execution_id", "expected_control_seq", "action", "text", "terminal_size"}:
            raise Fault("invalid_request", "Use only the documented execution-control fields.")
        result = execution.control_execution(config, state, identity, **arguments)
    elif operation == "inspect_workspace":
        if set(arguments) - {"workspace", "action", "path", "offset", "limit", "query", "staged", "expected_snapshot", "path_glob"}:
            raise Fault("invalid_request", "Use only the documented inspection fields.")
        result = workspace.inspect(config, identity, **arguments)
    elif operation == "read_workspace_file":
        if set(arguments) - {"workspace", "path", "offset", "expected_sha256", "start_line", "line_count"}:
            raise Fault("invalid_request", "Use only the documented source-read fields.")
        result = workspace.read_text_file(config, identity, **arguments)
    elif operation == "write_workspace_file":
        if set(arguments) - {"workspace", "path", "content", "expected_head_sha", "expected_sha256", "change_id", "expected_workspace_identity"}:
            raise Fault("invalid_request", "Use only the documented whole-file fields.")
        result = workspace.write_text_file(config, state, identity, **arguments,
                                          validate_new=lambda root: execution.check_workspace_leases(config, state, scope=root))
    elif operation == "edit_workspace_file":
        if set(arguments) - {"workspace", "path", "edits", "expected_workspace_identity", "expected_head_sha", "expected_sha256", "change_id", "dry_run", "expected_plan_sha256"}:
            raise Fault("invalid_request", "Use only the documented exact-edit fields.")
        result = workspace.edit_text_file(config, state, identity, **arguments,
                                         validate_new=lambda root: execution.check_workspace_leases(config, state, scope=root))
    elif operation == "apply_workspace_patch":
        if set(arguments) - {"workspace", "patch_text", "expected_workspace_identity", "expected_head_sha", "preconditions",
                             "create_parents", "change_id", "dry_run", "expected_plan_sha256"}:
            raise Fault("invalid_request", "Use only the documented coordinated-patch fields.")
        result = workspace.apply_patch(config, state, identity, **arguments,
                                       validate_new=lambda root: execution.check_workspace_leases(config, state, scope=root))
    elif operation == "read_workspace_change":
        if set(arguments) - {"change_id", "observe_current"}:
            raise Fault("invalid_request", "Use only the documented change-read fields.")
        result = workspace.read_change(config, state, identity, **arguments)
    else:
        raise Fault("unsupported_operation", "Select a documented remote workspace or execution operation.")
    response = {"protocol": PROTOCOL, "version": __version__, "identity": identity,
                "worker_sha256": archive.stem, "result": result}
    if operation in {"start_execution", "read_execution", "cancel_execution"}:
        response["result"] = execution.bound_output(
            result, lambda value: len(json.dumps(response | {"result": value}, ensure_ascii=True).encode()) + 1,
            MAX_MESSAGE,
        )
    if operation in {"inspect_workspace", "read_workspace_file", "write_workspace_file", "edit_workspace_file", "apply_workspace_patch", "read_workspace_change"}:
        response["result"] = workspace.bound_result(result,
            lambda value: len(json.dumps(response | {"result": value}, ensure_ascii=True).encode()) + 1)
    return response


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
        encoded = json.dumps({**(error.evidence or {}), "error": {"code": error.code, "message": str(error)}}).encode()
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        encoded = b'{"error":{"code":"source_unavailable","message":"Remote source or request unavailable; no success was verified."}}'
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()
