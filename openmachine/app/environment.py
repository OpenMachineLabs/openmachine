"""Task-scoped observation composition; domains retain facts and all authority."""

import json
import re
import time

from openmachine.foundation.sanitize import Fault

SECTIONS = ("workspace", "worktrees", "activity", "runtime")
DEFAULT_SECTIONS = ("workspace", "activity", "runtime")
MAX_LIMIT = 50
MAX_PAYLOAD = 65536
IDENTITY_FIELDS = ("cwd", "worktree", "workspace_identity", "head_sha", "branch", "git_state", "git_status")


def capabilities() -> dict:
    return {"schema_version": 1, "sections": list(SECTIONS), "default_sections": list(DEFAULT_SECTIONS),
            "default_limit": 10, "max_limit": MAX_LIMIT, "payload_bytes": MAX_PAYLOAD,
            "active_probes": False, "incremental": False}


def _ref(tool: str, **arguments) -> dict:
    return {"tool": tool, "arguments": arguments}


def _section(source: dict, kind: str, *, status: str = "observed", complete: bool = True,
             data: dict | None = None, limitations: tuple[str, ...] = ()) -> dict:
    return {"status": status, "observed_at_ms": time.time_ns() // 1000000,
            "source": source, "kind": kind, "complete": complete,
            "data": data or {}, "limitations": list(limitations)}


def _same_target(before: dict, after: dict) -> None:
    if any(before.get(key) != after.get(key) for key in IDENTITY_FIELDS):
        raise Fault("source_changed", "The target changed during observation; inspect the intended target again.")


def _git_section(app, selection: dict, context: dict, action: str, limit: int) -> dict:
    reference = _ref("inspect_workspace", **selection, action=action, limit=limit)
    if context["git_status"] == "not_a_worktree":
        return _section(reference, "current_read", status="not_applicable",
                        limitations=("The selected directory is confirmed non-Git.",))
    if context["git_status"] != "observed":
        raise Fault("git_unavailable", "Git identity is unavailable; this does not establish a clean or non-Git directory.")
    page = app.inspect_workspace(**selection, action=action, limit=limit)
    _same_target(context, page["context"])
    complete = (page.get("has_more") is False and page.get("truncated") is False
                and (action == "worktrees" or page.get("skipped_paths") == 0))
    result = _section(reference, "current_read", complete=complete,
        data={key: page[key] for key in ("entries", "snapshot", "offset", "next_offset", "has_more",
                                       "truncated", "skipped_paths", "ordering", "warnings") if key in page},
        limitations=(("Registration and path grants do not establish ownership, cleanliness or idleness.",)
                     if action == "worktrees" else ("Status does not inspect submodule contents or external activity.",)))
    result["snapshot"] = page["snapshot"]
    result["next_ref"] = (_ref("inspect_workspace", **selection, action=action, limit=limit,
        offset=page["next_offset"], expected_snapshot=page["snapshot"]) if page.get("has_more") else None)
    return result


def _binding(targets: list[dict], selection: dict, context: dict) -> dict:
    expected = {"machine": selection["machine"], "workspace": selection["workspace_name"],
                "worktree": selection.get("worktree", ""),
                "workspace_identity": context.get("workspace_identity"), "head_sha": context.get("head_sha")}
    comparisons = []
    for target in targets:
        differences = [key for key, value in expected.items()
                       if target.get(key, "" if key == "worktree" else None) != value]
        comparisons.append({"target": target, "matches": not differences, "differing_fields": differences})
    return {"matches": any(item["matches"] for item in comparisons), "comparisons": comparisons,
            "authority": "Observation does not authorize target adoption or task continuation."}


def _activity(app, selection: dict, context: dict, task_id: str, limit: int) -> dict:
    limitations = ("Retained task evidence, not a fresh probe of every original process.",
                   "Other tasks and external editors/processes are not enumerated.")
    if not task_id:
        return _section(_ref("find_tasks"), "retained_evidence", status="not_selected", complete=False,
                        limitations=limitations + ("Select an original task explicitly; no activity count was inferred.",))
    reference = _ref("read_task", task_id=task_id)
    summary = app.read_task(task_id)
    record = summary.get("record")
    if not isinstance(record, dict) or not isinstance(record.get("contract"), dict):
        return _section(reference, "retained_evidence", status="not_applicable", complete=False,
                        limitations=("This task has no structured target contract; read its original legacy record.",))
    if summary.get("current_record_complete") is not True:
        raise Fault("context_task_incomplete", "The current task contract is incomplete; read the original task directly.")
    page = app.read_task(task_id, section="operations", limit=limit)
    after = app.read_task(task_id)
    if page.get("task_revision") != record["revision"] or after["record"]["revision"] != record["revision"]:
        raise Fault("source_changed", "The task changed during observation; read a fresh summary and operation page.")
    contract = record["contract"]
    operations = []
    for operation in page["items"]:
        item = {key: operation[key] for key in ("id", "stage_id", "plan_revision", "epoch", "locator",
                  "admitted_at_ms", "state", "observed_at_ms", "result") if key in operation}
        item["evidence_ref"] = _ref("read_task", task_id=task_id, section="operations", operation_id=operation["id"])
        operations.append(item)
    result = _section(reference, "retained_evidence", complete=page.get("complete") is True,
        data={"task_id": task_id, "title": record["title"], "revision": record["revision"],
              "updated_at_ms": record["updated_at_ms"], "plan_revision": record["plan"]["revision"],
              "contract": {key: contract[key] for key in ("objective", "scope", "exclusions", "acceptance",
                  "stop_condition", "authority_refs", "expires_at_ms", "targets", "tools")},
              "contract_complete": False,
              "control": record["control"], "handoff": record["handoff"], "terminal": record["terminal"],
              "counts": summary["counts"], "unresolved_operation_ids": summary["unresolved_operation_ids"],
              "target_binding": _binding(contract["targets"], selection, context), "operations": operations,
              "has_more": page["has_more"], "watermark": page.get("watermark"),
              "foreground": summary.get("foreground"), "observations": summary.get("observations", [])},
        limitations=limitations)
    result["revision"] = record["revision"]
    result["next_ref"] = (_ref("read_task", task_id=task_id, section="operations", limit=limit,
        cursor=page["next_cursor"]) if page["has_more"] else None)
    return result


