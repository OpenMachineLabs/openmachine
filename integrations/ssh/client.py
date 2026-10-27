"""Bounded controller-side SSH requests; no task store or target implementation."""

import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import time
import threading
import tempfile
from typing import TYPE_CHECKING

from openmachine import __version__
from openmachine.foundation.sanitize import Fault

from . import MAX_MESSAGE, PROTOCOL

if TYPE_CHECKING:
    from openmachine.config import Settings

MUTATIONS = {"apply_workspace_patch", "start_execution", "create_worktree", "write_workspace_file", "edit_workspace_file", "cancel_execution", "control_execution"}


@contextmanager
def connection_args(alias: str | None, connection=None):
    """Use the system alias or exactly one pinned direct route, never trust on first use."""
    argv = ["ssh", "-T", "-a", "-x", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "LogLevel=ERROR"]
    if connection is None:
        yield [*argv, "--", alias]
        return
    # The trusted public key is part of Core configuration. A private temporary
    # known_hosts file avoids modifying the operator's SSH configuration/trust.
    with tempfile.TemporaryDirectory(prefix="om-ssh-") as directory:
        known = Path(directory) / "known_hosts"
        host = connection.host if connection.port == 22 else f"[{connection.host}]:{connection.port}"
        known.write_text(f"{host} {connection.host_key}\n")
        os.chmod(known, 0o600)
        argv.extend(["-F", "/dev/null", "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=/dev/null",
                     "-o", "UpdateHostKeys=no", "-p", str(connection.port), "-l", connection.user])
        if connection.identity_file:
            argv.extend(["-o", "IdentitiesOnly=yes", "-i", connection.identity_file])
        yield [*argv, "--", connection.host]


