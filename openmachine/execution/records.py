"""Execution receipt formats and validation, without process observation or writes."""

import json
import math
from pathlib import Path
import re

from openmachine.foundation.sanitize import Fault

MAX_TIMEOUT = 86400
FORMAT = 4
DEFAULT_GRACE = 5.0
LEGACY_GRACE = 0.5
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted", "outcome_unknown"}
STOP_STATUS = {"cancel": "cancelled", "timeout": "timed_out", "worker_signal": "interrupted"}


def _number(value) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def termination_grace(value: float | None) -> float:
    if value is None:
        return DEFAULT_GRACE
    if not _number(value) or not 0 <= value <= 30:
        raise Fault("invalid_grace", "Use null or a finite termination grace from 0 to 30 seconds.")
    return float(value)


def effective_request(record: dict) -> dict:
    """Legacy omission means its original behavior, never a new writer's default."""
    return {"workspace_access": "exclusive", "resource_keys": [], "task_id": "",
            "termination_grace_seconds": LEGACY_GRACE, **record["request"]}


def is_remote(record: dict) -> bool:
    return "scope_unit" in record


def _validate_facts(record: dict) -> None:
    command, stop, cleanup = record["command"], record["stop"], record["cleanup"]
    if record["io_mode"] != "pipe":
        raise ValueError("unsupported IO mode")
    grace = record["request"]["termination_grace_seconds"]
    if (not _number(grace) or not 0 <= grace <= 30 or not _number(stop["grace_seconds"])
            or stop["grace_seconds"] != grace):
        raise ValueError("invalid effective grace")
    if any(type(command[key]) is not bool for key in ("spawned", "exited")) or type(stop["force_used"]) is not bool:
        raise ValueError("invalid command facts")
    for fact, timestamp in (("spawned", "started_at"), ("exited", "exited_at")):
        if (_number(command[timestamp]) if command[fact] else command[timestamp] is None) is not True:
            raise ValueError("inconsistent command observation")
    if command["exited"] and not command["spawned"]:
        raise ValueError("exit without launch")
    code, sig = record["exit_code"], command["exit_signal"]
    if code is not None and not command["exited"]:
        raise ValueError("exit code without exit observation")
    if (sig is not None and type(sig) is not int) or sig != (-code if code is not None and code < 0 else None):
        raise ValueError("inconsistent exit signal")
    if stop["reason"] not in {None, *STOP_STATUS}:
        raise ValueError("invalid stop reason")
    if not (_number(stop["requested_at"]) if stop["reason"] else stop["requested_at"] is None):
        raise ValueError("invalid stop observation")
    containment = "systemd_scope" if is_remote(record) else "posix_session"
    if cleanup["containment"] != containment or cleanup["status"] not in {"pending", "verified", "unverified"}:
        raise ValueError("invalid cleanup")
    if cleanup["error"] is not None and (not isinstance(cleanup["error"], str) or len(cleanup["error"]) > 128):
        raise ValueError("invalid cleanup error")
    if record["status"] == "succeeded":
        if not command["spawned"] or not command["exited"] or stop["reason"] or record.get("error"):
            raise ValueError("inconsistent success")
        if cleanup["status"] != "verified" and not (is_remote(record) and cleanup["status"] == "pending"):
            raise ValueError("unverified success")
    if record["status"] in STOP_STATUS.values() and STOP_STATUS.get(stop["reason"]) != record["status"]:
        raise ValueError("inconsistent stop result")
    if cleanup["status"] == "unverified" and record["status"] in TERMINAL - {"outcome_unknown"}:
        raise ValueError("unverified terminal cleanup")


def read_record(directory: Path) -> dict:
    """Validate an original execution record without rewriting or observing its process."""
    try:
        with (directory / "record.json").open() as stream:
            record = json.load(stream)
        version = record.get("format_version", 1)
        if type(version) is not int or version not in (1, 2, 3, FORMAT):
            raise Fault("unsupported_state_version", "This runtime cannot read the execution receipt version.")
        states = TERMINAL | {"accepted", "preparing", "running", "stopping", "finishing"} if version == FORMAT else (
            TERMINAL - {"outcome_unknown"}) | {"accepted", "preparing", "running"}
        if record["status"] not in states:
            raise ValueError("invalid state")
        code = record["exit_code"]
        if code is not None and type(code) is not int:
            raise ValueError("invalid exit code")
        if record["status"] == "succeeded" and code != 0:
            raise ValueError("missing successful exit")
        if not isinstance(record["request"], dict) or not isinstance(record["context"], dict):
            raise ValueError("invalid receipt")
        request = record["request"]
        linked_task = request.get("task_id", "")
        if not isinstance(linked_task, str) or (linked_task and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", linked_task)):
            raise ValueError("invalid task reference")
        if not all(isinstance(request[key], str) for key in ("machine", "cwd", "expected_head_sha")):
            raise ValueError("invalid request identity")
        if not isinstance(request["argv"], list) or not request["argv"] or not all(isinstance(a, str) for a in request["argv"]):
            raise ValueError("missing command")
        if type(request["timeout_seconds"]) is not int or not 1 <= request["timeout_seconds"] <= MAX_TIMEOUT:
            raise ValueError("invalid timeout")
        if not _number(record["created_at"]):
            raise ValueError("missing creation time")
        if not isinstance(record["version"], str) or any(
            type(record[key]) is not bool for key in ("log_truncated", "output_incomplete")
        ):
            raise ValueError("invalid receipt metadata")
        if record["status"] in TERMINAL and not _number(record["finished_at"]):
            raise ValueError("missing terminal receipt")
        if version == 3 or is_remote(record):
            if (version not in (3, FORMAT) or record.get("scope_unit") != f"openmachine-{directory.name}.scope"
                    or not re.fullmatch(r"[0-9a-f]{64}", request["machine"])
                    or record["context"].get("target_identity") != request["machine"]
                    or not re.fullmatch(r"[0-9a-f]{64}", record["context"].get("worker_sha256", ""))):
                raise ValueError("invalid remote ownership")
        if version == FORMAT:
            _validate_facts(record)
        return record
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Fault("corrupt_state", "The original execution receipt is missing or invalid; do not relaunch.") from error
