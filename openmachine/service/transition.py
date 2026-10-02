"""Serialize service operations, activate candidates and restore prior selections."""

import os
from pathlib import Path
import plistlib
import sys

from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

from . import state as service_state
from . import checks
from . import install
from . import runtime
from .state import LAUNCHER


def _activate(state: Path, record: dict, target: str, candidate_config: Path | None = None) -> dict:
    candidate_path = candidate_config or service_state._configuration_for(state, record, target)
    candidate_digest = service_state._snapshot_configuration(state, candidate_path)
    candidate_path = service_state._saved_configuration(state, candidate_digest)
    candidate_record = record | {"config": str(candidate_path)}
    install._verify_candidate(state, candidate_record, target)
    with lease(state / "locks/lifecycle.lock"):
        # The parent may have advanced its budget during candidate preflight.
        # Preserve that current value in any pending/restored selection.
        recovery = service_state._read_recovery(state)
        if recovery is not None:
            record = dict(record, recovery=recovery)
        installed = service_state._installed(state, target)
        checks._candidate_state(service_state._runtime(state, target) / "bin/python", candidate_path, state, installed["state_contract"])
        pending_origin = record["pending"]
        restoring = pending_origin is not None and target == pending_origin["from"]
        if installed["state_contract"] < 4 and not restoring:
            origin = pending_origin["from"] if pending_origin is not None else record["current"]
            current = service_state._installed(state, origin)
            if current["state_contract"] >= 4:
                original_config = service_state._configuration_for(state, record, origin) if pending_origin else Path(record["config"])
                for name, machine in load(original_config).machines.items():
                    if machine.transport == "ssh":
                        retained = checks._remote_workspace_state(service_state._runtime(state, origin) / "bin/python", original_config, name)
                        if retained["workspace_change_store_present"] or not retained["legacy_workspace_compatible"]:
                            raise Fault("incompatible_remote_state", "Retained target changes or live root leases require a W1-compatible controller; no service was stopped.")
        # The actual candidate MCP/SSH readers must also accept all retained remote
        # records under the admission lock, including completed newer-format records.
        if any(item.transport == "ssh" for item in load(candidate_path).machines.values()):
            install._verify_candidate(state, candidate_record, target, check_remote=True)
        original = dict(record)
        pending = record["pending"]
        previous = pending["from"] if pending is not None else record["current"]
        previous_previous = pending["previous"] if pending is not None else record["previous"]
        if previous is None:
            raise Fault("recovery_required", "There is no verified prior installation to restore.")
        config_path = Path(record["config"])
        if pending is None and runtime.socket_alive(state / "mcp.sock"):
            prior = service_state._installed(state, previous)
            checks.probe_service(state, config_path, record["probe"], prior["version"],
                          expected_contract=prior["state_contract"])
        # A normal update snapshots the current registered bytes, including valid
        # edits since the last activation. Recovery uses the already saved intent.
        prior_path = config_path if pending is None else service_state._configuration_for(state, record, previous)
        prior_digest = service_state._snapshot_configuration(state, prior_path)
        configurations = {**record.get("configurations", {}), previous: prior_digest, target: candidate_digest}
        allowed = {prior_digest, candidate_digest}
        if pending is not None and pending["to"] in configurations:
            allowed.add(configurations[pending["to"]])
        record = {**record, "current": target, "configurations": configurations,
                  "pending": {"from": previous, "to": pending["to"] if restoring else target,
                              "previous": previous_previous}}
        save_json(state / "service/service.json", record)
        try:
            service_state._select_configuration(state, config_path, candidate_digest, allowed)
            runtime._stop(state)
            ready = runtime._start(state, record)
        except (Fault, OSError) as error:
            restored = {**original, "current": previous, "previous": previous_previous,
                        "configurations": configurations,
                        "pending": record["pending"]}
            save_json(state / "service/service.json", restored)
            try:
                service_state._select_configuration(state, config_path, prior_digest, allowed)
                install._verify_candidate(state, restored, previous, check_remote=True)
                runtime._stop(state)
                runtime._start(state, restored)
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
    if service_state._installed(state, record["current"])["state_contract"] >= 8:
        # One atomic record commits selection and readiness together. Before
        # this write, a failed first upgrade still has readable state-7 history.
        record = dict(record, recovery={"format": 1, "episode": None})
    save_json(state / "service/service.json", record)
    if service_state._record(state) != record:
        raise Fault("service_state_unverified", "The readiness/selection commit is unverified; read the original transition.")


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
        return runtime.status(config_path, state.resolve())
    state = private_directory(state)
    if len(os.fsencode(state / "mcp.sock")) >= 104:
        raise Fault("invalid_service_path", "Use a shorter state directory for the macOS Unix socket.")
    root = private_directory(state / "service")
    private_directory(state / "locks")
    with lease(root / "operation.lock"):
        if action == "install":
            if any(os.path.lexists(item) for item in (root / "service.json", root / "launch.py", root / "service.log", runtime._plist_path(state))):
                raise Fault("service_already_installed", "Preserve the existing installation; use status, start or update.")
            if wheel is None or requirements is None or not workspace or not path:
                raise Fault("invalid_service_request", "Install requires a local wheel, pinned requirements and explicit probe workspace/path.")
            load(config_path)
            identity = install._prepare(state, wheel, requirements, config_path)
            record = {"format": 1, "config": str(config_path), "probe": {"workspace": workspace, "path": path},
                      "current": identity, "previous": None, "pending": None,
                      "launch_python": str(Path(sys._base_executable).resolve()),
                      "path_environment": os.environ.get("PATH", os.defpath)}
            install._verify_candidate(state, record, identity)
            with lease(state / "locks/lifecycle.lock"):
                if runtime.socket_alive(state / "mcp.sock"):
                    raise Fault("service_already_running", "An unmanaged service is using this state; explicitly stop it before installation.")
                runtime._plist_path(state).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                for destination, content in ((root / "launch.py", LAUNCHER.encode()), (root / "service.log", b""),
                                             (runtime._plist_path(state), plistlib.dumps(runtime._definition(state, record)))):
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                save_json(root / "service.json", record)
            return {"status": "installed", "version": service_state._installed(state, identity)["version"], "current": identity,
                    "service": "stopped", "next_action": "service start", "tunnel": "not_managed"}
        record = service_state._record(state)
        if str(config_path) != record["config"]:
            raise Fault("service_configuration_mismatch", "Use the registered configuration with this state directory.")
        runtime._verify_files(state, record)
        if reset_recovery and service_state._installed(state, record["current"])["state_contract"] < 8:
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
            target = install._prepare(state, wheel, requirements, selected_config)
            if target == record["current"]:
                if selected_config.read_bytes() != config_path.read_bytes():
                    raise Fault("same_installation", "Configuration changes require an explicit distinct candidate installation.")
                return runtime.status(config_path, state)
            return _activate(state, record, target, selected_config)
        with lease(state / "locks/lifecycle.lock"):
            if action in ("start", "restart"):
                checks._candidate_state(service_state._runtime(state, record["current"]) / "bin/python", config_path, state)
            if action in ("stop", "restart"):
                runtime._stop(state)
            if reset_recovery:
                service_state._read_recovery(state)  # Corruption is not a budget reset request.
                with lease(root / "recovery.lock"):
                    service_state._save_recovery(state, None)
            if action == "stop":
                return {"status": "stopped", "workers": "preserved", "tunnel": "not_managed"}
        if action in ("start", "restart"):
            # The parent publishes accounting under shared lifecycle admission.
            # The operation lock still serializes explicit service transitions.
            return runtime._start(state, record)
        raise Fault("invalid_service_request", "Use install, start, status, stop, restart, update or rollback.")
