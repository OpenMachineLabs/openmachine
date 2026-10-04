"""Owned macOS service startup, status and bounded MCP supervision."""

import errno
import hashlib
import json
import os
from pathlib import Path
import plistlib
import random
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease

from . import state as service_state
from . import checks
from .state import HEALTH_INTERVAL_SECONDS, LAUNCHER, READINESS_SECONDS, RECOVERY_ATTEMPTS, RECOVERY_MS


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
    service_state._save_recovery(state, episode)
    return episode


def _recovery_delay(attempts: int) -> int:
    return int((1000 if attempts == 1 else 2000 if attempts == 2 else 60000) * random.uniform(0.8, 1.2))


def _label(state: Path) -> str:
    return "co.openmachine.runtime." + hashlib.sha256(str(state).encode()).hexdigest()[:20]


def _plist_path(state: Path) -> Path:
    return Path.home() / "Library/LaunchAgents" / (_label(state) + ".plist")


def _definition(state: Path, record: dict) -> dict:
    installed = service_state._installed(state, record["current"])
    environment = record["path_environment"]
    if installed["state_contract"] >= 14 and installed.get("payload_sha256"):
        retained = state / "service/payloads" / installed["payload_sha256"]
        environment = os.pathsep.join((str(retained / "bin"), str(retained / "git/bin"), os.defpath))
    arguments = [record["launch_python"], "-I", str(state / "service/launch.py")]
    if record["format"] == 2:
        arguments = [record["management"]["launch_python"], "-I", str(state / "service/manage.py"), "--launch"]
    return {"Label": _label(state), "ProgramArguments": arguments,
            "WorkingDirectory": str(state), "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10, "ExitTimeOut": 10,
            "AbandonProcessGroup": installed["state_contract"] < 8,
            "EnvironmentVariables": {"PATH": environment},
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
        definitions = [_definition(state, dict(record, current=identity)) for identity in selections]
        if record["format"] == 2:
            # A policy commit may precede native definition publication. Only
            # the exact former definition is admissible, and its old launcher
            # rejects format 2 without ever starting the stopped service.
            definitions.extend(_definition(state, dict(record, current=identity, format=1)) for identity in selections)
        if (path.read_bytes() not in [plistlib.dumps(item) for item in definitions]
                or (state / "service/launch.py").read_text() != LAUNCHER):
            raise ValueError("service definition changed")
        if "management" in record:
            anchor = service_state._installed(state, record["management"]["identity"])
            if anchor.get("management_contract") != service_state.MANAGEMENT_CONTRACT or not Path(record["management"]["launch_python"]).is_file():
                raise ValueError("retained management identity unavailable")
            entry = state / "service/manage.py"
            info = entry.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_nlink != 1 or entry.read_bytes() != service_state.management_entry(state, record)):
                raise ValueError("management entry changed")
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        raise Fault("service_definition_mismatch", "The installed launcher or LaunchAgent differs; do not overwrite or control another service.") from error


