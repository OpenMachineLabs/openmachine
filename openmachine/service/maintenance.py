"""Bound management CLI and finite, independent product install and update operations."""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Literal

from openmachine.config import initialize, load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

from . import install, payload, transition
from . import state as service_state

MAX_OPERATIONS = 128
UNPREPARED = {"accepted", "retaining", "retained", "preparing"}
PHASES = UNPREPARED | {"prepared", "activating", "completed", "rejected", "recovery_required"}
TERMINAL = {"completed", "rejected"}
CONTINUABLE = {"accepted", "retained", "prepared", "activating"}
CAPABILITIES = ["status", "configure", "configure:verify_existing", "install", "update", "update:selection_consent",
                "operation", "continue"]


def _selection(state: Path) -> str | None:
    if not os.path.lexists(state / "service/service.json"):
        return None
    return service_state.selection_digest(service_state._record(state))


def _operation_path(state: Path, identity: str) -> Path:
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise Fault("invalid_operation_id", "Use the original 32-lowercase-hex maintenance operation ID.")
    return state / "service/operations" / (identity + ".json")


def _read(state: Path, identity: str) -> dict:
    path = _operation_path(state, identity)
    info = path.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or path.parent.resolve() != path.parent):
        raise Fault("maintenance_state_invalid", "The original maintenance store is not an owned private directory.")
    record = service_state._json(path)
    try:
        request, prepared, phase = record["request"], record["prepared"], record["phase"]
        if (set(record) != {"format", "operation_id", "request", "phase", "created_at_ms", "updated_at_ms", "prepared", "outcome", "reason"}
                or type(record["format"]) is not int or record["format"] != 1 or record["operation_id"] != identity
                or phase not in PHASES
                or any(type(record[key]) is not int or not 0 < record[key] <= 2**53 - 1 for key in ("created_at_ms", "updated_at_ms"))
                or record["updated_at_ms"] < record["created_at_ms"]
                or not isinstance(record["reason"], str) or not re.fullmatch(r"[a-z0-9_]{0,128}", record["reason"])
                or set(request) != {"action", "config", "source", "payload_sha256", "selection_sha256"}
                or request["action"] not in {"install", "update"}
                or any(not isinstance(request[key], str) or len(request[key]) > 4096 or not Path(request[key]).is_absolute()
                       for key in ("config", "source"))
                or not re.fullmatch(service_state.HEX, request["payload_sha256"])
                or (request["selection_sha256"] is None) != (request["action"] == "install")
                or request["selection_sha256"] is not None and not re.fullmatch(service_state.HEX, request["selection_sha256"])):
            raise ValueError("unsupported operation")
        if prepared is not None and (set(prepared) != {"identity", "version"} or prepared["identity"] != request["payload_sha256"]
                                     or not isinstance(prepared["version"], str)):
            raise ValueError("invalid prepared identity")
        if (phase in UNPREPARED and prepared is not None
                or phase in {"prepared", "activating", "completed"} and prepared is None):
            raise ValueError("inconsistent prepared phase")
        if record["outcome"] is not None and not isinstance(record["outcome"], dict):
            raise ValueError("invalid outcome")
        if phase == "completed" and record["outcome"] is None:
            raise ValueError("missing completed outcome")
    except (KeyError, ValueError, TypeError) as error:
        raise Fault("maintenance_state_invalid", "Preserve the original maintenance record; its bounded contract is invalid.") from error
    return record


