"""Explicit macOS user-service installation, activation and compatible rollback."""

import asyncio
from email.parser import BytesParser
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

from openmachine import __version__, execution, tasks
from openmachine.config import load
from openmachine.sanitize import Fault

PROTOCOL = 1
# Bump for a change to configuration, task or execution formats/writer semantics.
# Managed transitions deliberately do not perform persistent-state migrations.
STATE_CONTRACT = 1
LAUNCHER = '''"""Stable stdlib-only launcher; exec the selected immutable environment."""
import json
import os
from pathlib import Path
import re

root = Path(__file__).resolve().parent
record = json.loads((root / "service.json").read_text())
identity = record["current"]
if record["format"] != 1 or not re.fullmatch(r"[0-9a-f]{64}", identity):
    raise SystemExit("Invalid Open Machine service selection; inspect service status.")
python = str(root / "versions" / identity / "bin" / "python")
os.execv(python, [python, "-I", "-m", "openmachine.cli", "--config", record["config"],
                 "--state-dir", str(root.parent), "serve", "--managed"])
'''


def _json(path: Path) -> dict:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 65536):
            raise ValueError("unsafe metadata")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("invalid metadata")
        return value
    except (OSError, ValueError, UnicodeError) as error:
        raise Fault("service_state_invalid", "Service metadata is missing, unsafe or corrupt; preserve it for recovery.") from error


def _record(state: Path) -> dict:
    record = _json(state / "service/service.json")
    try:
        if record["format"] != 1 or not Path(record["config"]).is_absolute():
            raise ValueError("unsupported selection")
        for key in ("current", "previous"):
            if not (key == "previous" and record[key] is None) and not re.fullmatch(r"[0-9a-f]{64}", record[key]):
                raise ValueError("invalid installation identity")
        probe = record["probe"]
        if set(probe) != {"workspace", "path"} or not all(isinstance(v, str) and v for v in probe.values()):
            raise ValueError("invalid readiness probe")
        if not Path(record["launch_python"]).is_absolute() or not isinstance(record["path_environment"], str):
            raise ValueError("invalid launcher")
        pending = record["pending"]
        if pending is not None:
            if set(pending) != {"from", "to", "previous"}:
                raise ValueError("invalid transition")
            for value in pending.values():
                if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ValueError("invalid transition identity")
    except (KeyError, TypeError, ValueError) as error:
        raise Fault("service_state_invalid", "Service selection is invalid; do not replace it or guess a version.") from error
    return record


def _runtime(state: Path, identity: str) -> Path:
    path = state / "service/versions" / identity
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise Fault("installation_unavailable", "The original immutable installation is unavailable; restore it, not its receipts.")
    return path


