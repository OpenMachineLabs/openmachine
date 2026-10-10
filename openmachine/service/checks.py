"""Actual state readers, candidate compatibility and bounded readiness checks."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from openmachine import __version__, execution, tasks, workspace
from openmachine.config import load
from openmachine.foundation.sanitize import Fault, PRE_EFFECT_REQUEST_FAILURES
from openmachine.foundation.state import lease
from openmachine.tasks import records as task_records

from . import processes, state as service_state
from .state import PROTOCOL, READINESS_SECONDS, STATE_CONTRACT


def check_state(config_path: Path, state: Path) -> dict:
    """Use the candidate's actual readers; never rewrite existing persistent data."""
    config = load(config_path)
    executions = _execution_state(state)
    task_state = tasks.check_state(state)
    execution.check_workspace_leases(config, state)
    changes = workspace.check_change_state(state)
    return {**executions, **changes, **task_state, **_ssh_recovery_state(state), **service_state._recovery_state(state), **service_state._policy_state(state), **_events_state(state), **_connections_state(state), "version": __version__, "protocol": PROTOCOL, "state_contract": STATE_CONTRACT,
            "management_contract": service_state.MANAGEMENT_CONTRACT, "sqlite_runtime": tasks.sqlite_runtime(),
            "prefix": str(Path(sys.prefix).resolve()), "configuration_sha256": config.configuration_sha256()}


def _execution_state(state: Path) -> dict:
    """Bind compatible readers to retained original-ID reconciliation outcomes."""
    count = reconciliations = 0
    fingerprint = hashlib.sha256()
    root = state / "executions"
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise Fault("corrupt_state", "The execution store is not an ordinary directory.")
        for directory in sorted(root.iterdir(), key=lambda path: path.name):
            if not re.fullmatch(r"[0-9a-f]{32}", directory.name) or directory.is_symlink() or not directory.is_dir():
                raise Fault("corrupt_state", "An execution-store entry is invalid; preserve it before changing versions.")
            record = execution.read_record(directory)
            if record.get("cleanup", {}).get("source") == "native_reconciliation":
                reconciliations += 1
                fingerprint.update(directory.name.encode())
                fingerprint.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
            count += 1
    return {"execution_records": count, "execution_reconciliations": reconciliations,
            "execution_reconciliation_sha256": fingerprint.hexdigest() if reconciliations else None}


def _connections_state(state: Path) -> dict:
    from integrations.connections import check_state
    return check_state(state)


