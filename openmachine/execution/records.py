"""Execution receipt formats and validation, without process observation or writes."""

import json
from pathlib import Path
import re

from openmachine.foundation.sanitize import Fault

MAX_TIMEOUT = 86400
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}


def read_record(directory: Path) -> dict:
    """Validate an original execution record without rewriting or observing its process."""
    try:
        with (directory / "record.json").open() as stream:
            record = json.load(stream)
        if record["status"] not in TERMINAL | {"accepted", "preparing", "running"}:
            raise ValueError("invalid state")
        if record.get("format_version", 1) not in (1, 2, 3):
            raise Fault("unsupported_state_version", "This runtime cannot read the execution receipt version.")
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
        if not isinstance(record["created_at"], (int, float)):
            raise ValueError("missing creation time")
        if not isinstance(record["version"], str) or any(
            type(record[key]) is not bool for key in ("log_truncated", "output_incomplete")
        ):
            raise ValueError("invalid receipt metadata")
        if record["status"] in TERMINAL and not isinstance(record["finished_at"], (int, float)):
            raise ValueError("missing terminal receipt")
        if record.get("format_version") == 3:
            if (record.get("scope_unit") != f"openmachine-{directory.name}.scope"
                    or not re.fullmatch(r"[0-9a-f]{64}", request["machine"])
                    or record["context"].get("target_identity") != request["machine"]
                    or not re.fullmatch(r"[0-9a-f]{64}", record["context"].get("worker_sha256", ""))):
                raise ValueError("invalid remote ownership")
        return record
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Fault("corrupt_state", "The original execution receipt is missing or invalid; do not relaunch.") from error