def _installed(state: Path, identity: str) -> dict:
    result = _json(_runtime(state, identity) / "installation.json")
    if (result.get("identity") != identity or result.get("protocol") != PROTOCOL
            or result.get("state_contract") != STATE_CONTRACT or not isinstance(result.get("version"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+!-]{0,99}", result["version"])):
        raise Fault("incompatible_runtime", "This installation has no compatible lifecycle/state contract; no downgrade was attempted.")
    return result


def guard_writes(state: Path) -> None:
    """Called under the shared activation lock, not an OS sandbox."""
    path = state / "service/service.json"
    if not path.exists() and not path.is_symlink():
        return
    record = _record(state)
    if record["pending"] is not None:
        raise Fault("activation_pending", "A service transition needs readback or rollback; new writes are paused.")
    if Path(sys.prefix).resolve() != _runtime(state, record["current"]):
        raise Fault("service_identity_mismatch", "This is not the selected managed runtime; reconnect before another write.")


def check_state(config_path: Path, state: Path) -> dict:
    """Use the candidate's actual readers; never rewrite existing persistent data."""
    config = load(config_path)
    count = 0
    root = state / "executions"
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise Fault("corrupt_state", "The execution store is not an ordinary directory.")
        for directory in root.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", directory.name) or directory.is_symlink() or not directory.is_dir():
                raise Fault("corrupt_state", "An execution-store entry is invalid; preserve it before changing versions.")
            execution._load(directory)
            count += 1
    page = tasks.find(state, limit=50)
    task_count = 0
    while True:
        for item in page["tasks"]:
            tasks.read(state, item["task_id"])
            task_count += 1
        if page["next_offset"] is None:
            break
        page = tasks.find(state, offset=page["next_offset"], limit=50, expected_generation=page["generation"])
    return {"version": __version__, "protocol": PROTOCOL, "state_contract": STATE_CONTRACT,
            "prefix": str(Path(sys.prefix).resolve()), "execution_records": count, "task_records": task_count,
            "configuration_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest()}


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, cwd="/", check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Fault("service_command_unverified", "The bounded service command failed or timed out; inspect status before retrying.") from error


def _candidate_state(python: Path, config_path: Path, state: Path) -> dict:
    result = _run([str(python), "-I", "-m", "openmachine.service", "check-state", str(config_path), str(state)])
    try:
        value = json.loads(result.stdout)
        if result.returncode or value["protocol"] != PROTOCOL or value["state_contract"] != STATE_CONTRACT:
            raise ValueError("incompatible candidate")
        return value
    except (ValueError, KeyError, TypeError) as error:
        raise Fault("incompatible_state", "The candidate cannot validate current configuration and records; no state was migrated.") from error


def _prepare(state: Path, wheel: Path, requirements: Path, config_path: Path) -> str:
    """Build a venv at its final path: Python environments must not be moved."""
    try:
        wheel = wheel.resolve(strict=True)
        requirements = requirements.resolve(strict=True)
        if wheel.suffix != ".whl" or not wheel.is_file() or not requirements.is_file():
            raise ValueError("expected local artifacts")
        with wheel.open("rb") as source:
            wheel_hash = hashlib.file_digest(source, "sha256").hexdigest()
        locked = requirements.read_bytes()
        lines = [line.strip() for line in locked.decode().splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not lines or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+!-]*", line) for line in lines):
            raise ValueError("requirements must pin every dependency")
        with zipfile.ZipFile(wheel) as archive:
            metadata = [name for name in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", name)]
            if len(metadata) != 1:
                raise ValueError("expected one distribution")
            package = BytesParser().parsebytes(archive.read(metadata[0]))
            if package["Name"] != "openmachine" or not package["Version"]:
                raise ValueError("wrong distribution")
            version = package["Version"]
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile) as error:
        raise Fault("invalid_installation_artifact", "Select a trusted local Open Machine wheel and fully pinned requirements file.") from error
    digest = hashlib.sha256((wheel_hash + hashlib.sha256(locked).hexdigest() + sys.version).encode()).hexdigest()
    versions = execution.private_directory(state / "service/versions")
    destination = versions / digest
    if destination.exists():
        original = _installed(state, digest)
        if original.get("wheel_sha256") != wheel_hash or original.get("version") != version:
            raise Fault("installation_conflict", "The retained installation does not match the selected artifact.")
        return digest
    destination.mkdir(mode=0o700)
    artifacts = execution.private_directory(destination / "artifacts")
    with wheel.open("rb") as source, (artifacts / wheel.name).open("xb") as output:
        shutil.copyfileobj(source, output)
    wheel = artifacts / wheel.name
    with wheel.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != wheel_hash:
            raise Fault("installation_artifact_changed", "The wheel changed during preparation; the active installation is untouched.")
    requirements = artifacts / "requirements.lock"
    requirements.write_bytes(locked)
    # Keep failed candidates for diagnosis. Never overwrite or garbage-collect a runtime.
    for argv in ([sys.executable, "-I", "-m", "venv", str(destination)],
                 [str(destination / "bin/python"), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                  "--no-input", "--no-deps", "-r", str(requirements)],
                 [str(destination / "bin/python"), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                  "--no-input", "--no-deps", str(wheel)],
                 [str(destination / "bin/python"), "-I", "-m", "pip", "check"]):
        if _run(argv, timeout=180).returncode:
            raise Fault("installation_failed", "Candidate installation failed; the active runtime is untouched and the candidate is retained.")
    observed = _candidate_state(destination / "bin/python", config_path, state)
    if observed["version"] != version or observed["prefix"] != str(destination):
        raise Fault("installation_identity_mismatch", "Wheel metadata, installed runtime and environment identity disagree.")
    execution._save(destination / "installation.json", {
        "identity": digest, "version": version, "protocol": PROTOCOL, "state_contract": STATE_CONTRACT,
        "wheel_sha256": wheel_hash, "requirements_sha256": hashlib.sha256(locked).hexdigest(),
    })
    return digest


async def _probe(socket_path: Path, config_path: Path, probe: dict, expected_version: str,
                 expected_pid: int | None = None) -> dict:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    config = load(config_path)
    selected = config.workspaces.get(probe["workspace"])
    if selected is None or config.machine(selected.machine).transport != "local":
        raise Fault("invalid_service_probe", "Select one configured local workspace and a permitted existing text file.")
    try:
        async with asyncio.timeout(8):
            async with httpx.AsyncClient(trust_env=False, timeout=5,
                    transport=httpx.AsyncHTTPTransport(uds=str(socket_path))) as client:
                async with streamable_http_client("http://localhost/mcp", http_client=client) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        initialized = await session.initialize()
                        capabilities = await session.call_tool("get_capabilities", {})
                        caps = capabilities.structuredContent
                        fingerprint = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
                        if (capabilities.isError or initialized.serverInfo.version != expected_version
                                or caps["version"] != expected_version or caps["configuration_sha256"] != fingerprint
                                or caps["lifecycle"]["protocol"] != PROTOCOL or caps["lifecycle"]["state_contract"] != STATE_CONTRACT
                                or (expected_pid is not None and caps["lifecycle"]["pid"] != expected_pid)):
                            raise Fault("service_identity_mismatch", "The live service does not match the selected version, configuration or process.")
                        result = await session.call_tool("read_workspace_file", {
                            "machine": selected.machine, "workspace_name": probe["workspace"], "path": probe["path"]})
                        read = result.structuredContent
                        if result.isError or not read["exists"] or read.get("content_redacted"):
                            raise Fault("service_read_unavailable", "The service could not read the selected permitted file; check the path and OS file permissions.")
                        return {"status": "ready", "version": expected_version, "configuration_sha256": fingerprint,
                                "pid": caps["lifecycle"]["pid"], "file_sha256": read["sha256"],
                                "tunnel": "not_probed", "chatgpt": "not_probed"}
    except Exception as error:
        # SDK task groups may wrap a failure. Never echo returned file data or exception payloads.
        def fault(exc):
            if isinstance(exc, Fault):
                return exc
            for child in getattr(exc, "exceptions", ()):
                found = fault(child)
                if found:
                    return found
            return None
        known = fault(error)
        if known:
            raise known from None
        raise Fault("service_unready", "MCP initialization or file-read verification failed; inspect service status and permissions.") from None


def probe_service(state: Path, config_path: Path, probe: dict, version: str, pid: int | None = None) -> dict:
    return asyncio.run(_probe(state / "mcp.sock", config_path, probe, version, pid))


def _verify_candidate(state: Path, record: dict, identity: str) -> None:
    installed = _installed(state, identity)
    python = _runtime(state, identity) / "bin/python"
    observed = _candidate_state(python, Path(record["config"]), state)
    if observed["version"] != installed["version"] or observed["prefix"] != str(python.parent.parent):
        raise Fault("installation_identity_mismatch", "The retained runtime changed identity; it will not be activated.")
    with tempfile.TemporaryDirectory(prefix="om-ready-", dir="/tmp") as temporary:
        test_state = Path(temporary) / "state"
        process = subprocess.Popen([str(python), "-I", "-m", "openmachine.cli", "--config", record["config"],
                                    "--state-dir", str(test_state), "serve"], cwd="/",
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not (test_state / "mcp.sock").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise Fault("candidate_unready", "The candidate did not create its private MCP listener; the active service is untouched.")
                time.sleep(0.05)
            probe_service(test_state, Path(record["config"]), record["probe"], installed["version"], process.pid)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _label(state: Path) -> str:
    return "co.openmachine.runtime." + hashlib.sha256(str(state).encode()).hexdigest()[:20]


def _plist_path(state: Path) -> Path:
    return Path.home() / "Library/LaunchAgents" / (_label(state) + ".plist")


def _definition(state: Path, record: dict) -> dict:
    return {"Label": _label(state), "ProgramArguments": [record["launch_python"], "-I", str(state / "service/launch.py")],
            "WorkingDirectory": str(state), "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10, "ExitTimeOut": 10, "AbandonProcessGroup": True,
            "EnvironmentVariables": {"PATH": record["path_environment"]},
            "StandardOutPath": "/dev/null", "StandardErrorPath": str(state / "service/service.log")}


def _verify_files(state: Path, record: dict) -> None:
    try:
        path = _plist_path(state)
        for owned in (path, state / "service/launch.py", state / "service/service.log"):
            info = owned.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError("unsafe service file")
        if (plistlib.loads(path.read_bytes()) != _definition(state, record)
                or (state / "service/launch.py").read_text() != LAUNCHER):
            raise ValueError("service definition changed")
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        raise Fault("service_definition_mismatch", "The installed launcher or LaunchAgent differs; do not overwrite or control another service.") from error


def _native(state: Path) -> dict:
    result = _run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{_label(state)}"])
    if result.returncode:
        if "Could not find service" in result.stderr:
            return {"loaded": False, "pid": None}
        raise Fault("user_service_unavailable", "The macOS GUI user domain is unavailable; log in as the owning user. No privileges were changed.")
    fields = {}
    for line in result.stdout.splitlines():
        found = re.fullmatch(r"\s*(path|program|pid|state|last exit code) = (.*)", line)
        if found and found[1] not in fields:
            fields[found[1]] = found[2].strip('"')
    if fields.get("path") != str(_plist_path(state)):
        raise Fault("service_identity_mismatch", "The loaded job does not belong to this installation; it was not controlled.")
    pid = fields.get("pid", "")
    return {"loaded": True, "pid": int(pid) if pid.isdigit() else None,
            "process_state": fields.get("state", "unknown"), "last_exit_code": fields.get("last exit code")}


def _live_socket(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(path))
        return True
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ECONNREFUSED):
            return False
        raise Fault("socket_state_unknown", "The private listener cannot be verified; do not remove it.") from error


def _stop(state: Path) -> None:
    observed = _native(state)
    if observed["loaded"]:
        _run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{_label(state)}"])
    deadline = time.monotonic() + 15
    while True:
        if not _native(state)["loaded"] and not _live_socket(state / "mcp.sock"):
            return
        if time.monotonic() >= deadline:
            raise Fault("service_stop_unverified", "Service termination is unverified; no candidate was started or worker cancelled.")
        time.sleep(0.1)


def _start(state: Path, record: dict) -> dict:
    observed = _native(state)
    if not observed["loaded"]:
        _run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(_plist_path(state))])
    elif observed["pid"] is None:
        _run(["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{_label(state)}"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = _native(state)
        if observed["pid"] is not None:
            try:
                return probe_service(state, Path(record["config"]), record["probe"],
                                     _installed(state, record["current"])["version"], observed["pid"])
            except Fault as error:
                if error.code != "service_unready":
                    raise
        time.sleep(0.1)
    raise Fault("service_unready", "The managed service did not pass MCP/file readiness; inspect its last exit and OS permissions.")


def _activate(state: Path, record: dict, target: str) -> dict:
    _verify_candidate(state, record, target)
    with execution._lease(state / "locks/lifecycle.lock"):
        # Freeze new cooperating MCP mutations, then check the candidate's readers again.
        _candidate_state(_runtime(state, target) / "bin/python", Path(record["config"]), state)
        original = dict(record)
        pending = record["pending"]
        previous = pending["from"] if pending is not None else record["current"]
        previous_previous = pending["previous"] if pending is not None else record["previous"]
        if previous is None:
            raise Fault("recovery_required", "There is no verified prior installation to restore.")
        record = {**record, "current": target,
                  "pending": {"from": previous, "to": target, "previous": previous_previous}}
        execution._save(state / "service/service.json", record)
        try:
            _stop(state)
            ready = _start(state, record)
        except Fault as error:
            restored = {**original, "current": previous, "previous": previous_previous,
                        "pending": {"from": previous, "to": target, "previous": previous_previous}}
            execution._save(state / "service/service.json", restored)
            try:
                _verify_candidate(state, restored, previous)
                _stop(state)
                _start(state, restored)
            except Fault:
                raise Fault("recovery_required", "Activation and restoration are unverified; selection intent is retained. Inspect status, then explicitly roll back.") from None
            restored["pending"] = None
            execution._save(state / "service/service.json", restored)
            raise Fault("activation_failed_restored", "The candidate failed readiness; the previous service was restored. No task was replayed. Cause: " + error.code) from None
        record.update(previous=previous if previous != target else previous_previous, pending=None)
        execution._save(state / "service/service.json", record)
        return {**ready, "current": target, "previous": record["previous"], "workers": "preserved"}


def status(config_path: Path, state: Path) -> dict:
    if not os.path.lexists(state / "service/service.json"):
        if os.path.lexists(state / "service/launch.py") or os.path.lexists(_plist_path(state)):
            raise Fault("service_state_invalid", "An incomplete installation exists; preserve its files and inspect the original setup before retrying.")
        return {"status": "not_installed", "tunnel": "not_probed", "chatgpt": "not_probed"}
    record = _record(state)
    if str(config_path.resolve()) != record["config"]:
        raise Fault("service_configuration_mismatch", "Use the configuration registered with this state directory.")
    _verify_files(state, record)
    observed = _native(state)
    result = {"status": "stopped", **observed, "current": record["current"], "previous": record["previous"],
              "pending": record["pending"], "version": _installed(state, record["current"])["version"],
              "tunnel": "not_probed", "chatgpt": "not_probed"}
    if observed["pid"] is not None:
        try:
            result.update(probe_service(state, config_path, record["probe"], result["version"], observed["pid"]))
        except Fault as error:
            result.update(status="unready", error=error.code)
    if record["pending"] is not None:
        result["status"] = "activation_pending"
    return result


def operate(config_path: Path, state: Path, action: str, *, wheel: Path | None = None,
            requirements: Path | None = None, workspace: str = "", path: str = "") -> dict:
    if sys.platform != "darwin" or os.getuid() == 0:
        raise Fault("unsupported_service_host", "Managed services require a logged-in macOS user. Linux manual serve and SSH workers remain separate.")
    if not config_path.is_absolute() or not state.is_absolute():
        raise Fault("invalid_service_path", "Use absolute configuration and state paths for a managed service.")
    config_path = config_path.resolve()
    if action == "status":
        return status(config_path, state.resolve())
    state = execution.private_directory(state)
    if len(os.fsencode(state / "mcp.sock")) >= 104:
        raise Fault("invalid_service_path", "Use a shorter state directory for the macOS Unix socket.")
    root = execution.private_directory(state / "service")
    execution.private_directory(state / "locks")
    with execution._lease(root / "operation.lock"):
        if action == "install":
            if any(os.path.lexists(item) for item in (root / "service.json", root / "launch.py", root / "service.log", _plist_path(state))):
                raise Fault("service_already_installed", "Preserve the existing installation; use status, start or update.")
            if wheel is None or requirements is None or not workspace or not path:
                raise Fault("invalid_service_request", "Install requires a local wheel, pinned requirements and explicit probe workspace/path.")
            load(config_path)
            identity = _prepare(state, wheel, requirements, config_path)
            record = {"format": 1, "config": str(config_path), "probe": {"workspace": workspace, "path": path},
                      "current": identity, "previous": None, "pending": None,
                      "launch_python": str(Path(sys._base_executable).resolve()),
                      "path_environment": os.environ.get("PATH", os.defpath)}
            _verify_candidate(state, record, identity)
            with execution._lease(state / "locks/lifecycle.lock"):
                if _live_socket(state / "mcp.sock"):
                    raise Fault("service_already_running", "An unmanaged service is using this state; explicitly stop it before installation.")
                _plist_path(state).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                for destination, content in ((root / "launch.py", LAUNCHER.encode()), (root / "service.log", b""),
                                             (_plist_path(state), plistlib.dumps(_definition(state, record)))):
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                execution._save(root / "service.json", record)
            return {"status": "installed", "version": _installed(state, identity)["version"], "current": identity,
                    "service": "stopped", "next_action": "service start", "tunnel": "not_managed"}
        record = _record(state)
        if str(config_path) != record["config"]:
            raise Fault("service_configuration_mismatch", "Use the registered configuration with this state directory.")
        _verify_files(state, record)
        if action == "rollback":
            target = record["pending"]["from"] if record["pending"] is not None else record["previous"]
            if target is None:
                raise Fault("rollback_unavailable", "No verified prior managed installation is retained.")
            return _activate(state, record, target)
        if record["pending"] is not None:
            raise Fault("activation_pending", "An interrupted transition needs status readback and explicit rollback, not another update.")
        if action == "update":
            if wheel is None or requirements is None:
                raise Fault("invalid_service_request", "Update requires a trusted local wheel and pinned requirements.")
            target = _prepare(state, wheel, requirements, config_path)
            if target == record["current"]:
                return status(config_path, state)
            return _activate(state, record, target)
        with execution._lease(state / "locks/lifecycle.lock"):
            if action in ("start", "restart"):
                _candidate_state(_runtime(state, record["current"]) / "bin/python", config_path, state)
            if action in ("stop", "restart"):
                _stop(state)
            if action in ("start", "restart"):
                return _start(state, record)
            if action == "stop":
                return {"status": "stopped", "workers": "preserved", "tunnel": "not_managed"}
        raise Fault("invalid_service_request", "Use install, start, status, stop, restart, update or rollback.")


if __name__ == "__main__":
    # Candidate-owned cross-version validation, not a second public CLI or executor.
    try:
        if len(sys.argv) != 4 or sys.argv[1] != "check-state":
            raise Fault("invalid_service_request", "Use the openmachine service CLI.")
        print(json.dumps(check_state(Path(sys.argv[2]), Path(sys.argv[3]))))
    except (Fault, OSError) as error:
        print(json.dumps({"error": error.code if isinstance(error, Fault) else "state_unavailable"}))
        sys.exit(1)
