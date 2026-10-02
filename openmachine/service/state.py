"""Private service selection, configuration snapshots and retained recovery state."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory, save_json


PROTOCOL = 1
# Bump for a change to configuration, task or execution formats/writer semantics.
# Managed transitions deliberately do not perform persistent-state migrations.
STATE_CONTRACT = 10
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
            or type(result.get("state_contract")) is not int or result["state_contract"] not in range(1, STATE_CONTRACT + 1) or not isinstance(result.get("version"), str)
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
