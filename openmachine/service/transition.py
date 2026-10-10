"""Serialize service operations, activate candidates and restore prior selections."""

from collections.abc import Callable
from contextlib import ExitStack
import os
from pathlib import Path
import plistlib
import sys

from openmachine import tasks
from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

from . import state as service_state
from . import checks
from . import install
from . import runtime
from .state import LAUNCHER


def _activate(state: Path, record: dict, target: str, candidate_config: Path | None = None, *,
              before_change: Callable[[], None] | None = None, require_safe: bool = False) -> dict:
    candidate_path = candidate_config or service_state._configuration_for(state, record, target)
    candidate_digest = service_state._snapshot_configuration(state, candidate_path)
    candidate_path = service_state._saved_configuration(state, candidate_digest)
    candidate_record = record | {"config": str(candidate_path)}
    install._verify_candidate(state, candidate_record, target)
    with lease(state / "locks/lifecycle.lock"), ExitStack() as activation_guards:
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
        original_settings, candidate_settings = load(Path(record["config"])), load(candidate_path)
        for name, machine in original_settings.machines.items():
            if machine.transport == "ssh" and name not in candidate_settings.machines:
                checks.machine_unused(original_settings, state, name)
        if require_safe:
            checks.activation_safe(state, config=original_settings, candidate_config=candidate_settings,
                                   guards=activation_guards)
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
        if before_change is not None:
            before_change()
        save_json(state / "service/service.json", record)
        try:
            service_state._select_configuration(state, config_path, candidate_digest, allowed)
            runtime._stop(state)
            if installed["state_contract"] >= 18:
                tasks.migrate(state)
            ready = _start_selected(state, record)
        except (Fault, OSError) as error:
            if installed["state_contract"] >= 18 and service_state._installed(state, previous)["state_contract"] < 18:
                try:
                    storage = tasks.storage_state(state)
                    cutover = storage["kind"] == "sqlite" or storage["fenced"]
                except (Fault, OSError):
                    cutover = True  # An unreadable authority cannot justify an old writer.
                if cutover:
                    raise Fault("task_activation_pending", "Task cutover requires this candidate. Continue the original maintenance operation, or explicitly restart this pending service activation.") from error
            restored = {**original, "current": previous, "previous": previous_previous,
                        "configurations": configurations,
                        "pending": record["pending"]}
            save_json(state / "service/service.json", restored)
            try:
                service_state._select_configuration(state, config_path, prior_digest, allowed)
                install._verify_candidate(state, restored, previous, check_remote=True)
                runtime._stop(state)
                _start_selected(state, restored)
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
        return _finish_activation(state, record, ready, bind_management=require_safe)


def _task_cutover(state: Path, record: dict, *, committed: bool = False) -> bool:
    pending, target = record["pending"], record["current"]
    if (pending is None and not committed or pending is not None
            and (pending["to"] != target or pending["from"] in {None, target})):
        return False
    digest = record.get("configurations", {}).get(target)
    if (service_state._installed(state, target)["state_contract"] < 18 or digest is None
            or service_state._configuration_digest(Path(record["config"])) != digest):
        return False
    service_state._saved_configuration(state, digest)
    storage = tasks.storage_state(state)
    return (storage["fenced"] and not storage.get("restore_required", False)
            and (storage["kind"] == "sqlite" and (pending is not None or storage["active"])
                 or storage["kind"] == "absent" and pending is not None and storage["active"] is False))


def _resume_activation(state: Path, record: dict) -> dict:
    """Finish the proven original cutover; never restore a pre-ledger writer."""
    with lease(state / "locks/lifecycle.lock"), ExitStack() as activation_guards:
        current = service_state._record(state)
        if ({key: value for key, value in current.items() if key != "recovery"}
                != {key: value for key, value in record.items() if key != "recovery"}):
            raise Fault("maintenance_input_changed", "The original activation selection changed; preserve its maintenance operation.")
        record, target = current, current["current"]
        if not _task_cutover(state, record, committed=True):
            raise Fault("maintenance_not_continuable", "The original candidate, configuration and Task cutover are not verified.")
        if record["pending"] is None:
            # Readiness committed but the maintenance acknowledgement was lost.
            # A live controller may have admitted new work: observation only.
            install._verify_candidate(state, record, target, check_remote=True)
            ready = runtime.status(Path(record["config"]), state)
            if (ready.get("current") != target or ready.get("status") != "ready"
                    and not (ready.get("status") == "stopped" and service_state.stopped(record))):
                raise Fault("service_state_unverified", "The original committed selection is not verifiably ready or deliberately stopped.")
            return ready
        prior_config = service_state._configuration_for(state, record, record["pending"]["from"])
        checks.activation_safe(state, config=load(prior_config),
                               candidate_config=load(Path(record["config"])), guards=activation_guards)
        runtime._stop(state)
        tasks.migrate(state)
        install._verify_candidate(state, record, target, check_remote=True)
        ready = _start_selected(state, record)
        return _finish_activation(state, record, ready, bind_management=True)


