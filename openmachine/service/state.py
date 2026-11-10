"""Private Core selection, installation identities and the pinned management entry."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from openmachine.foundation.sanitize import Fault

PROTOCOL = 1
# Contract 29: Code only, with no Tasks, Executions or service process.
STATE_CONTRACT = 29
MANAGEMENT_CONTRACT = 1
# The entry Desktop runs with the pinned interpreter. It is written once, at install, and always
# executes the selected installation's management, so it keeps working after every update.
MANAGER = '''"""Bound Core management entry; no shell or discovery."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

state, config, binding = BINDING
root = Path(state) / "service"
def read(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 65536):
        raise ValueError("unsafe management identity")
    return json.loads(path.read_text())
try:
    record = read(root / "service.json")
    if record["config"] != config or record["management"] != binding:
        raise ValueError("management binding changed")
    identity = record["current"]
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("invalid selection")
    selected = root / "versions" / identity
    if read(selected / "installation.json")["identity"] != identity or selected.resolve() != selected:
        raise ValueError("installation identity changed")
    python = str(selected / "bin/python")
    os.execv(python, [python, "-I", "-m", "openmachine.cli", "--config", config,
                     "--state-dir", state, "manage", *sys.argv[1:]])
except (OSError, ValueError, KeyError, TypeError):
    print(json.dumps({"management_contract": 1, "status": "incompatible",
                      "reason": "management_identity_unavailable",
                      "observed_at_ms": time.time_ns() // 1000000}))
    raise SystemExit(1)
'''
HEX = r"[0-9a-f]{64}"


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
        raise Fault("service_state_invalid", "Core metadata is missing, unsafe or corrupt; preserve it for recovery.") from error


def _record(state: Path) -> dict:
    """The selection: the explicit configuration, the selected installation and the pinned entry."""
    record = _json(state / "service/service.json")
    try:
        binding = record["management"]
        if (set(record) != {"config", "current", "management"} or not Path(record["config"]).is_absolute()
                or not re.fullmatch(HEX, record["current"]) or set(binding) != {"identity", "launch_python"}
                or not re.fullmatch(HEX, binding["identity"]) or not Path(binding["launch_python"]).is_absolute()):
            raise ValueError("unsupported selection")
    except (KeyError, TypeError, ValueError) as error:
        raise Fault("service_state_invalid", "Core selection is invalid; do not replace it or guess a version.") from error
    return record


def selection_digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


def _runtime(state: Path, identity: str) -> Path:
    path = state / "service/versions" / identity
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise Fault("installation_unavailable", "The original immutable installation is unavailable; restore it, not its receipts.")
    return path


def _installed(state: Path, identity: str) -> dict:
    result = _json(_runtime(state, identity) / "installation.json")
    if (set(result) != {"identity", "version", "protocol", "state_contract", "management_contract", "payload_sha256", "commit"}
            or result["identity"] != identity or result["protocol"] != PROTOCOL
            or type(result["state_contract"]) is not int or not 1 <= result["state_contract"] <= STATE_CONTRACT
            or result["management_contract"] != MANAGEMENT_CONTRACT
            or not isinstance(result["version"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+!-]{0,99}", result["version"])
            or not isinstance(result["payload_sha256"], str) or not re.fullmatch(HEX, result["payload_sha256"])
            or not isinstance(result["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", result["commit"])):
        raise Fault("incompatible_runtime", "This installation has no compatible state or management contract; no downgrade was attempted.")
    return result
