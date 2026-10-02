"""Actual state readers, candidate compatibility and bounded readiness checks."""

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from openmachine import __version__, execution, tasks, workspace
from openmachine.config import load
from openmachine.foundation.sanitize import Fault

from . import state as service_state
from .state import PROTOCOL, READINESS_SECONDS, STATE_CONTRACT


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
    return {**changes, **task_state, **_ssh_recovery_state(state), **service_state._recovery_state(state), **_events_state(state), **_connections_state(state), "version": __version__, "protocol": PROTOCOL, "state_contract": STATE_CONTRACT,
            "prefix": str(Path(sys.prefix).resolve()), "execution_records": count,
            "configuration_sha256": config.configuration_sha256()}


def _connections_state(state: Path) -> dict:
    from integrations.connections import check_state
    return check_state(state)


def _events_state(state: Path) -> dict:
    if os.path.lexists(state / "events.json") or os.path.lexists(state / "locks/events.lock"):
        from integrations.events import check_state as check_events
        return check_events(state)
    return {"events_present": False, "events_format": 1, "events_subscriptions": 0, "events_sha256": None}


def _ssh_recovery_state(state: Path) -> dict:
    # Import only for retained integration state, including when SSH was disabled
    # later. The connection owner validates its own history; service never edits it.
    if os.path.lexists(state / "ssh-recovery.json") or os.path.lexists(state / "locks/ssh-recovery.lock"):
        from integrations.ssh.recovery import check_state
        return check_state(state)
    return {"ssh_recovery_present": False, "ssh_recovery_format": 1,
            "ssh_recovery_targets": 0, "ssh_recovery_sha256": None}


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
                or value["state_contract"] not in range(1, STATE_CONTRACT + 1)
                or (expected_contract is not None and value["state_contract"] != expected_contract)):
            raise ValueError("incompatible candidate")
        execution.check_workspace_leases(load(config_path), state, legacy_candidate=value["state_contract"] < 4)
        connections = _connections_state(state)
        if connections["connections_present"] and (value["state_contract"] < 10 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in connections.items())):
            raise ValueError("candidate ignores retained connection authority")
        events = _events_state(state)
        if events["events_present"] and (value["state_contract"] < 9 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in events.items())):
            raise ValueError("candidate ignores or changes retained Events delivery budgets")
        recovery = _ssh_recovery_state(state)
        if recovery["ssh_recovery_present"] and (value["state_contract"] < 7 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in recovery.items())):
            raise ValueError("candidate ignores or changes retained SSH observation budgets")
        runtime_recovery = service_state._recovery_state(state)
        if runtime_recovery["runtime_recovery_present"] and (value["state_contract"] < 8 or any(
                type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in runtime_recovery.items())):
            raise ValueError("candidate ignores or changes retained runtime recovery budgets")
        retained_tasks = tasks.check_state(state)
        if retained_tasks["task_index_format"] == 2:
            formats = value.get("structured_task_read_formats")
            if (value["state_contract"] < 6 or not isinstance(formats, list) or not formats
                    or any(type(item) is not int or item not in tasks.READ_FORMATS for item in formats)
                    or len(formats) != len(set(formats)) or not ({1, 2} if connections["connections_present"] else {1}) <= set(formats)
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
