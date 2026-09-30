"""Configured SSH transport and a standard-library endpoint, not another executor."""

import hashlib
from importlib.resources import files
import io
import json
import os
from pathlib import Path
import platform
import pwd
import re
import selectors
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
import zipfile

from openmachine import __version__, execution, workspace
from openmachine.foundation.sanitize import Fault

if TYPE_CHECKING:
    from openmachine.config import Settings

MAX_MESSAGE = 1024 * 1024
MAX_BUNDLE = 512 * 1024
PROTOCOL = 1
MUTATIONS = {"start_execution", "write_workspace_file", "cancel_execution"}


def _archive_bytes(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError
            data = stream.read(MAX_BUNDLE + 1)
    except OSError as error:
        raise Fault("worker_identity_mismatch", "Use an available regular worker archive, not a filesystem link.") from error
    if len(data) > MAX_BUNDLE or path.name != hashlib.sha256(data).hexdigest() + ".pyz":
        raise Fault("worker_identity_mismatch", "The worker bytes do not match the originally configured checksum filename.")
    return data


def build_bundle(directory: Path) -> dict:
    """Build from installed resources; no checkout, dependencies or private state."""
    output = io.BytesIO()
    members = {"__main__.py": b"from integrations.ssh import main\nmain()\n"}
    for package, names in {
        "openmachine": ("__init__.py", "execution.py", "workspace.py", "logs.py"),
        "openmachine.foundation": ("__init__.py", "state.py", "files.py", "sanitize.py"),
        "integrations": ("__init__.py", "ssh.py"),
    }.items():
        for name in names:
            members[f"{package.replace('.', '/')}/{name}"] = files(package).joinpath(name).read_bytes()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(members.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, content)
    data = output.getvalue()
    if len(data) > MAX_BUNDLE:
        raise Fault("worker_size_limit", "The standard-library worker exceeds its distribution bound.")
    digest = hashlib.sha256(data).hexdigest()
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}.pyz"
    fd, temporary = tempfile.mkstemp(prefix=".worker-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            try:
                existing = _archive_bytes(destination)
            except Fault as error:
                raise Fault("worker_conflict", "Preserve the existing artifact; it is not the expected regular worker.") from error
            if existing != data:
                raise Fault("worker_conflict", "Preserve the existing artifact; its content does not match its name.")
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.unlink(temporary)
    return {"version": __version__, "path": str(destination), "sha256": digest, "size_bytes": len(data)}


def _exchange(argv: list[str], payload: bytes) -> tuple[int, bytes]:
    """Bound stdin, both output streams and elapsed time without a shell or replay."""
    if len(payload) > MAX_MESSAGE:
        raise Fault("request_limit", "Narrow the request to at most one MiB of encoded JSON.")
    output, diagnostic = bytearray(), bytearray()
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0) as process:
        try:
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdin, process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, None)
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, diagnostic)
                offset = 0
                deadline = time.monotonic() + 30
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    for key, _ in selector.select(0.05):
                        if key.fileobj is process.stdin:
                            offset += os.write(key.fd, payload[offset:offset + 65536])
                            if offset == len(payload):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                        else:
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            key.data.extend(chunk)
                            if len(key.data) > (MAX_MESSAGE if key.data is output else 8192):
                                raise Fault("response_limit", "SSH output exceeded its bound; no complete response was verified.")
                return process.wait(timeout=max(0.1, deadline - time.monotonic())), bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def call(config: "Settings", state: Path | None, machine: str, operation: str, **arguments) -> dict:
    target = config.machine(machine, write=operation in {"start_execution", "write_workspace_file"})
    if target.transport != "ssh":
        raise Fault("not_ssh", "Select a configured SSH target, or omit the explicit doctor target.")
    if not target.ssh_identity and operation != "probe":
        raise Fault("identity_required", "Probe this target and explicitly pin its ssh_identity before workspace access.")
    if operation == "start_execution" and arguments.get("task_id"):
        # Original-ID reconciliation must still work if the controller checkpoint has disappeared.
        try:
            call(config, state, machine, "read_execution", execution_id=arguments["execution_id"])
        except Fault as error:
            if error.code != "execution_not_found":
                raise
            from openmachine.tasks import read
            if state is None:
                raise Fault("task_unavailable", "The controller task store is unavailable.")
            read(state, arguments["task_id"])
    request = {"protocol": PROTOCOL, "version": __version__, "identity": target.ssh_identity,
               "execution_enabled": target.execution_enabled, "operation": operation, "arguments": arguments,
               "workspaces": {name: item.root for name, item in config.workspaces.items() if item.machine == machine}}
    argv = ["ssh", "-T", "-a", "-x", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "LogLevel=ERROR", "--",
            target.ssh_alias, shlex.join(["python3", "-I", target.ssh_worker, "rpc"])]
    mutating = operation in MUTATIONS
    try:
        code, raw = _exchange(argv, json.dumps(request, ensure_ascii=True).encode())
    except FileNotFoundError as error:
        raise Fault("ssh_missing", "Install the system OpenSSH client; no target was contacted.") from error
    except (OSError, TimeoutError, subprocess.SubprocessError, Fault) as error:
        if isinstance(error, Fault) and (error.code == "request_limit" or not mutating):
            raise
        raise Fault("outcome_unknown" if mutating else "ssh_unavailable",
                    "No complete SSH response was verified. Read the original execution or destination before any mutation retry.") from error
    try:
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise ValueError
    except (ValueError, UnicodeError) as error:
        reason = "ssh_unavailable" if code == 255 else "remote_runtime_unavailable" if code == 2 else "ssh_protocol_error"
        raise Fault("outcome_unknown" if mutating else reason,
                    "Check the existing SSH identity, Python 3.12+, and the configured worker archive. No result was verified.") from error
    if "error" in response:
        error = response["error"]
        if isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
            raise Fault(error["code"], error["message"])
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The remote error response was invalid.")
    if (code or response.get("protocol") != PROTOCOL or response.get("version") != __version__
            or response.get("worker_sha256") != Path(target.ssh_worker).stem
            or not isinstance(response.get("identity"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", response["identity"])
            or (target.ssh_identity and response["identity"] != target.ssh_identity)
            or not isinstance(response.get("result"), dict)):
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The remote response identity was not verified.")
    result = response["result"]
    if "machine" in result:
        result["machine"] = machine
    items = result.get("executions", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The execution list response was invalid.")
    for item in items:
        item["machine"] = machine
    return result | {"target_identity": response["identity"], "worker_sha256": response["worker_sha256"]}


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
            execution.worker(Path(sys.argv[2]), *(int(value) for value in sys.argv[3:]))
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