def _runtime(app, machine: str) -> dict:
    caps = app.get_capabilities()
    selected = caps["machines"][machine]
    return _section(_ref("get_capabilities"), "configuration_not_health", data={
        "controller": {key: caps.get(key) for key in ("version", "commit", "configuration_sha256", "transport")},
        "selected_machine": {key: selected.get(key) for key in ("transport", "execution_enabled",
            "workspace_write_effective", "probe_status")},
        "execution": caps["execution"], "limits": caps["limits"], "boundary": caps["boundary"]},
        limitations=("Configured support and grants are not live backend health or permission for this request.",
                     "Controller version/limits are not remote worker or command-process measurements."))


def inspect_context(app, machine: str, workspace_name: str, worktree: str = "", task_id: str = "",
                    sections: list[str] | None = None, limit: int = 10) -> dict:
    selected = list(DEFAULT_SECTIONS) if sections is None else sections
    if (type(selected) is not list or not 1 <= len(selected) <= len(SECTIONS)
            or any(type(item) is not str or item not in SECTIONS for item in selected)
            or len(set(selected)) != len(selected) or type(limit) is not int or not 1 <= limit <= MAX_LIMIT):
        raise Fault("invalid_context_request", "Select distinct implemented sections and a limit from 1 to 50.")
    for value in (machine, workspace_name):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise Fault("invalid_context_request", "Select a configured machine and workspace name.")
    if not isinstance(task_id, str) or task_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id):
        raise Fault("invalid_context_request", "Use an original task ID or leave it empty.")
    if not isinstance(worktree, str) or len(worktree) > 4096:
        raise Fault("invalid_context_request", "Use a bounded worktree selector.")
    target_machine = app.config.machine(machine)
    workspace = app.config.workspaces.get(workspace_name)
    if workspace is None or workspace.machine != machine:
        raise Fault("unknown_workspace", "Select a workspace configured on this machine.")
    selection = {"machine": machine, "workspace_name": workspace_name, **({"worktree": worktree} if worktree else {})}
    started = time.time_ns() // 1000000
    # The existing reader owns scope and canonical identity, including SSH routing.
    initial = app.read_workspace_file(**selection, path="AGENTS.md", start_line=1, line_count=1)
    context = initial["context"]
    result = {"schema_version": 1, "atomic": False, "started_at_ms": started,
              "target": {"machine": machine, "transport": target_machine.transport,
                         "workspace": workspace_name, "requested_worktree": worktree, **context,
                         "observed_at_ms": time.time_ns() // 1000000,
                         "instructions_loaded": False,
                         "source": _ref("read_workspace_file", **selection, path="AGENTS.md", start_line=1, line_count=1)},
              "sections": {}}
    for section in selected:
        try:
            if section in ("workspace", "worktrees"):
                value = _git_section(app, selection, context, "status" if section == "workspace" else "worktrees", limit)
            elif section == "activity":
                value = _activity(app, selection, context, task_id, limit)
            else:
                value = _runtime(app, machine)
        except (Fault, OSError) as error:
            # A mixed workspace is invalid as a whole; task-page drift is local to activity.
            if isinstance(error, Fault) and section != "activity" and error.code in {
                    "source_changed", "context_mismatch", "path_denied", "unknown_worktree", "identity_mismatch"}:
                raise
            code = error.code if isinstance(error, Fault) else "source_unavailable"
            source = (_ref("read_task", task_id=task_id) if section == "activity" else
                      _ref("get_capabilities") if section == "runtime" else
                      _ref("inspect_workspace", **selection, action="status" if section == "workspace" else "worktrees", limit=limit))
            value = _section(source, "retained_evidence" if section == "activity" else
                             "configuration_not_health" if section == "runtime" else "current_read",
                             status="unavailable", complete=False)
            value["error"] = {"code": code, "message": str(error) if isinstance(error, Fault) else type(error).__name__}
        result["sections"][section] = value
    final = app.read_workspace_file(**selection, path="AGENTS.md", start_line=1, line_count=1)
    _same_target(context, final["context"])
    result["finished_at_ms"] = time.time_ns() // 1000000
    result["target"]["rechecked_at_ms"] = result["finished_at_ms"]
    result["limitations"] = ["No observation grants authority, reserves resources or proves external idleness.",
                             "Refresh affected facts before acting; domain guards remain authoritative."]
    if len(json.dumps(result, ensure_ascii=True).encode()) > MAX_PAYLOAD:
        raise Fault("context_response_limit", "Context exceeds 64 KiB; select fewer sections or a smaller limit.")
    return result
