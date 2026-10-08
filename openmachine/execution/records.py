"""Execution request/receipt contracts, without process observation or writes."""

import hashlib
import json
import math
from pathlib import Path
import re

from openmachine.foundation.sanitize import Fault
from openmachine.workspace import worktrees

MAX_TIMEOUT = 86400
FORMAT = 5
CREATION_FORMAT = 6
DEFAULT_GRACE = 5.0
LEGACY_GRACE = 0.5
CONTROL_PROTOCOL = 1
MAX_CONTROL_SEQ = 2**53 - 1
MAX_INPUT = 16384
CONTROL_ACTIONS = {"write_stdin", "interrupt", "resize"}
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


def resolve_terminal_size(tty: bool, value: dict | None) -> dict | None:
    if type(tty) is not bool:
        raise Fault("invalid_tty", "tty must be an explicit boolean.")
    if not tty:
        if value is not None:
            raise Fault("invalid_terminal_size", "Pipe execution has no terminal dimensions.")
        return None
    if value is None:
        return {"rows": 24, "cols": 80}
    if (not isinstance(value, dict) or set(value) != {"rows", "cols"}
            or any(type(v) is not int or not 1 <= v <= 1000 for v in value.values())):
        raise Fault("invalid_terminal_size", "Use integer rows and cols from 1 to 1000, with no extra fields.")
    return dict(value)


def control_request(expected_control_seq: int, action: str, text: str | None = None,
                    terminal_size: dict | None = None) -> dict:
    if type(expected_control_seq) is not int or not 0 <= expected_control_seq < MAX_CONTROL_SEQ:
        raise Fault("invalid_control_seq", "Use the previously observed sequence from 0 through 2^53-2.")
    if not isinstance(action, str) or action not in CONTROL_ACTIONS:
        raise Fault("invalid_control_action", "Use write_stdin, interrupt or resize.")
    if action == "write_stdin":
        if not isinstance(text, str) or "\0" in text or terminal_size is not None:
            raise Fault("invalid_input", "Supply UTF-8 text without NUL and no terminal_size.")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError as error:
            raise Fault("invalid_input", "Input must encode as valid UTF-8 without unpaired surrogates.") from error
        if not 1 <= size <= MAX_INPUT:
            raise Fault("invalid_input", "Supply 1 through 16384 UTF-8 bytes; no newline is added.")
    elif action == "resize":
        if text is not None or terminal_size is None:
            raise Fault("invalid_control", "Resize requires terminal_size and no text.")
        terminal_size = resolve_terminal_size(True, terminal_size)
    elif text is not None or terminal_size is not None:
        raise Fault("invalid_control", "Interrupt accepts neither text nor terminal_size.")
    return {"expected_control_seq": expected_control_seq, "action": action,
            "text": text, "terminal_size": terminal_size}


