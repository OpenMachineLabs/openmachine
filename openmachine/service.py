"""Explicit macOS user-service installation, activation and compatible rollback."""

import asyncio
from email.parser import BytesParser
import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import random
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

from openmachine import __version__, execution, tasks, workspace
from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

PROTOCOL = 1
# Bump for a change to configuration, task or execution formats/writer semantics.
# Managed transitions deliberately do not perform persistent-state migrations.
STATE_CONTRACT = 8
READINESS_SECONDS = 5
HEALTH_INTERVAL_SECONDS = 30
RECOVERY_MS = 15 * 60 * 1000
RECOVERY_ATTEMPTS = 15
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
        configurations = record.get("configurations")
        if configurations is not None:
            if (not isinstance(configurations, dict) or len(configurations) > 3 or record["current"] not in configurations
                    or any(not re.fullmatch(r"[0-9a-f]{64}", key) or not re.fullmatch(r"[0-9a-f]{64}", value)
                           for key, value in configurations.items())):
                raise ValueError("invalid configuration snapshots")
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
            or type(result.get("state_contract")) is not int or result["state_contract"] not in range(1, 9) or not isinstance(result.get("version"), str)
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
            execution.read_record(directory)
            count += 1
    task_state = tasks.check_state(state)
    execution.check_workspace_leases(config, state)
    changes = workspace.check_change_state(state)
    return {**changes, **task_state, **_ssh_recovery_state(state), **_recovery_state(state), "version": __version__, "protocol": PROTOCOL, "state_contract": STATE_CONTRACT,
            "prefix": str(Path(sys.prefix).resolve()), "execution_records": count,
            "configuration_sha256": config.configuration_sha256()}


def _ssh_recovery_state(state: Path) -> dict:
    # Import only for retained integration state, including when SSH was disabled
    # later. The connection owner validates its own history; service never edits it.
    if os.path.lexists(state / "ssh-recovery.json") or os.path.lexists(state / "locks/ssh-recovery.lock"):
        from integrations.ssh.recovery import check_state
        return check_state(state)
    return {"ssh_recovery_present": False, "ssh_recovery_format": 1,
            "ssh_recovery_targets": 0, "ssh_recovery_sha256": None}