def _exchange(argv: list[str], payload: bytes, *, cancelled: threading.Event | None = None) -> tuple[int, bytes]:
    """Bound stdin, both output streams and elapsed time without a shell or replay."""
    if len(payload) > MAX_MESSAGE:
        raise Fault("request_limit", "Narrow the request to at most one MiB of encoded JSON.")
    output, diagnostic = bytearray(), bytearray()
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0, env=os.environ | {"LC_ALL": "C"}) as process:
        try:
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdin, process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, None)
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, diagnostic)
                offset = 0
                deadline = time.monotonic() + 30
                while selector.get_map() or process.poll() is None:
                    if cancelled is not None and cancelled.is_set():
                        raise Fault("observation_cancelled", "The SSH observation was cancelled; the original worker is unchanged.")
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    for key, _ in selector.select(0.05):
                        if key.fileobj is process.stdin:
                            try:
                                offset += os.write(key.fd, payload[offset:offset + 65536])
                            except BrokenPipeError:
                                # Early SSH refusal still owns bounded diagnostics
                                # needed to distinguish transport from identity faults.
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                                continue
                            if offset == len(payload):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                        else:
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            key.data.extend(chunk)
                            if len(key.data) > (MAX_MESSAGE if key.data is output else 8192):
                                raise Fault("response_limit", "SSH output exceeded its bound; no complete response was verified.")
                code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
                if code == 255:
                    # Inspect bounded diagnostics locally; never retain or return
                    # hostnames, account details or raw authentication messages.
                    detail = bytes(diagnostic).lower()
                    if any(item in detail for item in (b"host key verification failed", b"host identification has changed", b"offending ")):
                        raise Fault("ssh_host_key_mismatch", "SSH host-key verification failed; automatic recovery is stopped.")
                    if any(item in detail for item in (b"permission denied", b"authentication failed", b"no supported authentication", b"too many authentication failures")):
                        raise Fault("ssh_authentication_required", "SSH authentication was refused; automatic recovery is stopped.")
                    if any(item in detail for item in (b"connection timed out", b"connection refused", b"no route to host",
                            b"network is unreachable", b"connection reset by peer", b"connection closed by remote host",
                            b"temporary failure in name resolution", b"operation timed out")) or re.search(
                            rb"(?:connection to [^\r\n]+ closed by remote host\.?|connection closed by [^\r\n]+ port [0-9]+)\s*$",
                            detail, re.MULTILINE):
                        raise Fault("ssh_unavailable", "The original SSH connection is temporarily unavailable.", {"ssh_retryable": True})
                return code, bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def call(config: "Settings", machine: str, operation: str, *,
         cancelled: threading.Event | None = None, **arguments) -> dict:
    if cancelled is not None and operation not in {"read_execution", "read_workspace_change"}:
        raise Fault("invalid_request", "Observer cancellation is only valid for original receipt reads.")
    if cancelled is not None and cancelled.is_set():
        raise Fault("observation_cancelled", "The observation was cancelled before contacting the target.")
    target = config.machine(machine, write=(operation == "start_execution"
                                              or operation == "control_execution" and arguments.get("action") in {"write_stdin", "resize"}))
    if target.transport != "ssh":
        raise Fault("not_ssh", "Select a configured SSH target, or omit the explicit doctor target.")
    if not target.ssh_identity and operation != "probe":
        raise Fault("identity_required", "Probe this target and explicitly pin its ssh_identity before workspace access.")
    request = {"protocol": PROTOCOL, "version": __version__, "identity": target.ssh_identity,
               "execution_enabled": target.execution_enabled,
               "workspace_write_enabled": target.execution_enabled if target.workspace_write_enabled is None else target.workspace_write_enabled,
               "operation": operation, "arguments": arguments,
               "workspaces": {name: item.root for name, item in config.workspaces.items() if item.machine == machine}}
    mutating = operation in MUTATIONS and not (operation in {"edit_workspace_file", "apply_workspace_patch"} and arguments.get("dry_run") is True)
    identity_evidence = {"machine": machine, "change_id": arguments["change_id"]} if arguments.get("change_id") else None
    try:
        with connection_args(target.ssh_alias, target.ssh_connection) as argv:
            code, raw = _exchange([*argv, shlex.join(["python3", "-I", target.ssh_worker, "rpc"])],
                                  json.dumps(request, ensure_ascii=True).encode(), cancelled=cancelled)
    except FileNotFoundError as error:
        raise Fault("ssh_missing", "Install the system OpenSSH client; no target was contacted.") from error
    except (OSError, TimeoutError, subprocess.SubprocessError, Fault) as error:
        if isinstance(error, Fault) and (error.code == "request_limit" or not mutating):
            raise
        evidence = identity_evidence
        if not mutating and isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            evidence = {"ssh_retryable": True}
        raise Fault("outcome_unknown" if mutating else "ssh_unavailable",
                    "No complete SSH response was verified. Read the original execution/change before any mutation retry.", evidence) from error
    try:
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise ValueError
    except (ValueError, UnicodeError) as error:
        reason = "ssh_unavailable" if code == 255 else "remote_runtime_unavailable" if code == 2 else "ssh_protocol_error"
        raise Fault("outcome_unknown" if mutating else reason,
                    "Check the existing SSH identity, Python 3.12+, and the configured worker archive. No result was verified.", identity_evidence) from error
    if "error" in response:
        error = response["error"]
        if isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
            raise Fault(error["code"], error["message"], {key: value for key, value in response.items() if key != "error"} | (identity_evidence or {}))
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The remote error response was invalid.", identity_evidence)
    if (code or response.get("protocol") != PROTOCOL or response.get("version") != __version__
            or response.get("worker_sha256") != Path(target.ssh_worker).stem
            or not isinstance(response.get("identity"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", response["identity"])
            or (target.ssh_identity and response["identity"] != target.ssh_identity)
            or not isinstance(response.get("result"), dict)):
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The remote response identity was not verified.", identity_evidence)
    result = response["result"]
    if operation == "inspect_environment" and (
            type(result.get("observation_version")) is not int or result["observation_version"] != 1
            or result.get("section") != arguments.get("section") or result.get("scope") != "worker_endpoint"
            or any(type(result.get(key)) is not int for key in ("started_at_ms", "finished_at_ms"))
            or not isinstance(result.get("context"), dict)
            or not isinstance(result.get("data"), dict) or type(result.get("complete")) is not bool
            or result.get("status") not in {"observed", "unavailable", "not_selected"}):
        raise Fault("ssh_protocol_error", "The pinned worker did not return the requested target observation contract.")
    if "machine" in result:
        result["machine"] = machine
    items = result.get("executions", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The execution list response was invalid.")
    for item in items:
        item["machine"] = machine
    return result | {"target_identity": response["identity"], "worker_sha256": response["worker_sha256"]}