def control_fingerprint(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def effective_request(record: dict) -> dict:
    """Legacy omission means its original behavior, never a new writer's default."""
    return {"workspace_access": "exclusive", "resource_keys": [], "task_id": "",
            "termination_grace_seconds": LEGACY_GRACE, "tty": False, "terminal_size": None,
            **record["request"]}


def is_remote(record: dict) -> bool:
    return "scope_unit" in record


def _validate_facts(record: dict) -> None:
    command, stop, cleanup = record["command"], record["stop"], record["cleanup"]
    modes = {"pipe", "pty"} if record["format_version"] >= FORMAT else {"pipe"}
    if record["io_mode"] not in modes:
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


def _validate_interactive(record: dict) -> None:
    request = record["request"]
    tty = request["tty"]
    if resolve_terminal_size(tty, request["terminal_size"]) != request["terminal_size"]:
        raise ValueError("unresolved initial terminal size")
    if resolve_terminal_size(tty, record["terminal_size"]) != record["terminal_size"]:
        raise ValueError("invalid current terminal size")
    if record["io_mode"] != ("pty" if tty else "pipe") or type(record["control_ready"]) is not bool:
        raise ValueError("inconsistent terminal facts")
    control = record["control"]
    if control is None:
        if tty or record["control_ready"]:
            raise ValueError("missing terminal control")
        return
    if (not isinstance(control, dict) or set(control) != {"protocol", "seq", "last"}
            or type(control["protocol"]) is not int or control["protocol"] != CONTROL_PROTOCOL
            or type(control["seq"]) is not int or not 0 <= control["seq"] <= MAX_CONTROL_SEQ):
        raise ValueError("invalid control contract")
    last = control["last"]
    if control["seq"] == 0:
        if last is not None:
            raise ValueError("unexpected control receipt")
        return
    if (not isinstance(last, dict) or set(last) != {"seq", "action", "request_sha256", "status",
                                                  "bytes_written", "accepted_at", "completed_at", "error"}
            or type(last["seq"]) is not int or last["seq"] != control["seq"]
            or last["action"] not in CONTROL_ACTIONS
            or not re.fullmatch(r"[0-9a-f]{64}", last["request_sha256"])
            or last["status"] not in {"pending", "applied", "not_applied", "partial", "outcome_unknown"}
            or not _number(last["accepted_at"])
            or (last["error"] is not None and (not isinstance(last["error"], str) or len(last["error"]) > 128))):
        raise ValueError("invalid last control")
    if not (last["completed_at"] is None if last["status"] == "pending" else _number(last["completed_at"])):
        raise ValueError("invalid control publication")
    written = last["bytes_written"]
    if last["action"] != "write_stdin" or last["status"] in {"pending", "outcome_unknown"}:
        if written is not None:
            raise ValueError("unverified byte count")
    elif type(written) is not int or not 0 <= written <= MAX_INPUT:
        raise ValueError("invalid delivered byte count")
    elif (last["status"] == "not_applied") != (written == 0):
        raise ValueError("inconsistent input effect")


def _validate_creation(record: dict) -> None:
    request = record["request"]
    intent = request["worktree_creation"]
    plan = record["context"]["creation_plan"]
    if not isinstance(intent, dict) or set(intent) != {
            "workspace", "destination", "base_ref", "new_branch", "expected_workspace_identity", "worktree"}:
        raise ValueError("invalid worktree request")
    if not isinstance(intent["workspace"], str) or not 1 <= len(intent["workspace"]) <= 128:
        raise ValueError("invalid worktree workspace")
    try:
        normalized = worktrees.normalize_creation(**{key: value for key, value in intent.items() if key != "workspace"})
        worktrees.validate_creation_plan(plan)
    except Fault as error:
        raise ValueError("invalid worktree plan") from error
    if (normalized != {key: value for key, value in intent.items() if key != "workspace"}
            or plan["destination"] != intent["destination"] or plan["branch"] != intent["new_branch"]
            or plan["source_identity"] != intent["expected_workspace_identity"]
            or request["cwd"] != plan["source_root"] or request["argv"] != plan["argv"]
            or request["resource_keys"] != plan["resource_keys"]
            or record["context"]["workspace_lock_root"] != plan["destination"]
            or request["workspace_access"] != "exclusive" or request["expected_head_sha"]
            or request["tty"] or request["terminal_size"] is not None):
        raise ValueError("inconsistent worktree execution")
    created = record["context"].get("created_worktree")
    if created is not None and (not isinstance(created, dict)
            or created.get("worktree") != plan["destination"]
            or created.get("common_dir") != plan["common_dir"]
            or created.get("head_sha") != plan["base_commit"]
            or created.get("branch") != (plan["branch"] or None)
            or not isinstance(created.get("workspace_identity"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", created["workspace_identity"])):
        raise ValueError("invalid created worktree identity")
    if record["status"] == "succeeded" and created is None:
        raise ValueError("unverified worktree creation")


def read_record(directory: Path) -> dict:
    """Validate all accepted formats without rewriting or observing the process."""
    try:
        with (directory / "record.json").open() as stream:
            record = json.load(stream)
        version = record.get("format_version", 1)
        if type(version) is not int or version not in (1, 2, 3, 4, FORMAT, CREATION_FORMAT):
            raise Fault("unsupported_state_version", "This runtime cannot read the execution receipt version.")
        states = TERMINAL | {"accepted", "preparing", "running", "stopping", "finishing"} if version >= 4 else (
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
            if (version not in (3, 4, FORMAT, CREATION_FORMAT) or record.get("scope_unit") != f"openmachine-{directory.name}.scope"
                    or not re.fullmatch(r"[0-9a-f]{64}", request["machine"])
                    or record["context"].get("target_identity") != request["machine"]
                    or not re.fullmatch(r"[0-9a-f]{64}", record["context"].get("worker_sha256", ""))):
                raise ValueError("invalid remote ownership")
        if version >= 4:
            _validate_facts(record)
        if version >= FORMAT:
            try:
                _validate_interactive(record)
            except Fault as error:
                raise ValueError("invalid interactive receipt") from error
        if version == CREATION_FORMAT:
            _validate_creation(record)
        elif "worktree_creation" in request or "creation_plan" in record["context"] or "created_worktree" in record["context"]:
            raise ValueError("unsupported worktree execution semantics")
        return record
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Fault("corrupt_state", "The original execution receipt is missing or invalid; do not relaunch.") from error