def _save(state: Path, record: dict, phase: str, *, reason: str = "", **changes) -> dict:
    value = record | changes | {"phase": phase, "reason": reason, "updated_at_ms": time.time_ns() // 1000000}
    if len(json.dumps(value).encode()) > 65536:
        raise Fault("maintenance_record_full", "The bounded maintenance record is full; no further effect was admitted.")
    save_json(_operation_path(state, record["operation_id"]), value)
    if _read(state, record["operation_id"]) != value:
        raise Fault("maintenance_unverified", "Maintenance publication is unverified; read the original operation.")
    return value


def _available(state: Path) -> None:
    root = state / "service/operations"
    if not os.path.lexists(root):
        return
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
        raise Fault("maintenance_state_invalid", "The maintenance store is not a private owned directory.")
    count = 0
    for path in root.iterdir():
        count += 1
        if count > MAX_OPERATIONS or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
            raise Fault("maintenance_store_full", "Preserve retained maintenance history; no new operation was admitted.")
        if _read(state, path.stem)["phase"] not in TERMINAL:
            raise Fault("maintenance_busy", "Read or explicitly continue the original maintenance operation; do not allocate a replacement ID.")
    if count == MAX_OPERATIONS:
        raise Fault("maintenance_store_full", "The bounded maintenance store has reached 128 retained operations.")


def _busy(state: Path) -> bool | None:
    """Whether a maintenance process holds the operation lock; None when that is unknown. A shared probe
    conflicts only with that holder, so concurrent readers never report one another or block an admission."""
    try:
        with lease(state / "service/operation.lock", shared=True, create=False):
            return False
    except FileNotFoundError:
        return False
    except Fault as error:
        if error.code != "resource_busy":
            raise
        return True
    except OSError:
        return None


def read_operation(config_path: Path, state: Path, identity: str) -> dict:
    record = _read(state, identity)
    if record["request"]["config"] != str(config_path):
        raise Fault("service_configuration_mismatch", "This operation belongs to a different explicit configuration.")
    busy = _busy(state)
    reason = "" if busy is not None else "maintenance_owner_unknown"
    try:
        selection = _selection(state)
    except Fault:
        selection, reason = None, "service_selection_unknown"
    phase = record["phase"]
    status = phase if phase in TERMINAL else "busy" if busy else (
        "continuation_required" if phase in CONTINUABLE and busy is False else "recovery_required")
    return {"status": status, "operation": record, "owner_busy": busy,
            "selection_sha256": selection, "reason": reason, "observed_at_ms": time.time_ns() // 1000000}


def _spawn(state: Path, identity: str, lock, python: str) -> None:
    # The child inherits this same flock, not a second lock or a reacquisition.
    # DEVNULL avoids both GUI pipe lifetime and unbounded maintenance log files.
    subprocess.Popen([python, "-I", "-B", "-m", "openmachine.service.maintenance", str(state), identity,
                      str(lock.fileno())], cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(lock.fileno(),))


def _uninstalled(state: Path) -> None:
    if state.exists() and any(item.name != "service" for item in state.iterdir()):
        raise Fault("service_adoption_required", "Existing state is not a clean first-run state; inspect its original owner.")
    if any(os.path.lexists(state / "service" / name) for name in ("service.json", "manage.py")):
        raise Fault("service_already_installed", "An existing or incomplete installation requires readback; no silent adoption is permitted.")


def _unchanged(state: Path, request: dict) -> None:
    """The state an operation was admitted against still holds before each of its effects."""
    if request["action"] == "install":
        _uninstalled(state)
    elif _selection(state) != request["selection_sha256"]:
        raise Fault("maintenance_input_changed", "The selection changed after admission; this operation cannot be rebased or replayed.")


def submit(config_path: Path, state: Path, action: Literal["install", "update"], identity: str,
           source: Path, sha256: str, expected_selection_sha256: str | None = None) -> dict:
    destination = _operation_path(state, identity)
    if os.path.lexists(destination):
        return read_operation(config_path, state, identity)
    if not source.is_absolute():
        raise Fault("invalid_service_path", "The payload path must be absolute.")
    # Verification is read-only. The expected digest comes from the trusted product artifact.
    payload.verify(source, sha256)
    load(config_path)
    state = private_directory(state)
    root = private_directory(state / "service")
    with lease(root / "operation.lock") as lock:
        if os.path.lexists(destination):
            return {"status": "busy", "operation": _read(state, identity)}
        _available(state)
        if action == "install":
            _uninstalled(state)
        else:
            if service_state._record(state)["config"] != str(config_path):
                raise Fault("service_configuration_mismatch", "Use the explicit configuration registered with this state.")
            if _selection(state) != expected_selection_sha256:
                raise Fault("selection_changed", "The confirmed Core installation changed before admission; review it again.")
        private_directory(root / "operations")
        now = time.time_ns() // 1000000
        request = {"action": action, "config": str(config_path), "source": str(source), "payload_sha256": sha256,
                   "selection_sha256": expected_selection_sha256}
        record = {"format": 1, "operation_id": identity, "request": request, "phase": "accepted",
                  "created_at_ms": now, "updated_at_ms": now, "prepared": None, "outcome": None, "reason": ""}
        record = _save(state, record, "accepted")
        try:
            _spawn(state, identity, lock, sys.executable)
        except OSError:
            # Popen failed before a process was created: the intent is known unperformed and needs
            # explicit continuation, never a new ID.
            record = _save(state, record, "accepted", reason="maintenance_launch_unavailable")
        return {"status": "accepted", "operation": record}


def _activated(state: Path, target: str) -> bool:
    """Whether the one atomic selection write already names this operation's installation."""
    return os.path.lexists(state / "service/service.json") and service_state._record(state)["current"] == target


def continue_operation(config_path: Path, state: Path, identity: str) -> dict:
    with lease(state / "service/operation.lock", create=False) as lock:
        record = _read(state, identity)
        if record["request"]["config"] != str(config_path):
            raise Fault("service_configuration_mismatch", "Use the original operation's configuration.")
        if record["phase"] not in CONTINUABLE:
            raise Fault("maintenance_not_continuable", "Preparation or activation may have occurred; read original state without replay.")
        python = sys.executable
        if record["phase"] != "accepted":
            retained = state / "service/payloads" / record["request"]["payload_sha256"]
            payload.verify(retained, record["request"]["payload_sha256"])
            python = str(retained / "python/bin/python3")
        _spawn(state, identity, lock, python)
        return {"status": "accepted", "operation": record}


def run_operation(state: Path, identity: str, lock_fd: int) -> None:
    """One admitted operation; the caller owns the inherited operation flock."""
    record = _read(state, identity)
    if record["phase"] not in CONTINUABLE:
        return
    request = record["request"]
    retained = state / "service/payloads" / request["payload_sha256"]
    try:
        if record["phase"] == "accepted":
            _unchanged(state, request)
            record = _save(state, record, "retaining")
            payload.retain(state, Path(request["source"]), request["payload_sha256"])
            record = _save(state, record, "retained")
            # Preparation and selection run in the incoming Core, which owns the state they write.
            python = str(retained / "python/bin/python3")
            os.set_inheritable(lock_fd, True)
            os.execve(python, [python, "-I", "-B", "-m", "openmachine.service.maintenance", str(state), identity,
                               str(lock_fd)], payload.environment())
        if record["phase"] == "retained":
            record = _save(state, record, "preparing")
            record = _save(state, record, "prepared", prepared=install.prepare(state, retained, request["payload_sha256"]))
        target = record["prepared"]["identity"]
        # An interrupted activation is settled from the selection it would have written, never replayed blindly.
        if record["phase"] == "activating" and _activated(state, target):
            _save(state, record, "completed", outcome={"current": target})
            return
        _unchanged(state, request)
        record = _save(state, record, "activating")
        if request["action"] == "install":
            transition.register(Path(request["config"]), state, target)
        else:
            transition.select(state, target)
        _save(state, record, "completed", outcome={"current": target})
    except Fault as error:
        phase = ("recovery_required" if record["phase"] == "activating" or error.code.endswith("unverified")
                 or error.code == "service_output_limit" else "rejected")
        _save(state, record, phase, reason=error.code)
    except OSError:
        # Filesystem acknowledgement loss can follow a completed atomic effect; continue settles an activation.
        _save(state, record, "activating" if record["phase"] == "activating" else "recovery_required",
              reason="maintenance_effect_unverified")


def observe(config_path: Path, state: Path) -> dict:
    status = "busy" if _busy(state) else None
    if not os.path.lexists(state / "service/service.json"):
        return {"status": status or "not_installed", "current": None, "selection_sha256": None,
                "product_version": None, "product_commit": None, "entry": None}
    record = service_state._record(state)
    if record["config"] != str(config_path):
        raise Fault("service_configuration_mismatch", "Use the explicit configuration registered with this state.")
    installed = service_state._installed(state, record["current"])
    binding = record["management"]
    return {"status": status or "installed", "current": record["current"],
            "selection_sha256": service_state.selection_digest(record),
            "product_version": installed["version"], "product_commit": installed["commit"],
            "entry": {"python": binding["launch_python"],
                      "access_python": str(state / "service/payloads" / installed["payload_sha256"] / "python/bin/python3"),
                      "path": str(state / "service/manage.py"), "config": str(config_path), "state": str(state),
                      "identity": binding["identity"]}}


def dispatch(config_path: Path, state: Path, action: str, *, contract: int = 1, **inputs) -> dict:
    envelope = {"management_contract": service_state.MANAGEMENT_CONTRACT, "capabilities": CAPABILITIES}
    try:
        if contract != service_state.MANAGEMENT_CONTRACT:
            raise Fault("incompatible_management", "This Core supports management contract 1.")
        if not config_path.is_absolute() or not state.is_absolute():
            raise Fault("invalid_service_path", "Management requires explicit absolute configuration and state paths.")
        config_path, state = config_path.resolve(), state.resolve()
        if sys.platform != "darwin" or os.getuid() == 0:
            raise Fault("unsupported_service_host", "Management requires the owning logged-in macOS user.")
        if action == "status":
            result = observe(config_path, state)
        elif action == "configure":
            _uninstalled(state)
            result = {"status": "configured", **initialize(config_path, **inputs)}
        elif action in {"install", "update"}:
            result = submit(config_path, state, action, **inputs)
        elif action == "operation":
            result = read_operation(config_path, state, **inputs)
        else:
            result = continue_operation(config_path, state, **inputs)
    except (Fault, OSError) as error:
        code = error.code if isinstance(error, Fault) else "management_unavailable"
        result = {"status": "busy" if code in {"resource_busy", "maintenance_busy"} else
                  "incompatible" if code.startswith("incompatible") else "unknown", "reason": code}
        if action == "update" and code == "selection_changed" and not os.path.lexists(_operation_path(state, inputs["identity"])):
            result.update(status="rejected", admitted=False, operation_id=inputs["identity"])
    return envelope | result | {"observed_at_ms": time.time_ns() // 1000000}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 4:
            raise ValueError("internal maintenance entry")
        state, identity, fd = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
        info, expected = os.fstat(fd), (state / "service/operation.lock").lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)):
            raise ValueError("not the original operation lock")
        # _spawn hands off the already held open-file description. Reacquiring
        # an operation flock here would violate that ownership handoff.
        run_operation(state, identity, fd)
    except (Fault, OSError, ValueError):
        sys.exit(1)  # Original phase remains readable; observer loss is not failure.