def _read_recovery(state: Path) -> dict | None:
    path = state / "service/service.json"
    if not os.path.lexists(path) and not os.path.lexists(state / "service/recovery.lock"):
        return None
    try:
        record = _record(state)
        if "recovery" not in record and not os.path.lexists(state / "service/recovery.lock"):
            return None
        value = record.get("recovery")
        if (not isinstance(value, dict) or set(value) != {"format", "episode"}
                or type(value["format"]) is not int or value["format"] != 1):
            raise ValueError("unsupported recovery record")
        episode = value["episode"]
        if episode is not None:
            if (not isinstance(episode, dict) or set(episode) != {"id", "started_at_ms", "deadline_ms", "attempts", "next_attempt_ms", "reason"}
                    or not isinstance(episode["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", episode["id"])
                    or not isinstance(episode["reason"], str) or not re.fullmatch(r"[a-z0-9_]{1,128}", episode["reason"])
                    or any(type(episode[key]) is not int or not 0 <= episode[key] <= 2**53 - 1
                           for key in ("started_at_ms", "deadline_ms", "attempts", "next_attempt_ms"))
                    or not 1 <= episode["attempts"] <= RECOVERY_ATTEMPTS
                    or episode["deadline_ms"] != episode["started_at_ms"] + RECOVERY_MS
                    or not episode["started_at_ms"] <= episode["next_attempt_ms"] <= episode["deadline_ms"]):
                raise ValueError("invalid recovery episode")
        return value
    except (Fault, ValueError, TypeError) as error:
        raise Fault("runtime_recovery_invalid", "Managed recovery history is missing, unsafe or corrupt; preserve it.") from error


def _recovery_state(state: Path) -> dict:
    value = _read_recovery(state)
    return {"runtime_recovery_present": value is not None, "runtime_recovery_format": 1,
            "runtime_recovery_sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() if value else None}


def _save_recovery(state: Path, episode: dict | None) -> None:
    # Lifecycle admission serializes this record with selection changes.
    value = {"format": 1, "episode": episode}
    save_json(state / "service/service.json", dict(_record(state), recovery=value))
    if _read_recovery(state) != value:
        raise Fault("runtime_recovery_unverified", "Recovery accounting was not verified; no further attempt is safe.")


def _recovery_blocked(episode: dict, now: int) -> bool:
    return episode["reason"].startswith("blocked_") or episode["attempts"] >= RECOVERY_ATTEMPTS or now >= episode["deadline_ms"]


def _recovery_attempt(state: Path, episode: dict | None, now: int) -> dict:
    if episode is None:
        episode = {"id": os.urandom(16).hex(), "started_at_ms": now, "deadline_ms": now + RECOVERY_MS,
                   "attempts": 0, "next_attempt_ms": now, "reason": "readiness_pending"}
    if _recovery_blocked(episode, now):
        raise Fault("runtime_recovery_blocked", "Readiness recovery is blocked; inspect status before explicitly renewing its budget.")
    if now < episode["next_attempt_ms"]:
        raise Fault("runtime_recovery_cooldown", "The original recovery episode is still in cooldown.")
    episode = dict(episode, attempts=episode["attempts"] + 1, reason="readiness_pending")
    # Reserve the attempt before any launch/probe. An interrupted attempt remains
    # charged and waits out its deadline, rather than gaining a fresh fast burst.
    episode["next_attempt_ms"] = min(now + READINESS_SECONDS * 1000 + _recovery_delay(episode["attempts"]), episode["deadline_ms"])
    _save_recovery(state, episode)
    return episode


def _recovery_delay(attempts: int) -> int:
    return int((1000 if attempts == 1 else 2000 if attempts == 2 else 60000) * random.uniform(0.8, 1.2))


def _run(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout, cwd="/", check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Fault("service_command_unverified", "The bounded service command failed or timed out; inspect status before retrying.") from error


def _change_support(value: dict) -> set[int]:
    """Interpret an actual reader's coverage, including W1/W2's format-1 report."""
    latest = value.get("workspace_change_format")
    formats = value.get("workspace_change_read_formats", [latest])
    if (type(latest) is not int or latest not in workspace.READ_CHANGE_FORMATS or
        not isinstance(formats, list) or not formats or len(set(formats)) != len(formats) or
        any(type(item) is not int or item not in workspace.READ_CHANGE_FORMATS for item in formats) or latest not in formats):
        raise ValueError("missing or unsupported actual change reader")
    return set(formats)


def _candidate_state(python: Path, config_path: Path, state: Path, expected_contract: int | None = None) -> dict:
    result = _run([str(python), "-I", "-m", "openmachine.service", "check-state", str(config_path), str(state)])
    try:
        value = json.loads(result.stdout)
        if (result.returncode or value["protocol"] != PROTOCOL or type(value["state_contract"]) is not int
                or value["state_contract"] not in range(1, 9)
                or (expected_contract is not None and value["state_contract"] != expected_contract)):
            raise ValueError("incompatible candidate")
        execution.check_workspace_leases(load(config_path), state, legacy_candidate=value["state_contract"] < 4)
        recovery = _ssh_recovery_state(state)
        if recovery["ssh_recovery_present"] and (value["state_contract"] < 7 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in recovery.items())):
            raise ValueError("candidate ignores or changes retained SSH observation budgets")
        runtime_recovery = _recovery_state(state)
        if runtime_recovery["runtime_recovery_present"] and (value["state_contract"] < 8 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in runtime_recovery.items())):
            raise ValueError("candidate ignores or changes retained runtime recovery budgets")
        retained_tasks = tasks.check_state(state)
        if retained_tasks["task_index_format"] == 2:
            formats = value.get("structured_task_read_formats")
            if (value["state_contract"] < 6 or not isinstance(formats, list) or not formats
                    or any(type(item) is not int or item not in tasks.READ_FORMATS for item in formats)
                    or len(formats) != len(set(formats)) or not set(tasks.READ_FORMATS) <= set(formats)
                    or any(type(value.get(key)) is not type(retained_tasks[key]) or value[key] != retained_tasks[key] for key in (
                        "task_store_present", "task_records", "structured_task_records", "task_index_format",
                        "task_index_generation", "task_index_coverage"))):
                raise ValueError("candidate ignores or incompletely reads retained task state")
        retained = workspace.check_change_state(state)
        if retained["workspace_change_store_present"]:
            supported = _change_support(value)
            if (value.get("workspace_change_store_present") is not True or
                value.get("workspace_change_records") != retained["workspace_change_records"] or
                not set(retained["workspace_change_formats_present"]).issubset(supported)):
                raise ValueError("candidate ignores retained workspace state")
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
    versions = private_directory(state / "service/versions")
    destination = versions / digest
    if destination.exists():
        original = _installed(state, digest)
        if original.get("wheel_sha256") != wheel_hash or original.get("version") != version:
            raise Fault("installation_conflict", "The retained installation does not match the selected artifact.")
        return digest
    destination.mkdir(mode=0o700)
    artifacts = private_directory(destination / "artifacts")
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
    save_json(destination / "installation.json", {
        "identity": digest, "version": version, "protocol": observed["protocol"], "state_contract": observed["state_contract"],
        "wheel_sha256": wheel_hash, "requirements_sha256": hashlib.sha256(locked).hexdigest(),
    })
    return digest


async def _probe(socket_path: Path, config_path: Path, probe: dict, expected_version: str,
                 expected_pid: int | None = None, *, expected_contract: int = STATE_CONTRACT,
                 check_remote: bool = False, timeout_seconds: float | None = None) -> dict:
    import httpx2 as httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    config = load(config_path)
    selected = config.workspaces.get(probe["workspace"])
    if selected is None or config.machine(selected.machine).transport != "local":
        raise Fault("invalid_service_probe", "Select one configured local workspace and a permitted existing text file.")
    remote_targets = [name for name, item in config.machines.items() if item.transport == "ssh"] if check_remote else []
    try:
        async with asyncio.timeout(timeout_seconds if timeout_seconds is not None else 8 + 30 * len(remote_targets)):
            async with httpx.AsyncClient(trust_env=False, timeout=30 if remote_targets else 5,
                    transport=httpx.AsyncHTTPTransport(uds=str(socket_path))) as client:
                async with streamable_http_client("http://localhost/mcp", http_client=client) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        initialized = await session.initialize()
                        capabilities = await session.call_tool("get_capabilities", {})
                        caps = capabilities.structured_content
                        fingerprint = config.configuration_sha256()
                        if (capabilities.is_error or initialized.server_info.version != expected_version
                                or caps["version"] != expected_version or caps["configuration_sha256"] != fingerprint
                                or caps["lifecycle"]["protocol"] != PROTOCOL or caps["lifecycle"]["state_contract"] != expected_contract
                                or (expected_pid is not None and caps["lifecycle"]["pid"] != expected_pid)):
                            raise Fault("service_identity_mismatch", "The live service does not match the selected version, configuration or process.")
                        result = await session.call_tool("read_workspace_file", {
                            "machine": selected.machine, "workspace_name": probe["workspace"], "path": probe["path"]})
                        read = result.structured_content
                        if result.is_error or not read["exists"]:
                            raise Fault("service_read_unavailable", "The service could not read the selected permitted file; check the path and OS file permissions.")
                        remote_records = {}
                        for name in remote_targets:
                            cursor, count = "", 0
                            while True:
                                response = await session.call_tool("list_executions", {
                                    "machine": name, "cursor": cursor, "limit": 100})
                                page = response.structured_content
                                if response.is_error or not isinstance(page, dict) or page.get("unreadable"):
                                    raise Fault("incompatible_remote_state", "The candidate cannot validate retained target receipts.")
                                if page.get("target_identity") != config.machines[name].ssh_identity:
                                    raise Fault("incompatible_remote_state", "The candidate did not verify the configured target identity.")
                                count += len(page["executions"])
                                following = page["next_cursor"]
                                if following is None:
                                    break
                                if not isinstance(following, str) or following <= cursor:
                                    raise Fault("incompatible_remote_state", "Target receipt pagination made no progress.")
                                cursor = following
                            remote_records[name] = count
                        return {"status": "ready", "version": expected_version, "configuration_sha256": fingerprint,
                                "ssh_records_checked": remote_records,
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


def probe_service(state: Path, config_path: Path, probe: dict, version: str, pid: int | None = None,
                  *, expected_contract: int = STATE_CONTRACT, check_remote: bool = False,
                  timeout_seconds: float | None = None) -> dict:
    return asyncio.run(_probe(state / "mcp.sock", config_path, probe, version, pid,
                              expected_contract=expected_contract, check_remote=check_remote, timeout_seconds=timeout_seconds))


def _managed_probe(state: Path, record: dict, parent: int, *, child: int | None = None,
                   timeout_seconds: float = READINESS_SECONDS) -> dict:
    deadline = time.monotonic() + timeout_seconds
    installed = _installed(state, record["current"])
    # Callers hold the single readiness slot. The OS job PID is the parent,
    # not the MCP PID; a matching version/config alone cannot prove ownership.
    ready = probe_service(state, Path(record["config"]), record["probe"], installed["version"], child,
                          expected_contract=installed["state_contract"], timeout_seconds=timeout_seconds)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Fault("service_unready", "The MCP readiness attempt exceeded its deadline.")
    if type(ready["pid"]) is not int or ready["pid"] <= 0:
        raise Fault("service_identity_mismatch", "The MCP process identity is invalid.")
    relationship = _run(["/bin/ps", "-o", "ppid=", "-p", str(ready["pid"])], timeout=min(1, remaining))
    if relationship.returncode or relationship.stdout.strip() != str(parent):
        raise Fault("service_identity_mismatch", "The MCP process is not a child of the selected managed service.")
    return {**ready, "supervisor_pid": parent}


def _remote_workspace_state(python: Path, config_path: Path, name: str) -> dict:
    """Ask that exact installed controller and pinned endpoint to validate target history."""
    target = load(config_path).machines[name]
    result = _run([str(python), "-I", "-m", "openmachine.cli", "--config", str(config_path),
                   "doctor", "--machine", name], timeout=35)
    try:
        value = json.loads(result.stdout)
        if (result.returncode not in (0, 1) or value.get("identity_verified") is not True or
            result.returncode == 1 and not value.get("unavailable_workspaces") or
            value.get("target_identity") != target.ssh_identity or
            value.get("worker_sha256") != Path(target.ssh_worker).stem or
            not _change_support(value) or
            type(value.get("workspace_change_records")) is not int or not 0 <= value["workspace_change_records"] <= workspace.MAX_CHANGES or
            type(value.get("workspace_change_store_present")) is not bool or
            type(value.get("legacy_workspace_compatible")) is not bool or
            not value["workspace_change_store_present"] and value["workspace_change_records"] != 0):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise Fault("incompatible_remote_state", "The original target workspace history or lease compatibility was not verified.") from error


def _verify_candidate(state: Path, record: dict, identity: str, *, check_remote: bool = False) -> None:
    installed = _installed(state, identity)
    python = _runtime(state, identity) / "bin/python"
    observed = _candidate_state(python, Path(record["config"]), state, installed["state_contract"])
    if observed["version"] != installed["version"] or observed["prefix"] != str(python.parent.parent):
        raise Fault("installation_identity_mismatch", "The retained runtime changed identity; it will not be activated.")
    if check_remote and installed["state_contract"] >= 4:
        for name, target in load(Path(record["config"])).machines.items():
            if target.transport == "ssh":
                _remote_workspace_state(python, Path(record["config"]), name)
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
            probe_service(test_state, Path(record["config"]), record["probe"], installed["version"], process.pid,
                          expected_contract=installed["state_contract"], check_remote=check_remote)
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
            "ThrottleInterval": 10, "ExitTimeOut": 10,
            "AbandonProcessGroup": _installed(state, record["current"])["state_contract"] < 8,
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
        selections = {record["current"]}
        if record["pending"] is not None:
            selections.update((record["pending"]["from"], record["pending"]["to"]))
        if (plistlib.loads(path.read_bytes()) not in [_definition(state, dict(record, current=identity)) for identity in selections]
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
    if record["pending"] is None and _installed(state, record["current"])["state_contract"] >= 8:
        if _read_recovery(state) is None:
            _save_recovery(state, None)
    observed = _native(state)
    if not observed["loaded"]:
        # Only verified transition definitions are accepted; write the selected
        # profile after bootout. Interrupted transitions retain both identities.
        _verify_files(state, record)
        desired = plistlib.dumps(_definition(state, record))
        if _plist_path(state).read_bytes() != desired:
            fd, temporary = tempfile.mkstemp(prefix=".openmachine-", dir=_plist_path(state).parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(desired)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, _plist_path(state))
                directory = os.open(_plist_path(state).parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(temporary).unlink(missing_ok=True)
        _run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(_plist_path(state))])
    elif observed["pid"] is None:
        _run(["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{_label(state)}"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = _native(state)
        if record["pending"] is None:
            recovery = _read_recovery(state)
            if recovery and recovery["episode"]:
                if recovery["episode"]["reason"].startswith("blocked_"):
                    raise Fault("runtime_recovery_blocked", "The persisted recovery budget is blocked; inspect service status.")
                # Only the owner may advance an active recovery episode. An
                # explicit start observes it, including its original cooldown.
                time.sleep(0.1)
                continue
        if observed["pid"] is not None:
            try:
                if _installed(state, record["current"])["state_contract"] >= 8:
                    with lease(state / "service/readiness.lock"):
                        return _managed_probe(state, record, observed["pid"])
                return probe_service(state, Path(record["config"]), record["probe"],
                                     _installed(state, record["current"])["version"], observed["pid"],
                                     expected_contract=_installed(state, record["current"])["state_contract"])
            except Fault as error:
                if error.code not in {"service_unready", "resource_busy"}:
                    raise
        time.sleep(0.1)
    raise Fault("service_unready", "The managed service did not pass MCP/file readiness; inspect its last exit and OS permissions.")


def _launch_mcp(state: Path, record: dict) -> subprocess.Popen:
    return subprocess.Popen([str(_runtime(state, record["current"]) / "bin/python"), "-I", "-m", "openmachine.cli",
                             "--config", record["config"], "--state-dir", str(state), "serve", "--managed-child"],
                            cwd=state, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


def _publish_recovery(state: Path, record: dict, episode: dict | None, stop: threading.Event) -> bool:
    while not stop.is_set():
        try:
            with lease(state / "locks/lifecycle.lock", shared=True):
                current = _record(state)
                if current["current"] != record["current"] or current["pending"] is not None:
                    return False
                _save_recovery(state, episode)
                return True
        except Fault as error:
            if error.code != "resource_busy":
                raise
            stop.wait(0.05)
    return False


def _stop_mcp(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _monitor_mcp(state: Path, record: dict, child: subprocess.Popen, stop: threading.Event) -> None:
    next_check = time.monotonic() + HEALTH_INTERVAL_SECONDS
    while child.poll() is None and not stop.is_set():
        remaining = next_check - time.monotonic()
        if remaining > 0:
            stop.wait(min(0.1, remaining))
            continue
        try:
            with lease(state / "service/readiness.lock"):
                _managed_probe(state, record, os.getpid(), child=child.pid)
        except Fault as error:
            if error.code != "resource_busy":
                raise
            # Another bounded readiness caller owns the slot; contention is
            # not evidence that the MCP child needs to be replaced.
            next_check = time.monotonic() + 1
        else:
            next_check = time.monotonic() + HEALTH_INTERVAL_SECONDS


def supervise(config_path: Path, state: Path) -> None:
    """Own only the selected MCP child; launchd retains the service lifetime."""
    stop = threading.Event()
    previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    child = None
    try:
        record = _record(state)
        _verify_files(state, record)
        if (str(config_path.resolve()) != record["config"] or _native(state)["pid"] != os.getpid()
                or Path(sys.prefix).resolve() != _runtime(state, record["current"])
                or os.getpgrp() != os.getpid()):
            raise Fault("service_identity_mismatch", "Only the selected launchd job may supervise this MCP runtime.")
        # Activation owns one candidate start and its readiness proof. Defer
        # recovery state until commit so a failed first upgrade can restore v7.
        if record["pending"] is not None:
            child = _launch_mcp(state, record)
            while not stop.is_set() and _record(state)["pending"] is not None:
                if child.poll() is not None:
                    return
                stop.wait(0.05)
        if stop.is_set():
            return
        if _read_recovery(state) is None:
            raise Fault("runtime_recovery_invalid", "The service operation has not initialized its recovery record.")
        with lease(state / "service/recovery.lock"):
            while not stop.is_set():
                episode = _read_recovery(state)["episode"]
                now = time.time_ns() // 1000000
                if episode is not None and _recovery_blocked(episode, now):
                    reason = episode["reason"] if episode["reason"].startswith("blocked_") else "blocked_budget_exhausted"
                    _publish_recovery(state, record, dict(episode, reason=reason), stop)
                    return
                if episode is not None and now < episode["next_attempt_ms"]:
                    stop.wait((episode["next_attempt_ms"] - now) / 1000)
                    continue
                monitoring = False
                try:
                    with lease(state / "service/readiness.lock"):
                        # Do not hold the application admission lock over a wait
                        # or probe, but serialize accounting with activation.
                        with lease(state / "locks/lifecycle.lock", shared=True):
                            current = _record(state)
                            if current["current"] != record["current"] or current["pending"] is not None:
                                return
                            episode = _recovery_attempt(state, episode, now)
                        deadline = time.monotonic() + min(READINESS_SECONDS, (episode["deadline_ms"] - now) / 1000)
                        if child is None or child.poll() is not None:
                            load(Path(record["config"]))
                            child = _launch_mcp(state, record)
                        while not (state / "mcp.sock").exists() and child.poll() is None and not stop.is_set() and time.monotonic() < deadline:
                            stop.wait(min(0.05, max(0, deadline - time.monotonic())))
                        if stop.is_set():
                            return
                        if child.poll() is not None:
                            raise Fault("service_child_exited", "The original MCP child exited before readiness.")
                        if time.monotonic() >= deadline:
                            raise Fault("service_unready", "The MCP child did not become ready within its attempt.")
                        _managed_probe(state, record, os.getpid(), child=child.pid, timeout_seconds=deadline - time.monotonic())
                    if not _publish_recovery(state, record, None, stop):
                        return
                    monitoring = True
                    _monitor_mcp(state, record, child, stop)
                except (Fault, OSError) as error:
                    if isinstance(error, Fault) and error.code == "resource_busy":
                        stop.wait(0.05)
                        continue
                    if monitoring:
                        # Read-only health checks do not spend a recovery
                        # budget. Persist the new episode before cleanup or
                        # replacement, under the existing activation guard.
                        while not stop.is_set():
                            try:
                                with lease(state / "locks/lifecycle.lock", shared=True):
                                    current = _record(state)
                                    if current["current"] != record["current"] or current["pending"] is not None:
                                        return
                                    episode = _recovery_attempt(state, None, time.time_ns() // 1000000)
                                break
                            except Fault as busy:
                                if busy.code != "resource_busy":
                                    raise
                                stop.wait(0.05)
                        if stop.is_set():
                            return
                    elif episode is None:
                        raise
                    code = error.code if isinstance(error, Fault) else "service_state_unavailable"
                    retryable = code in {"service_unready", "service_child_exited"}
                    episode = dict(episode, reason=code if retryable else "blocked_" + code)
                    episode["next_attempt_ms"] = min(time.time_ns() // 1000000 + _recovery_delay(episode["attempts"]), episode["deadline_ms"])
                    if not _publish_recovery(state, record, episode, stop):
                        return
                    if monitoring and retryable:
                        _stop_mcp(child)
                        child = None
                    continue
    except (Fault, OSError) as error:
        # Exit successfully to suppress launchd's automatic restart on a known
        # fatal boundary. CLI start/status still report the actual unready state.
        print(json.dumps({"error": error.code if isinstance(error, Fault) else "service_state_unavailable"}), file=sys.stderr)
    finally:
        try:
            if child is not None:
                _stop_mcp(child)
        finally:
            signal.signal(signal.SIGTERM, previous)


def _saved_configuration(state: Path, digest: str) -> Path:
    path = state / "service/configurations" / (digest + ".toml")
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 65536
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("invalid snapshot")
    except (OSError, ValueError) as error:
        raise Fault("configuration_unavailable", "The exact retained configuration is unavailable; preserve transition state.") from error
    return path


def _snapshot_configuration(state: Path, source: Path) -> str:
    load(source)
    data = source.read_bytes()
    if len(data) > 65536:
        raise Fault("invalid_service_configuration", "Managed configuration snapshots are bounded to 64 KiB.")
    digest = hashlib.sha256(data).hexdigest()
    root = private_directory(state / "service/configurations")
    destination = root / (digest + ".toml")
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=root)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
        os.unlink(temporary)
        _saved_configuration(state, digest)
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return digest


def _configuration_for(state: Path, record: dict, identity: str) -> Path:
    snapshots = record.get("configurations")
    if snapshots is None:
        # Legacy selection did not change configuration during activation.
        return Path(record["config"])
    if identity not in snapshots:
        raise Fault("configuration_unavailable", "No exact configuration is retained for the requested installation.")
    return _saved_configuration(state, snapshots[identity])


def _select_configuration(state: Path, path: Path, digest: str, allowed: set[str]) -> None:
    data = _saved_configuration(state, digest).read_bytes()
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_size > 65536 or hashlib.sha256(path.read_bytes()).hexdigest() not in allowed):
        raise Fault("configuration_changed", "The registered configuration changed outside this transition; do not overwrite it.")
    if path.read_bytes() == data:
        return
    fd, temporary = tempfile.mkstemp(prefix=".openmachine-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _activate(state: Path, record: dict, target: str, candidate_config: Path | None = None) -> dict:
    candidate_path = candidate_config or _configuration_for(state, record, target)
    candidate_digest = _snapshot_configuration(state, candidate_path)
    candidate_path = _saved_configuration(state, candidate_digest)
    candidate_record = record | {"config": str(candidate_path)}
    _verify_candidate(state, candidate_record, target)
    with lease(state / "locks/lifecycle.lock"):
        # The parent may have advanced its budget during candidate preflight.
        # Preserve that current value in any pending/restored selection.
        recovery = _read_recovery(state)
        if recovery is not None:
            record = dict(record, recovery=recovery)
        installed = _installed(state, target)
        _candidate_state(_runtime(state, target) / "bin/python", candidate_path, state, installed["state_contract"])
        pending_origin = record["pending"]
        restoring = pending_origin is not None and target == pending_origin["from"]
        if installed["state_contract"] < 4 and not restoring:
            origin = pending_origin["from"] if pending_origin is not None else record["current"]
            current = _installed(state, origin)
            if current["state_contract"] >= 4:
                original_config = _configuration_for(state, record, origin) if pending_origin else Path(record["config"])
                for name, machine in load(original_config).machines.items():
                    if machine.transport == "ssh":
                        retained = _remote_workspace_state(_runtime(state, origin) / "bin/python", original_config, name)
                        if retained["workspace_change_store_present"] or not retained["legacy_workspace_compatible"]:
                            raise Fault("incompatible_remote_state", "Retained target changes or live root leases require a W1-compatible controller; no service was stopped.")
        # The actual candidate MCP/SSH readers must also accept all retained remote
        # records under the admission lock, including completed newer-format records.
        if any(item.transport == "ssh" for item in load(candidate_path).machines.values()):
            _verify_candidate(state, candidate_record, target, check_remote=True)
        original = dict(record)
        pending = record["pending"]
        previous = pending["from"] if pending is not None else record["current"]
        previous_previous = pending["previous"] if pending is not None else record["previous"]
        if previous is None:
            raise Fault("recovery_required", "There is no verified prior installation to restore.")
        config_path = Path(record["config"])
        if pending is None and _live_socket(state / "mcp.sock"):
            prior = _installed(state, previous)
            probe_service(state, config_path, record["probe"], prior["version"],
                          expected_contract=prior["state_contract"])
        # A normal update snapshots the current registered bytes, including valid
        # edits since the last activation. Recovery uses the already saved intent.
        prior_path = config_path if pending is None else _configuration_for(state, record, previous)
        prior_digest = _snapshot_configuration(state, prior_path)
        configurations = {**record.get("configurations", {}), previous: prior_digest, target: candidate_digest}
        allowed = {prior_digest, candidate_digest}
        if pending is not None and pending["to"] in configurations:
            allowed.add(configurations[pending["to"]])
        record = {**record, "current": target, "configurations": configurations,
                  "pending": {"from": previous, "to": pending["to"] if restoring else target,
                              "previous": previous_previous}}
        save_json(state / "service/service.json", record)
        try:
            _select_configuration(state, config_path, candidate_digest, allowed)
            _stop(state)
            ready = _start(state, record)
        except (Fault, OSError) as error:
            restored = {**original, "current": previous, "previous": previous_previous,
                        "configurations": configurations,
                        "pending": record["pending"]}
            save_json(state / "service/service.json", restored)
            try:
                _select_configuration(state, config_path, prior_digest, allowed)
                _verify_candidate(state, restored, previous, check_remote=True)
                _stop(state)
                _start(state, restored)
            except (Fault, OSError):
                raise Fault("recovery_required", "Activation and restoration are unverified; exact configuration/selection intent is retained.") from None
            restored = {**original, "current": previous, "previous": previous_previous, "pending": None}
            if "configurations" in restored:
                restored["configurations"] = {key: value for key, value in {
                    **restored["configurations"], previous: prior_digest}.items()
                    if key in {previous, previous_previous}}
            _commit_ready(state, restored)
            cause = error.code if isinstance(error, Fault) else type(error).__name__
            raise Fault("activation_failed_restored", "The previous service and exact configuration were restored. No task was replayed. Cause: " + cause) from None
        record.update(previous=previous if previous != target else previous_previous, pending=None)
        retained = {record["current"], record["previous"]} - {None}
        record["configurations"] = {key: value for key, value in configurations.items() if key in retained}
        _commit_ready(state, record)
        return {**ready, "current": target, "previous": record["previous"], "workers": "preserved"}


def _commit_ready(state: Path, record: dict) -> None:
    if _installed(state, record["current"])["state_contract"] >= 8:
        # One atomic record commits selection and readiness together. Before
        # this write, a failed first upgrade still has readable state-7 history.
        record = dict(record, recovery={"format": 1, "episode": None})
    save_json(state / "service/service.json", record)
    if _record(state) != record:
        raise Fault("service_state_unverified", "The readiness/selection commit is unverified; read the original transition.")


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
    result = {"status": "unready" if observed["loaded"] else "stopped", **observed, "current": record["current"], "previous": record["previous"],
              "pending": record["pending"], "version": _installed(state, record["current"])["version"],
              "tunnel": "not_probed", "chatgpt": "not_probed"}
    recovery = _read_recovery(state)
    result["recovery"] = recovery["episode"] if recovery else None
    managed = _installed(state, record["current"])["state_contract"] >= 8
    if managed:
        result.update(supervisor_pid=observed["pid"], pid=None)
    if recovery and recovery["episode"] and observed["loaded"]:
        result["status"] = "blocked" if recovery["episode"]["reason"].startswith("blocked_") else "recovering"
    elif observed["pid"] is not None:
        try:
            if managed:
                with lease(state / "service/readiness.lock"):
                    result.update(_managed_probe(state, record, observed["pid"]))
            else:
                result.update(probe_service(state, config_path, record["probe"], result["version"], observed["pid"],
                                            expected_contract=_installed(state, record["current"])["state_contract"]))
        except Fault as error:
            result.update(status="recovering" if error.code == "resource_busy" else "unready", error=error.code)
    if record["pending"] is not None:
        result["status"] = "activation_pending"
    return result


def operate(config_path: Path, state: Path, action: str, *, wheel: Path | None = None,
            requirements: Path | None = None, workspace: str = "", path: str = "",
            candidate_config: Path | None = None, reset_recovery: bool = False) -> dict:
    if sys.platform != "darwin" or os.getuid() == 0:
        raise Fault("unsupported_service_host", "Managed services require a logged-in macOS user. Linux manual serve and SSH workers remain separate.")
    if not config_path.is_absolute() or not state.is_absolute():
        raise Fault("invalid_service_path", "Use absolute configuration and state paths for a managed service.")
    if reset_recovery and action != "restart":
        raise Fault("invalid_service_request", "Explicit recovery renewal requires service restart --reset-recovery.")
    config_path = config_path.resolve()
    if action == "status":
        return status(config_path, state.resolve())
    state = private_directory(state)
    if len(os.fsencode(state / "mcp.sock")) >= 104:
        raise Fault("invalid_service_path", "Use a shorter state directory for the macOS Unix socket.")
    root = private_directory(state / "service")
    private_directory(state / "locks")
    with lease(root / "operation.lock"):
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
            with lease(state / "locks/lifecycle.lock"):
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
                save_json(root / "service.json", record)
            return {"status": "installed", "version": _installed(state, identity)["version"], "current": identity,
                    "service": "stopped", "next_action": "service start", "tunnel": "not_managed"}
        record = _record(state)
        if str(config_path) != record["config"]:
            raise Fault("service_configuration_mismatch", "Use the registered configuration with this state directory.")
        _verify_files(state, record)
        if reset_recovery and _installed(state, record["current"])["state_contract"] < 8:
            raise Fault("incompatible_runtime", "This installation does not own managed readiness recovery.")
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
            selected_config = candidate_config.resolve(strict=True) if candidate_config is not None else config_path
            target = _prepare(state, wheel, requirements, selected_config)
            if target == record["current"]:
                if selected_config.read_bytes() != config_path.read_bytes():
                    raise Fault("same_installation", "Configuration changes require an explicit distinct candidate installation.")
                return status(config_path, state)
            return _activate(state, record, target, selected_config)
        with lease(state / "locks/lifecycle.lock"):
            if action in ("start", "restart"):
                _candidate_state(_runtime(state, record["current"]) / "bin/python", config_path, state)
            if action in ("stop", "restart"):
                _stop(state)
            if reset_recovery:
                _read_recovery(state)  # Corruption is not a budget reset request.
                with lease(root / "recovery.lock"):
                    _save_recovery(state, None)
            if action == "stop":
                return {"status": "stopped", "workers": "preserved", "tunnel": "not_managed"}
        if action in ("start", "restart"):
            # The parent publishes accounting under shared lifecycle admission.
            # The operation lock still serializes explicit service transitions.
            return _start(state, record)
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