def _finish_activation(state: Path, record: dict, ready: dict, *, bind_management: bool) -> dict:
    pending, target = record["pending"], record["current"]
    record = record | {"previous": pending["from"] if pending["from"] != target else pending["previous"], "pending": None}
    retained = {target, record["previous"]} - {None}
    record["configurations"] = {key: value for key, value in record["configurations"].items() if key in retained}
    installed = service_state._installed(state, target)
    if bind_management and installed.get("management_contract") == service_state.MANAGEMENT_CONTRACT and installed.get("payload_sha256"):
        record = service_state._bind_management(state, record, target)
    _commit_ready(state, record)
    return {**ready, "current": target, "previous": record["previous"], "workers": "preserved",
            "version": installed["version"], "commit": installed.get("commit")}


def _start_selected(state: Path, record: dict) -> dict:
    """The original job is unloaded; retain admission until readiness is proven."""
    pending = record["pending"]
    if (pending is not None and record["current"] == pending["from"]
            and service_state._installed(state, record["current"])["state_contract"]
                < service_state._installed(state, pending["to"])["state_contract"]):
        # Publish the exact restored profile before an older supervisor reads it.
        # Its remaining handoff must not name an unreadable newer installation.
        runtime._sync_definition(state, record)
        record["pending"] = pending | {"to": record["current"]}
        save_json(state / "service/service.json", record)
    if service_state.stopped(record):
        runtime._sync_definition(state, record)
        return {"status": "stopped", "readiness": "not_probed"}
    return runtime._start(state, record)


def _commit_ready(state: Path, record: dict) -> None:
    if not service_state.stopped(record) and service_state._installed(state, record["current"])["state_contract"] >= 8:
        # One atomic record commits selection and readiness together. Before
        # this write, a failed first upgrade still has readable state-7 history.
        record = dict(record, recovery={"format": 1, "episode": None})
    save_json(state / "service/service.json", record)
    if service_state._record(state) != record:
        raise Fault("service_state_unverified", "The readiness/selection commit is unverified; read the original transition.")


def _register(config_path: Path, state: Path, identity: str, workspace: str, path: str,
              prepared: dict | None = None, before_change: Callable[[], None] | None = None) -> dict:
    """Caller owns operation.lock; preserve the existing native registration owner."""
    root = state / "service"
    record = {"format": 1, "config": str(config_path), "probe": {"workspace": workspace, "path": path},
              "current": identity, "previous": None, "pending": None,
              "launch_python": str(Path(sys._base_executable).resolve()),
              "path_environment": os.environ.get("PATH", os.defpath)}
    if prepared is not None:
        record.update(launch_python=prepared["launch_python"], path_environment=prepared["path_environment"])
    install._verify_candidate(state, record, identity)
    with lease(state / "locks/lifecycle.lock"):
        if runtime.socket_alive(state / "mcp.sock"):
            raise Fault("service_already_running", "An unmanaged service is using this state; explicitly stop it before installation.")
        if prepared is not None:
            checks.activation_safe(state)
            if before_change is not None:
                before_change()
            record = service_state._bind_management(state, record, identity)
            record.update(format=2, policy={"desired": "stopped", "start_at_login": False})
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


def operate(config_path: Path, state: Path, action: str, *, wheel: Path | None = None,
            requirements: Path | None = None, workspace: str = "", path: str = "",
            candidate_config: Path | None = None, reset_recovery: bool = False,
            require_safe: bool = False, commit: str | None = None) -> dict:
    if sys.platform != "darwin" or os.getuid() == 0:
        raise Fault("unsupported_service_host", "Managed services require a logged-in macOS user. Linux manual serve and SSH workers remain separate.")
    if not config_path.is_absolute() or not state.is_absolute():
        raise Fault("invalid_service_path", "Use absolute configuration and state paths for a managed service.")
    if reset_recovery and action != "restart":
        raise Fault("invalid_service_request", "Explicit recovery renewal requires service restart --reset-recovery.")
    if commit is not None and (action != "update" or wheel is not None or requirements is not None):
        raise Fault("invalid_service_request", "Commit updates cannot be combined with local wheel or requirements inputs.")
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
            return _register(config_path, state, identity, workspace, path)
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
            return _activate(state, record, target, require_safe=require_safe)
        if record["pending"] is not None:
            if action == "restart" and not reset_recovery and _task_cutover(state, record):
                return _resume_activation(state, record)
            raise Fault("activation_pending", "Read the original transition; explicitly resume a verified Task cutover or restore a compatible prior installation.")
        if action == "update":
            if commit is None and (wheel is None or requirements is None):
                raise Fault("invalid_service_request", "Update requires an exact commit or a trusted local wheel and pinned requirements.")
            selected_config = candidate_config.resolve(strict=True) if candidate_config is not None else config_path
            selected_config = service_state._saved_configuration(state, service_state._snapshot_configuration(state, selected_config))
            target = (install._prepare_commit(state, commit, selected_config) if commit is not None
                      else install._prepare(state, wheel, requirements, selected_config))
            if target == record["current"]:
                if selected_config.read_bytes() != config_path.read_bytes():
                    raise Fault("same_installation", "Configuration changes require an explicit distinct candidate installation.")
                return runtime.status(config_path, state)
            return _activate(state, record, target, selected_config, require_safe=require_safe)
        with lease(state / "locks/lifecycle.lock"):
            record = service_state._record(state)
            if action in ("start", "restart"):
                checks._candidate_state(service_state._runtime(state, record["current"]) / "bin/python", config_path, state)
            if action == "start" and service_state.stopped(record):
                record = dict(record, policy=record["policy"] | {"desired": "running"})
                save_json(root / "service.json", record)
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