def _events_state(state: Path) -> dict:
    """Fingerprint a retired private archive; never load callbacks as live work."""
    path, lock = state / "events.json", state / "locks/events.lock"
    absent = {"events_present": False, "events_format": 1, "events_subscriptions": 0,
              "events_sha256": None, "events_retired": True}
    if not os.path.lexists(path) and not os.path.lexists(lock):
        return absent

    def private(path, directory=False):
        info = path.lstat()
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("unsafe retired archive")
        return info

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate archive key")
            result[key] = value
        return result

    try:
        private(state, True)
        private(lock.parent, True)
        private(lock)
        with lease(lock, shared=True, create=False):
            before = private(path)
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
                info = os.fstat(stream.fileno())
                if ((info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                        or info.st_size > 524288):
                    raise ValueError("archive identity or size changed")
                raw = stream.read(524289)
            if len(raw) > 524288:
                raise ValueError("archive exceeds capacity")
            value = json.loads(raw, object_pairs_hook=pairs)
            if (not isinstance(value, dict) or set(value) != {"format", "subscriptions", "connections"}
                    or type(value["format"]) is not int or value["format"] != 1
                    or any(not isinstance(value[key], dict) or len(value[key]) > 64
                           for key in ("subscriptions", "connections"))):
                raise ValueError("unsupported retired archive envelope")
            # Match the former reader's canonical fingerprint for original writer
            # records. Nested delivery data stays opaque and is never scheduled.
            fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True,
                separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
            return {**absent, "events_present": True,
                    "events_subscriptions": len(value["subscriptions"]), "events_sha256": fingerprint}
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise Fault("events_corrupt", "Retired Events history is missing, unsafe or corrupt; preserve it without activation.") from error


def _ssh_recovery_state(state: Path) -> dict:
    # Import only for retained integration state, including when SSH was disabled
    # later. The connection owner validates its own history; service never edits it.
    if os.path.lexists(state / "ssh-recovery.json") or os.path.lexists(state / "locks/ssh-recovery.lock"):
        from integrations.ssh.recovery import check_state
        return check_state(state)
    return {"ssh_recovery_present": False, "ssh_recovery_format": 1,
            "ssh_recovery_targets": 0, "ssh_recovery_sha256": None}


def machine_unused(config, state: Path, name: str, *, check_remote: bool = True) -> None:
    """Never strand a configured workspace, task reference or remote receipt."""
    from integrations.ssh import client, recovery
    target = config.machine(name)
    if any(item.machine == name for item in config.workspaces.values()):
        raise Fault("machine_in_use", "A configured workspace still references this machine.")
    history = tasks.check_state(state)
    if history["task_records"] != history["structured_task_records"]:
        raise Fault("machine_in_use", "Legacy task history cannot prove that this target is unused; preserve the connection.")
    if tasks.references_target(state, name, target.ssh_identity):
        raise Fault("machine_in_use", "A retained task still references this machine.")
    if target.ssh_identity in recovery.read(state)["targets"]:
        raise Fault("machine_in_use", "Retained SSH observation history still references this identity.")
    if not target.ssh_identity:
        raise Fault("machine_in_use", "An unpinned target cannot prove that remote history is absent; preserve its connection.")
    if check_remote:
        # Turning grants off does not erase earlier work outside controller tasks.
        probe = client.call(config, name, "probe")
        records = client.call(config, name, "list_executions", limit=1)
        if (type(probe.get("workspace_change_records")) is not int or probe["workspace_change_records"] != 0
                or records.get("executions") != [] or records.get("unreadable") != []
                or records.get("next_cursor", "unknown") is not None):
            raise Fault("machine_in_use", "The remote target retains original work; preserve its connection.")


_HISTORICAL_CHANGE_NO_EFFECT = frozenset(
    f"Domain failure: {code}." for code in PRE_EFFECT_REQUEST_FAILURES)


def _task_operation_blocks_activation(operation: dict, historical: dict | None = None) -> bool:
    result = operation.get("result")
    pending = (operation.get("state") != "observed" or result is None
               or result.get("status") in {"running", "unknown"})
    if not pending or result is None or result.get("status") != "unknown":
        return pending
    if operation.get("state") != "unknown":
        return True
    locator = operation.get("locator") or {}
    if (locator.get("kind") == "execution" and locator.get("control_seq") is None
            and locator.get("target_identity") is None and result.get("receipt_sha256")
            and (historical or {}).get(locator.get("execution_id")) ==
                (locator.get("machine"), result["receipt_sha256"])):
        # Replacing the controller never resolves or repeats original work.
        return False
    if locator.get("kind") == "github":
        # Provider uncertainty remains retained; switching a compatible local
        # runtime neither replays nor cancels that external operation.
        return False
    return not (locator.get("kind") == "change" and result.get("receipt_sha256") is None
                and result.get("summary") in _HISTORICAL_CHANGE_NO_EFFECT)


def activation_safe(state: Path, *, config=None, guards=None) -> None:
    """Admit controller replacement without resolving old work or releasing guards."""
    historical, unresolved = {}, []
    root = state / "executions"
    if root.exists():
        for directory in root.iterdir():
            record = execution.read_record(directory)
            if record.get("cleanup", {}).get("source") == "native_reconciliation":
                original = execution.read_record(directory, original_reconciled=True)
                historical[directory.name] = (original["request"]["machine"],
                    task_records.execution_receipt_digest(original | {"execution_id": directory.name}))
            if (record["status"] not in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
                    or record.get("cleanup", {}).get("status", "verified") != "verified"
                    or ((record.get("control") or {}).get("last") or {}).get("status") in {"pending", "partial", "outcome_unknown"}):
                if config is None or guards is None:
                    raise Fault("activation_deferred", "An original execution is active or unresolved; preserve it and continue this update explicitly later.")
                unresolved.append(directory.name)
    if unresolved:
        try:
            records = guards.enter_context(execution.activation_guard(config, state, unresolved))
        except (Fault, OSError, ValueError) as error:
            raise Fault("activation_deferred", "Original execution command, control or resource ownership remains unverified; no service was stopped.") from error
        for identity, record in records.items():
            historical[identity] = (record["request"]["machine"],
                task_records.execution_receipt_digest(record | {"execution_id": identity}))
    try:
        # Exhaust the owning iterator so Task storage readers close before cutover.
        blocked = False
        for operation in tasks.activation_blockers(state):
            blocked |= _task_operation_blocks_activation(operation, historical)
    except Fault as error:
        raise Fault("activation_deferred", "Task history is not fully readable; activation remains unperformed.") from error
    if blocked:
        raise Fault("activation_deferred", "An original task operation is active or unresolved; no worker was stopped.")
    # The owning readers validate their records before classifying in-flight
    # writes or authentication. No service-owned copy of either schema exists.
    from integrations.connections import check_state as connections_state
    workspace.check_change_state(state, require_idle=True)
    connections_state(state, require_idle=True)


def _change_support(value: dict) -> set[int]:
    """Interpret an actual reader's coverage, including W1/W2's format-1 report."""
    latest = value.get("workspace_change_format")
    formats = value.get("workspace_change_read_formats", [latest])
    if (type(latest) is not int or latest not in workspace.READ_CHANGE_FORMATS or
        not isinstance(formats, list) or not formats or len(set(formats)) != len(formats) or
        any(type(item) is not int or item not in workspace.READ_CHANGE_FORMATS for item in formats) or latest not in formats):
        raise ValueError("missing or unsupported actual change reader")
    return set(formats)


class _TaskGenerationMoved(Exception):
    """The candidate and local readers observed different Task generations."""


def _candidate_state(python: Path, config_path: Path, state: Path, expected_contract: int | None = None,
                     *, env: dict[str, str] | None = None) -> dict:
    # Only resample a provably changing Task generation, before activation.
    # Every attempt repeats the complete candidate/retained-state verification.
    for attempt in range(3):
        try:
            return _candidate_state_once(python, config_path, state, expected_contract, env=env)
        except _TaskGenerationMoved as error:
            if attempt == 2:
                raise Fault("incompatible_state",
                            "Task history kept changing during candidate validation; activation remains unverified.") from error
            time.sleep(0.05)


def _candidate_state_once(python: Path, config_path: Path, state: Path, expected_contract: int | None = None,
                          *, env: dict[str, str] | None = None) -> dict:
    result = processes.run([str(python), "-I", "-m", "openmachine.service", "check-state", str(config_path), str(state)],
                  **({"env": env} if env is not None else {}))
    try:
        value = json.loads(result.stdout)
        if (result.returncode or value["protocol"] != PROTOCOL or type(value["state_contract"]) is not int
                or value["state_contract"] not in range(1, STATE_CONTRACT + 1)
                or (expected_contract is not None and value["state_contract"] != expected_contract)):
            raise ValueError("incompatible candidate")
        execution.check_workspace_leases(load(config_path), state, legacy_candidate=value["state_contract"] < 4)
        executions = _execution_state(state)
        if executions["execution_reconciliations"] and (value["state_contract"] < 20 or any(
                type(value.get(key)) is not type(executions[key]) or value[key] != executions[key]
                for key in ("execution_reconciliations", "execution_reconciliation_sha256"))):
            raise ValueError("candidate ignores or changes retained execution reconciliation")
        connections = _connections_state(state)
        required_connection_contract = 11 if connections["connections_format"] >= 2 else 10
        if connections["connections_present"] and (value["state_contract"] < required_connection_contract or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in connections.items())):
            raise ValueError("candidate ignores retained connection authority")
        events = _events_state(state)
        if events["events_present"] and (value["state_contract"] < 9 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in events.items())):
            raise ValueError("candidate cannot preserve retired Events history without reactivating delivery")
        recovery = _ssh_recovery_state(state)
        if recovery["ssh_recovery_present"] and (value["state_contract"] < 7 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in recovery.items())):
            raise ValueError("candidate ignores or changes retained SSH observation budgets")
        runtime_recovery = service_state._recovery_state(state)
        if runtime_recovery["runtime_recovery_present"] and (value["state_contract"] < 8 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in runtime_recovery.items())):
            raise ValueError("candidate ignores or changes retained runtime recovery budgets")
        policy = service_state._policy_state(state)
        if policy["service_policy_present"] and (value["state_contract"] < 14 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in policy.items())):
            raise ValueError("candidate ignores persistent stopped or startup intent")
        retained_tasks = tasks.check_state(state)
        storage = retained_tasks["task_storage"]
        if storage["kind"] == "sqlite" or value["state_contract"] >= 18:
            candidate_storage = value.get("task_storage")
            engine = value.get("sqlite_runtime")
            if (value["state_contract"] < 18 or not isinstance(candidate_storage, dict)
                    or not isinstance(engine, dict) or engine.get("supported") is not True
                    or any(type(candidate_storage.get(key)) is not type(storage[key])
                           or candidate_storage[key] != storage[key]
                           for key in storage if key != "generation")):
                raise ValueError("candidate ignores the authoritative Task ledger or its SQLite engine")
            if type(candidate_storage.get("generation")) is not type(storage["generation"]):
                raise ValueError("missing Task ledger generation")
            if candidate_storage["generation"] != storage["generation"]:
                raise _TaskGenerationMoved()
        if (retained_tasks["task_index_format"] == 2
                and type(value.get("task_index_generation")) is int
                and value["task_index_generation"] != retained_tasks["task_index_generation"]):
            raise _TaskGenerationMoved()
        retained_formats = retained_tasks["structured_task_formats_present"]
        if any(item >= 3 for item in retained_formats):
            minimum_contract = 16 if 4 in retained_formats else 15
            present = value.get("structured_task_formats_present")
            if (value["state_contract"] < minimum_contract or not isinstance(present, list)
                    or any(type(item) is not int for item in present) or present != retained_formats):
                raise ValueError("candidate ignores retained task recovery or scoped admission semantics")
        if retained_tasks["task_index_format"] == 2 or storage["kind"] == "sqlite":
            formats = value.get("structured_task_read_formats")
            if (value["state_contract"] < 6 or not isinstance(formats, list) or not formats
                    or any(type(item) is not int or item not in tasks.READ_FORMATS for item in formats)
                    or len(formats) != len(set(formats))
                    or not (set(retained_formats) | ({1, 2} if connections["connections_present"] else {1})) <= set(formats)
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
        raise Fault("incompatible_state", "The candidate cannot validate current configuration and records; preserve the selected Task authority.") from error


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
    installed = service_state._installed(state, record["current"])
    # Callers hold the single readiness slot. The OS job PID is the parent,
    # not the MCP PID; a matching version/config alone cannot prove ownership.
    ready = probe_service(state, Path(record["config"]), record["probe"], installed["version"], child,
                          expected_contract=installed["state_contract"], timeout_seconds=timeout_seconds)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Fault("service_unready", "The MCP readiness attempt exceeded its deadline.")
    if type(ready["pid"]) is not int or ready["pid"] <= 0:
        raise Fault("service_identity_mismatch", "The MCP process identity is invalid.")
    relationship = processes.run(["/bin/ps", "-o", "ppid=", "-p", str(ready["pid"])], timeout=min(1, remaining))
    if relationship.returncode or relationship.stdout.strip() != str(parent):
        raise Fault("service_identity_mismatch", "The MCP process is not a child of the selected managed service.")
    return {**ready, "supervisor_pid": parent}


def _remote_workspace_state(python: Path, config_path: Path, name: str) -> dict:
    """Ask that exact installed controller and pinned endpoint to validate target history."""
    target = load(config_path).machines[name]
    result = processes.run([str(python), "-I", "-m", "openmachine.cli", "--config", str(config_path),
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