def _startup(state: Path) -> dict:
    result = checks._run(["/bin/launchctl", "print-disabled", f"gui/{os.getuid()}"])
    if result.returncode:
        raise Fault("startup_unknown", "The effective user-service startup registration could not be observed.")
    match = re.search(r'"' + re.escape(_label(state)) + r'"\s*=>\s*(true|false)', result.stdout)
    # An omitted override uses this verified RunAtLoad LaunchAgent's default.
    if "disabled services = {" not in result.stdout:
        raise Fault("startup_unknown", "The native startup registration reply was not recognized.")
    return {"enabled": match is None or match[1] == "false", "source": "launchctl",
            "observed_at_ms": time.time_ns() // 1000000}


def _set_startup(state: Path, enabled: bool) -> dict:
    result = checks._run(["/bin/launchctl", "enable" if enabled else "disable", f"gui/{os.getuid()}/{_label(state)}"])
    observed = _startup(state)
    if result.returncode or observed["enabled"] != enabled:
        raise Fault("startup_denied", "The requested startup registration was not verified; read effective OS policy.")
    return observed


def _sync_definition(state: Path, record: dict) -> None:
    _verify_files(state, record)
    path = _plist_path(state)
    desired = plistlib.dumps(_definition(state, record))
    if path.read_bytes() == desired:
        return
    fd, temporary = tempfile.mkstemp(prefix=".openmachine-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _native(state: Path) -> dict:
    result = checks._run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{_label(state)}"])
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


def socket_alive(path: Path) -> bool:
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
        checks._run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{_label(state)}"])
    deadline = time.monotonic() + 15
    while True:
        if not _native(state)["loaded"] and not socket_alive(state / "mcp.sock"):
            return
        if time.monotonic() >= deadline:
            raise Fault("service_stop_unverified", "Service termination is unverified; no candidate was started or worker cancelled.")
        time.sleep(0.1)


def _start(state: Path, record: dict) -> dict:
    if service_state.stopped(record):
        raise Fault("runtime_stopped", "Explicit Start is required before launching a deliberately stopped runtime.")
    if record["pending"] is None and service_state._installed(state, record["current"])["state_contract"] >= 8:
        if service_state._read_recovery(state) is None:
            service_state._save_recovery(state, None)
    observed = _native(state)
    if record["format"] == 2 and observed["loaded"] and observed["pid"] is None:
        # A successful stopped-intent login leaves an idle registered job.
        # Reload its verified definition, including any disabled startup policy.
        _stop(state)
        observed = _native(state)
    if not observed["loaded"]:
        # Only verified transition definitions are accepted; write the selected
        # profile after bootout. Interrupted transitions retain both identities.
        _sync_definition(state, record)
        disabled = record["format"] == 2 and not _startup(state)["enabled"]
        try:
            if disabled:
                _set_startup(state, True)
            checks._run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(_plist_path(state))])
        finally:
            if disabled:
                _set_startup(state, False)
    elif observed["pid"] is None:
        checks._run(["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{_label(state)}"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observed = _native(state)
        if record["pending"] is None:
            recovery = service_state._read_recovery(state)
            if recovery and recovery["episode"]:
                if recovery["episode"]["reason"].startswith("blocked_"):
                    raise Fault("runtime_recovery_blocked", "The persisted recovery budget is blocked; inspect service status.")
                # Only the owner may advance an active recovery episode. An
                # explicit start observes it, including its original cooldown.
                time.sleep(0.1)
                continue
        if observed["pid"] is not None:
            try:
                if service_state._installed(state, record["current"])["state_contract"] >= 8:
                    with lease(state / "service/readiness.lock"):
                        return checks._managed_probe(state, record, observed["pid"])
                return checks.probe_service(state, Path(record["config"]), record["probe"],
                                     service_state._installed(state, record["current"])["version"], observed["pid"],
                                     expected_contract=service_state._installed(state, record["current"])["state_contract"])
            except Fault as error:
                if error.code not in {"service_unready", "resource_busy"}:
                    raise
        time.sleep(0.1)
    raise Fault("service_unready", "The managed service did not pass MCP/file readiness; inspect its last exit and OS permissions.")


def _launch_mcp(state: Path, record: dict) -> subprocess.Popen:
    return subprocess.Popen([str(service_state._runtime(state, record["current"]) / "bin/python"), "-I", "-m", "openmachine.cli",
                             "--config", record["config"], "--state-dir", str(state), "serve", "--managed-child"],
                            cwd=state, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


def _publish_recovery(state: Path, record: dict, episode: dict | None, stop: threading.Event) -> bool:
    while not stop.is_set():
        try:
            with lease(state / "locks/lifecycle.lock", shared=True):
                current = service_state._record(state)
                if current["current"] != record["current"] or current["pending"] is not None or service_state.stopped(current):
                    return False
                service_state._save_recovery(state, episode)
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
                checks._managed_probe(state, record, os.getpid(), child=child.pid)
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
        record = service_state._record(state)
        _verify_files(state, record)
        if service_state.stopped(record):
            return
        if (str(config_path.resolve()) != record["config"] or _native(state)["pid"] != os.getpid()
                or Path(sys.prefix).resolve() != service_state._runtime(state, record["current"])
                or os.getpgrp() != os.getpid()):
            raise Fault("service_identity_mismatch", "Only the selected launchd job may supervise this MCP runtime.")
        # Activation owns one candidate start and its readiness proof. Defer
        # recovery state until commit so a failed first upgrade can restore v7.
        if record["pending"] is not None:
            child = _launch_mcp(state, record)
            while not stop.is_set() and service_state._record(state)["pending"] is not None:
                if child.poll() is not None:
                    return
                stop.wait(0.05)
        if stop.is_set():
            return
        if service_state._read_recovery(state) is None:
            raise Fault("runtime_recovery_invalid", "The service operation has not initialized its recovery record.")
        with lease(state / "service/recovery.lock"):
            while not stop.is_set():
                episode = service_state._read_recovery(state)["episode"]
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
                            current = service_state._record(state)
                            if current["current"] != record["current"] or current["pending"] is not None or service_state.stopped(current):
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
                        checks._managed_probe(state, record, os.getpid(), child=child.pid, timeout_seconds=deadline - time.monotonic())
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
                                    current = service_state._record(state)
                                    if current["current"] != record["current"] or current["pending"] is not None or service_state.stopped(current):
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


def status(config_path: Path, state: Path) -> dict:
    if not os.path.lexists(state / "service/service.json"):
        if os.path.lexists(state / "service/launch.py") or os.path.lexists(_plist_path(state)):
            raise Fault("service_state_invalid", "An incomplete installation exists; preserve its files and inspect the original setup before retrying.")
        return {"status": "not_installed", "tunnel": "not_probed", "chatgpt": "not_probed"}
    record = service_state._record(state)
    if str(config_path.resolve()) != record["config"]:
        raise Fault("service_configuration_mismatch", "Use the configuration registered with this state directory.")
    _verify_files(state, record)
    observed = _native(state)
    result = {"status": "unready" if observed["loaded"] else "stopped", **observed, "current": record["current"], "previous": record["previous"],
              "pending": record["pending"], "version": service_state._installed(state, record["current"])["version"],
              "tunnel": "not_probed", "chatgpt": "not_probed"}
    recovery = service_state._read_recovery(state)
    result["recovery"] = recovery["episode"] if recovery else None
    managed = service_state._installed(state, record["current"])["state_contract"] >= 8
    if managed:
        result.update(supervisor_pid=observed["pid"], pid=None)
    if recovery and recovery["episode"] and observed["loaded"]:
        result["status"] = "blocked" if recovery["episode"]["reason"].startswith("blocked_") else "recovering"
    elif observed["pid"] is not None:
        try:
            if managed:
                with lease(state / "service/readiness.lock"):
                    result.update(checks._managed_probe(state, record, observed["pid"]))
            else:
                result.update(checks.probe_service(state, config_path, record["probe"], result["version"], observed["pid"],
                                            expected_contract=service_state._installed(state, record["current"])["state_contract"]))
        except Fault as error:
            result.update(status="recovering" if error.code == "resource_busy" else "unready", error=error.code)
    if record["pending"] is not None:
        result["status"] = "activation_pending"
    return result
