"""Bound management CLI and finite, independent product install/update operations."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Literal

from openmachine import __version__
from openmachine.config import initialize, load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

from . import checks, install, payload, runtime, state as service_state, transition

MAX_OPERATIONS = 128
PHASES = {"accepted", "retaining", "retained", "preparing", "prepared", "activating",
          "deferred", "completed", "rejected", "restored", "recovery_required"}
TERMINAL = {"completed", "rejected", "restored"}
CONTINUABLE = {"accepted", "retained", "prepared", "deferred"}
CAPABILITIES = ["status", "bind", "start", "stop", "startup", "configure", "install", "update", "operation", "continue", "rollback"]


def _digest(path: Path) -> str:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or info.st_size > 65536):
        raise Fault("invalid_service_configuration", "Select an owned regular configuration of at most 64 KiB.")
    load(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _selection(state: Path) -> str | None:
    if not os.path.lexists(state / "service/service.json"):
        return None
    record = service_state._record(state)
    # Recovery advances independently; policy, configuration and selection do not.
    record.pop("recovery", None)
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


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
        request = record["request"]
        if (set(record) != {"format", "operation_id", "request", "phase", "created_at_ms", "updated_at_ms", "prepared", "outcome", "reason"}
                or type(record["format"]) is not int or record["format"] != 1 or record["operation_id"] != identity
                or record["phase"] not in PHASES
                or any(type(record[key]) is not int or not 0 < record[key] <= 2**53 - 1 for key in ("created_at_ms", "updated_at_ms"))
                or record["updated_at_ms"] < record["created_at_ms"]
                or not isinstance(record["reason"], str) or not re.fullmatch(r"[a-z0-9_]{0,128}", record["reason"])
                or set(request) != {"action", "config", "source", "payload_sha256", "candidate_config", "config_sha256", "candidate_sha256", "selection_sha256", "workspace", "path"}
                or request["action"] not in {"install", "update"}
                or any(not isinstance(request[key], str) or len(request[key]) > 4096 for key in request if key != "selection_sha256")
                or any(not Path(request[key]).is_absolute() for key in ("config", "source", "candidate_config"))
                or any(not re.fullmatch(r"[0-9a-f]{64}", request[key]) for key in ("payload_sha256", "config_sha256", "candidate_sha256"))
                or request["selection_sha256"] is not None and not re.fullmatch(r"[0-9a-f]{64}", request["selection_sha256"])):
            raise ValueError("unsupported operation")
        prepared = record["prepared"]
        if prepared is not None and (not isinstance(prepared, dict)
                or set(prepared) != {"status", "identity", "version", "payload_sha256", "runtime", "launch_python", "path_environment", "selection", "service"}
                or any(not isinstance(value, str) or len(value) > 8192 for value in prepared.values())
                or not re.fullmatch(r"[0-9a-f]{64}", prepared["identity"])
                or prepared["payload_sha256"] != request["payload_sha256"]
                or prepared["runtime"] != str(state / "service/versions" / prepared["identity"])
                or not Path(prepared["launch_python"]).is_absolute()
                or (prepared["status"], prepared["selection"], prepared["service"]) != ("prepared", "unchanged", "not_started")):
            raise ValueError("invalid prepared identity")
        if (record["phase"] in {"accepted", "retaining", "retained", "preparing"} and prepared is not None
                or record["phase"] in {"prepared", "deferred", "activating", "completed", "restored"} and prepared is None):
            raise ValueError("inconsistent prepared phase")
        if record["outcome"] is not None and not isinstance(record["outcome"], dict):
            raise ValueError("invalid outcome")
        if record["phase"] == "completed" and record["outcome"] is None:
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


def read_operation(config_path: Path, state: Path, identity: str) -> dict:
    record = _read(state, identity)
    if record["request"]["config"] != str(config_path):
        raise Fault("service_configuration_mismatch", "This operation belongs to a different explicit configuration.")
    busy, reason = False, ""
    try:
        with lease(state / "service/operation.lock", create=False):
            pass
    except Fault as error:
        if error.code != "resource_busy":
            raise
        busy = True
    except OSError:
        busy, reason = None, "maintenance_owner_unknown"
    try:
        selection = _selection(state)
    except (Fault, OSError):
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


def submit(config_path: Path, state: Path, action: Literal["install", "update"], identity: str,
           source: Path, sha256: str, *, candidate_config: Path | None = None,
           workspace: str = "", path: str = "") -> dict:
    destination = _operation_path(state, identity)
    if os.path.lexists(destination):
        return read_operation(config_path, state, identity)
    if not source.is_absolute() or candidate_config is not None and not candidate_config.is_absolute():
        raise Fault("invalid_service_path", "Payload and candidate configuration paths must be absolute.")
    # Verification is read-only. The expected digest is supplied by the trusted
    # product artifact, never a feed-selected command or an arbitrary executable.
    payload.verify(source, sha256)
    selected_config = candidate_config or config_path
    config_hash, candidate_hash = _digest(config_path), _digest(selected_config)
    state = private_directory(state)
    root = private_directory(state / "service")
    private_directory(state / "locks")
    with lease(root / "operation.lock") as lock:
        if os.path.lexists(destination):
            return {"status": "busy", "operation": _read(state, identity)}
        _available(state)
        if action == "install":
            if candidate_config is not None or not workspace or not path:
                raise Fault("invalid_service_request", "Installation requires the configured probe workspace/path, without a replacement configuration.")
            _uninstalled(state)
        else:
            record = service_state._record(state)
            _bound(config_path, state, record)
            if record["pending"] is not None:
                raise Fault("activation_pending", "Read the original transition before submitting another update.")
        private_directory(root / "operations")
        request = {"action": action, "config": str(config_path), "source": str(source), "payload_sha256": sha256,
                   "candidate_config": str(selected_config), "config_sha256": config_hash, "candidate_sha256": candidate_hash,
                   "selection_sha256": _selection(state), "workspace": workspace, "path": path}
        if any(isinstance(value, str) and len(value) > 4096 for value in request.values()):
            raise Fault("invalid_service_request", "Maintenance input fields are bounded to 4096 characters.")
        now = time.time_ns() // 1000000
        record = {"format": 1, "operation_id": identity, "request": request, "phase": "accepted",
                  "created_at_ms": now, "updated_at_ms": now, "prepared": None, "outcome": None, "reason": ""}
        record = _save(state, record, "accepted")
        try:
            _spawn(state, identity, lock, sys.executable)
        except OSError:
            # Popen failed before a process was created. The recorded intent is
            # known unperformed and needs explicit continuation, never a new ID.
            record = _save(state, record, "accepted", reason="maintenance_launch_unavailable")
        return {"status": "accepted", "operation": record}


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


def _uninstalled(state: Path) -> None:
    root = state / "service"
    if state.exists() and any(item.name not in {"service", "locks"} for item in state.iterdir()):
        raise Fault("service_adoption_required", "Existing runtime history is not a clean first-run state; inspect its original owner.")
    if any(os.path.lexists(item) for item in (root / "service.json", root / "launch.py", root / "service.log", root / "manage.py", runtime._plist_path(state))):
        raise Fault("service_already_installed", "Existing or incomplete native registration requires readback; no silent adoption is permitted.")
    if runtime._native(state)["loaded"] or runtime.socket_alive(state / "mcp.sock"):
        raise Fault("service_already_running", "An existing native job or listener blocks installation; nothing was stopped.")


def _bound(config_path: Path, state: Path, record: dict) -> None:
    if record["config"] != str(config_path):
        raise Fault("service_configuration_mismatch", "Use the explicit configuration registered with this state.")
    runtime._verify_files(state, record)


def _inputs_unchanged(state: Path, record: dict) -> Path:
    request = record["request"]
    if (_selection(state) != request["selection_sha256"]
            or _digest(Path(request["config"])) != request["config_sha256"]):
        raise Fault("maintenance_input_changed", "Original selection, policy or configuration changed; this operation cannot be rebased or replayed.")
    snapshot = state / "service/configurations" / (request["candidate_sha256"] + ".toml")
    if not os.path.lexists(snapshot):
        if _digest(Path(request["candidate_config"])) != request["candidate_sha256"]:
            raise Fault("maintenance_input_changed", "The exact accepted candidate configuration is no longer available.")
        digest = service_state._snapshot_configuration(state, Path(request["candidate_config"]))
        if digest != request["candidate_sha256"]:
            raise Fault("maintenance_input_changed", "Candidate configuration changed while being retained.")
    return service_state._saved_configuration(state, request["candidate_sha256"])


def run_operation(state: Path, identity: str, lock_fd: int) -> None:
    """One admitted operation; the caller owns the inherited operation flock."""
    record = _read(state, identity)
    if record["phase"] not in CONTINUABLE:
        return
    try:
        request = record["request"]
        snapshot = _inputs_unchanged(state, record)
        if record["phase"] == "accepted":
            record = _save(state, record, "retaining")
            retained, manifest = payload.retain(state, Path(request["source"]), request["payload_sha256"])
            incoming = checks._run([str(retained / "python/bin/python3"), "-I", "-B", "-m", "openmachine.service",
                                   "check-state", str(snapshot), str(state)], env=payload.environment(retained))
            try:
                proof = json.loads(incoming.stdout)
                if (incoming.returncode or type(proof.get("management_contract")) is not int
                        or proof["management_contract"] != service_state.MANAGEMENT_CONTRACT
                        or proof.get("version") != manifest["core"]["version"]
                        or proof.get("prefix") != str(retained / "python")):
                    raise ValueError("incoming implementation contract")
            except (ValueError, TypeError, AttributeError) as error:
                raise Fault("incompatible_management", "The trusted incoming payload does not implement this maintenance contract.") from error
            record = _save(state, record, "retained")
            # A future-state transition runs in the trusted incoming Core, not
            # an old CLI coerced into accepting a contract it cannot read.
            retained = state / "service/payloads" / request["payload_sha256"]
            python = str(retained / "python/bin/python3")
            os.set_inheritable(lock_fd, True)
            os.execve(python, [python, "-I", "-B", "-m", "openmachine.service.maintenance", str(state), identity, str(lock_fd)],
                      payload.environment(retained))
        if record["phase"] == "retained":
            record = _save(state, record, "preparing")
            retained = state / "service/payloads" / request["payload_sha256"]
            prepared = install._prepare_payload(snapshot, state, retained, request["payload_sha256"])
            record = _save(state, record, "prepared", prepared=prepared)
        if record["phase"] not in {"prepared", "deferred"}:
            return
        prepared = record["prepared"]
        config = Path(request["config"])
        def before_change():
            nonlocal record
            _inputs_unchanged(state, record)
            record = _save(state, record, "activating")
        if request["action"] == "install":
            _uninstalled(state)
            result = transition._register(config, state, prepared["identity"], request["workspace"], request["path"],
                                          prepared=prepared, before_change=before_change)
            runtime._set_startup(state, False)
        else:
            selected = service_state._record(state)
            _bound(config, state, selected)
            if selected["current"] == prepared["identity"]:
                if request["candidate_sha256"] != request["config_sha256"]:
                    raise Fault("same_installation", "A configuration change requires an explicit distinct candidate installation.")
                result = runtime.status(config, state)
                if "management" not in selected:
                    with lease(state / "locks/lifecycle.lock"):
                        before_change()
                        selected = service_state._record(state)
                        selected = service_state._bind_management(state, selected, prepared["identity"])
                        save_json(state / "service/service.json", selected)
            else:
                result = transition._activate(state, selected, prepared["identity"], snapshot,
                                              before_change=before_change, require_safe=True)
        _save(state, record, "completed", outcome=result)
    except Fault as error:
        phase = "restored" if error.code == "activation_failed_restored" else (
            "recovery_required" if record["phase"] == "activating" or error.code.endswith("unverified")
            or error.code == "service_output_limit" else
            "deferred" if record["phase"] in {"prepared", "deferred"} and error.code in {
                "resource_busy", "activation_deferred", "legacy_workspace_busy", "incompatible_remote_state"} else "rejected")
        _save(state, record, phase, reason=error.code)
    except OSError:
        # Filesystem acknowledgement loss can follow a completed atomic effect.
        _save(state, record, "recovery_required", reason="maintenance_effect_unverified")


def control(config_path: Path, state: Path, action: Literal["start", "stop", "startup", "bind"], *,
            enabled: bool | None = None, identity: str | None = None) -> dict:
    with lease(state / "service/operation.lock"):
        record = service_state._record(state)
        _bound(config_path, state, record)
        if record["pending"] is not None:
            raise Fault("activation_pending", "Read or explicitly restore the original pending transition first.")
        selected = service_state._installed(state, record["current"])
        if action == "bind":
            with lease(state / "locks/lifecycle.lock"):
                record = service_state._record(state)
                record = service_state._bind_management(state, record, identity or record["current"])
                save_json(state / "service/service.json", record)
        elif action == "start" and record["format"] == 1:
            with lease(state / "locks/lifecycle.lock"):
                checks._candidate_state(service_state._runtime(state, record["current"]) / "bin/python", config_path, state)
            runtime._start(state, record)
        elif selected["state_contract"] < 14:
            raise Fault("incompatible_management", "The selected old Core supports legacy Start/status only; update before persistent Stop or login policy.")
        elif action == "startup" and record["format"] == 1:
            if type(enabled) is not bool:
                raise Fault("invalid_service_request", "Startup requires one explicit boolean.")
            # Native registration already persists this choice. Do not change
            # the launch format of a running legacy job: launchd caches argv
            # until bootout. The first deliberate Stop creates format 2.
            runtime._set_startup(state, enabled)
        else:
            with lease(state / "locks/lifecycle.lock"):
                # The supervisor can advance its existing budget before this
                # admission is acquired. Never publish policy from a stale copy.
                record = service_state._record(state)
                checks._candidate_state(service_state._runtime(state, record["current"]) / "bin/python", config_path, state)
                record = service_state._bind_management(state, record, record["current"])
                policy = record.get("policy") or {"desired": "running", "start_at_login": runtime._startup(state)["enabled"]}
                if action == "startup":
                    if type(enabled) is not bool:
                        raise Fault("invalid_service_request", "Startup requires one explicit boolean.")
                    policy = policy | {"start_at_login": enabled}
                else:
                    policy = policy | {"desired": "running" if action == "start" else "stopped"}
                record = record | {"format": 2, "policy": policy}
                save_json(state / "service/service.json", record)
                if action == "stop":
                    runtime._stop(state)
                # A running legacy job continues with its original definition;
                # the next startup uses the exact new policy-aware entry.
                runtime._sync_definition(state, record)
                if action == "startup":
                    runtime._set_startup(state, enabled)
            if action == "start":
                runtime._start(state, record)
    return observe(config_path, state)


def observe(config_path: Path, state: Path) -> dict:
    result = runtime.status(config_path, state)
    if result["status"] == "not_installed":
        return result
    record = service_state._record(state)
    selected = service_state._installed(state, record["current"])
    result.update(state_contract=selected["state_contract"], policy=record.get("policy"), startup=runtime._startup(state),
                  persistent_control_supported=selected["state_contract"] >= 14)
    if result.get("error") == "resource_busy":
        result.update(status="busy", reason="readiness_busy")
    if result.get("recovery") and runtime._recovery_blocked(result["recovery"], time.time_ns() // 1000000):
        result["status"] = "blocked"
    if service_state.stopped(record) and result["status"] != "activation_pending":
        idle = result.get("supervisor_pid") is None and result.get("pid") is None
        result["status"] = "stopped" if idle and not runtime.socket_alive(state / "mcp.sock") else "blocked"
        result["reason"] = "deliberate_stop" if result["status"] == "stopped" else "stop_unverified"
    if "management" in record:
        result["entry"] = {"python": record["management"]["launch_python"], "path": str(state / "service/manage.py"),
                           "config": str(config_path), "state": str(state), "identity": record["management"]["identity"]}
    if record["pending"] is not None:
        result["status"] = "activation_pending"
    result["product_version"] = selected["version"]
    return result


def dispatch(config_path: Path, state: Path, action: str, *, contract: int = 1, **inputs) -> dict:
    envelope = {"management_contract": service_state.MANAGEMENT_CONTRACT, "capabilities": CAPABILITIES,
                "implementation_version": __version__, "implementation_state_contract": service_state.STATE_CONTRACT}
    try:
        if contract != service_state.MANAGEMENT_CONTRACT:
            raise Fault("incompatible_management", "This Core supports management contract 1.")
        if not config_path.is_absolute() or not state.is_absolute():
            raise Fault("invalid_service_path", "Management requires explicit absolute configuration and state paths.")
        config_path, state = config_path.resolve(), state.resolve()
        if sys.platform != "darwin" or os.getuid() == 0:
            raise Fault("unsupported_service_host", "Management requires the owning logged-in macOS user.")
        if len(os.fsencode(state / "mcp.sock")) >= 104:
            raise Fault("invalid_service_path", "Use a shorter state path for the macOS private socket.")
        if action == "status":
            result = observe(config_path, state)
        elif action == "configure":
            _uninstalled(state)
            result = {"status": "configured", **initialize(config_path, **inputs)}
        elif action in {"install", "update"}:
            result = submit(config_path, state, action, **inputs)
        elif action == "operation":
            result = read_operation(config_path, state, **inputs)
        elif action == "continue":
            result = continue_operation(config_path, state, **inputs)
        elif action == "rollback":
            result = transition.operate(config_path, state, "rollback", require_safe=True)
        elif action in {"start", "stop", "startup", "bind"}:
            result = control(config_path, state, action, **inputs)
        else:
            raise Fault("invalid_service_request", "Select an advertised management action.")
    except (Fault, OSError) as error:
        code = error.code if isinstance(error, Fault) else "management_unavailable"
        result = {"status": "busy" if code in {"resource_busy", "maintenance_busy"} else
                  "incompatible" if code.startswith("incompatible") else "activation_pending" if code == "activation_pending" else
                  "blocked" if code == "runtime_recovery_blocked" else "unready" if code == "service_unready" else "unknown",
                  "reason": code}
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
