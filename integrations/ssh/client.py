"""Bounded controller-side SSH requests; no task store or target implementation."""

import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import time
from typing import TYPE_CHECKING

from openmachine import __version__
from openmachine.foundation.sanitize import Fault

from . import MAX_MESSAGE, PROTOCOL

if TYPE_CHECKING:
    from openmachine.config import Settings

MUTATIONS = {"start_execution", "write_workspace_file", "cancel_execution"}


def _exchange(argv: list[str], payload: bytes) -> tuple[int, bytes]:
    """Bound stdin, both output streams and elapsed time without a shell or replay."""
    if len(payload) > MAX_MESSAGE:
        raise Fault("request_limit", "Narrow the request to at most one MiB of encoded JSON.")
    output, diagnostic = bytearray(), bytearray()
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0) as process:
        try:
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdin, process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, None)
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, diagnostic)
                offset = 0
                deadline = time.monotonic() + 30
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    for key, _ in selector.select(0.05):
                        if key.fileobj is process.stdin:
                            offset += os.write(key.fd, payload[offset:offset + 65536])
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
                return process.wait(timeout=max(0.1, deadline - time.monotonic())), bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def call(config: "Settings", machine: str, operation: str, **arguments) -> dict:
    target = config.machine(machine, write=operation in {"start_execution", "write_workspace_file"})
    if target.transport != "ssh":
        raise Fault("not_ssh", "Select a configured SSH target, or omit the explicit doctor target.")
    if not target.ssh_identity and operation != "probe":
        raise Fault("identity_required", "Probe this target and explicitly pin its ssh_identity before workspace access.")
    request = {"protocol": PROTOCOL, "version": __version__, "identity": target.ssh_identity,
               "execution_enabled": target.execution_enabled, "operation": operation, "arguments": arguments,
               "workspaces": {name: item.root for name, item in config.workspaces.items() if item.machine == machine}}
    argv = ["ssh", "-T", "-a", "-x", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "LogLevel=ERROR", "--",
            target.ssh_alias, shlex.join(["python3", "-I", target.ssh_worker, "rpc"])]
    mutating = operation in MUTATIONS
    try:
        code, raw = _exchange(argv, json.dumps(request, ensure_ascii=True).encode())
    except FileNotFoundError as error:
        raise Fault("ssh_missing", "Install the system OpenSSH client; no target was contacted.") from error
    except (OSError, TimeoutError, subprocess.SubprocessError, Fault) as error:
        if isinstance(error, Fault) and (error.code == "request_limit" or not mutating):
            raise
        raise Fault("outcome_unknown" if mutating else "ssh_unavailable",
                    "No complete SSH response was verified. Read the original execution or destination before any mutation retry.") from error
    try:
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise ValueError
    except (ValueError, UnicodeError) as error:
        reason = "ssh_unavailable" if code == 255 else "remote_runtime_unavailable" if code == 2 else "ssh_protocol_error"
        raise Fault("outcome_unknown" if mutating else reason,
                    "Check the existing SSH identity, Python 3.12+, and the configured worker archive. No result was verified.") from error
    if "error" in response:
        error = response["error"]
        if isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
            raise Fault(error["code"], error["message"])
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The remote error response was invalid.")
    if (code or response.get("protocol") != PROTOCOL or response.get("version") != __version__
            or response.get("worker_sha256") != Path(target.ssh_worker).stem
            or not isinstance(response.get("identity"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", response["identity"])
            or (target.ssh_identity and response["identity"] != target.ssh_identity)
            or not isinstance(response.get("result"), dict)):
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The remote response identity was not verified.")
    result = response["result"]
    if "machine" in result:
        result["machine"] = machine
    items = result.get("executions", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise Fault("outcome_unknown" if mutating else "ssh_protocol_error", "The execution list response was invalid.")
    for item in items:
        item["machine"] = machine
    return result | {"target_identity": response["identity"], "worker_sha256": response["worker_sha256"]}
