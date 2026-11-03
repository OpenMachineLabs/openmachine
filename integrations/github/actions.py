"""Bounded Actions controls and deployment observations; no workflow executor."""

import json
import math
import re
from typing import Annotated, Literal
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openmachine.foundation.sanitize import Fault, redact
from . import client, connection, evidence
from .connection import object_response

WorkflowAction = Literal["dispatch", "rerun", "rerun_failed", "cancel"]
READ_ACTIONS = {"workflows", "workflow", "workflow_runs", "workflow_run", "workflow_jobs",
                "deployments", "deployment", "deployment_statuses"}
MAX_ID = 2**53 - 1
WORKFLOW = r"(?:[1-9][0-9]{0,15}|[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}\.ya?ml)"
Text = Annotated[str, Field(strict=True, min_length=1, max_length=255)]


class ReadFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    workflow: Annotated[str, Field(pattern="^" + WORKFLOW + "$")] | None = None
    branch: Text | None = None
    head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None = None
    event: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")] | None = None
    status: Literal["completed", "action_required", "cancelled", "failure", "neutral", "skipped",
                    "stale", "success", "timed_out", "in_progress", "queued", "requested", "waiting", "pending"] | None = None
    environment: Text | None = None
    task: Text | None = None
    attempt: Annotated[int, Field(strict=True, ge=1, le=MAX_ID)] | None = None


def positive(value) -> bool:
    return type(value) is int and 0 < value <= MAX_ID


def valid_ref(value) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 255 and value != "@"
            and not value.startswith("-")
            and not re.search(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{", value)
            and not value.endswith(".")
            and all(part and not part.startswith(".") and not part.endswith(".lock") for part in value.split("/")))


def workflow_selector(value) -> bool:
    return (isinstance(value, str) and re.fullmatch(WORKFLOW, value) is not None
            and ".." not in value and (not value.isdecimal() or positive(int(value))))


def workflow_metadata(repository, selector, *, transport):
    value = object_response(transport("GET", f"repos/{repository}/actions/workflows/{quote(selector, safe='')}"))
    if (not positive(value.get("id")) or not isinstance(value.get("path"), str)
            or (str(value["id"]) != selector if selector.isdecimal()
                else value["path"] != f".github/workflows/{selector}")):
        raise Fault("github_invalid_response", "The workflow identity differs from the requested ID or filename.")
    return value


def run_metadata(repository, number, *, attempt=None, transport):
    endpoint = f"repos/{repository}/actions/runs/{number}"
    if attempt is not None:
        endpoint += f"/attempts/{attempt}"
    value = object_response(transport("GET", endpoint))
    validate_run(repository, value)
    if value["id"] != number or attempt is not None and value["run_attempt"] != attempt:
        raise Fault("github_invalid_response", "GitHub returned another run or attempt.")
    return value


def validate_run(repository, value):
    returned_repository = object_response(value.get("repository")).get("full_name")
    if (not positive(value.get("id")) or not positive(value.get("workflow_id"))
            or not positive(value.get("run_attempt"))
            or not isinstance(value.get("head_sha"), str) or not re.fullmatch(evidence.SHA, value["head_sha"])
            or not isinstance(returned_repository, str) or returned_repository.casefold() != repository.casefold()
            or value.get("status") not in ("queued", "in_progress", "completed", "waiting", "requested", "pending")
            or "conclusion" not in value or value["conclusion"] is not None and not isinstance(value["conclusion"], str)):
        raise Fault("github_invalid_response", "The run identity, repository, attempt or state is incomplete.")


FIELDS = {
    "workflow": "id name path state html_url created_at updated_at",
    "workflow_run": "id workflow_id name display_title head_branch head_sha event status conclusion run_number run_attempt html_url created_at updated_at run_started_at",
    "workflow_jobs": "id run_id run_attempt name head_sha status conclusion started_at completed_at html_url steps",
    "deployment": "id sha ref task environment original_environment description created_at updated_at transient_environment production_environment",
    "deployment_statuses": "id state description environment environment_url log_url created_at updated_at",
}


def project(kind, value):
    required = {"workflow": ("path", "state"), "workflow_run": ("status",),
                "workflow_jobs": ("name", "status"), "deployment": ("sha", "ref", "environment", "task"),
                "deployment_statuses": ("state",)}[kind]
    if not positive(value.get("id")) or any(not isinstance(value.get(key), str) for key in required):
        raise Fault("github_invalid_response", "The provider observation is missing required fields.")
    result = {key: value[key] for key in FIELDS[kind].split() if key in value}
    if kind == "workflow_jobs":
        steps = value.get("steps")
        if not isinstance(steps, list) or any(not isinstance(step, dict) or not positive(step.get("number"))
                or not isinstance(step.get("name"), str) or not isinstance(step.get("status"), str) for step in steps):
            raise Fault("github_invalid_response", "Job steps are missing or malformed.")
        result["steps"] = [{key: step.get(key) for key in ("number", "name", "status", "conclusion", "started_at", "completed_at")} for step in steps]
    return result


def read(action, repository, number, ref, path, page, limit, filters, *, transport):
    try:
        filters = ReadFilters.model_validate({} if filters is None else filters).model_dump(exclude_none=True)
    except ValidationError as error:
        raise Fault("invalid_github_request", "Use only the closed Actions/deployment filters.") from error
    allowed = {"workflow_runs": {"workflow", "branch", "head_sha", "event", "status"},
               "workflow_run": {"attempt"}, "workflow_jobs": {"attempt"},
               "deployments": {"environment", "task", "head_sha"}}
    if (type(page) is not int or type(limit) is not int or not 1 <= page <= 1000 or not 1 <= limit <= 100
            or set(filters) - allowed.get(action, set()) or any(
            isinstance(value, str) and any(ord(c) < 32 or ord(c) == 127 for c in value) for value in filters.values())
            or "branch" in filters and not valid_ref(filters["branch"])
            or "workflow" in filters and not workflow_selector(filters["workflow"])
            or type(number) is not int or number < 0 or number > MAX_ID
            or ref and (action != "deployments" or not valid_ref(ref))
            or path and action != "workflow"):
        raise Fault("invalid_github_request", "Filters and selectors must belong to the selected read action.")
    root = f"repos/{repository}"
    key, kind, extra = "", action, {}
    if action == "workflow":
        if bool(number) == bool(path) or path and not workflow_selector(path):
            raise Fault("invalid_github_request", "Select one workflow ID in number or filename in path.")
        return {"data": project(action, workflow_metadata(repository, str(number) if number else path, transport=transport))}
    if action in {"workflow_run", "workflow_jobs", "deployment", "deployment_statuses"}:
        if not positive(number):
            raise Fault("invalid_github_request", "Select a positive run or deployment ID in number.")
    elif number:
        raise Fault("invalid_github_request", "List reads do not accept an object number.")
    if action in {"workflow_run", "workflow_jobs"}:
        if action == "workflow_jobs" and "attempt" not in filters:
            raise Fault("invalid_github_request", "Jobs require the observed run attempt in filters.attempt.")
        run = run_metadata(repository, number, attempt=filters.get("attempt"), transport=transport)
        if action == "workflow_run":
            return {"data": project(action, run)}
        endpoint, key = f"{root}/actions/runs/{number}/attempts/{run['run_attempt']}/jobs", "jobs"
        extra = {"run_id": number, "run_attempt": run["run_attempt"], "head_sha": run["head_sha"]}
    elif action == "workflows":
        endpoint, key, kind = f"{root}/actions/workflows", "workflows", "workflow"
    elif action == "workflow_runs":
        selector = filters.pop("workflow", None)
        selected = workflow_metadata(repository, selector, transport=transport) if selector else None
        endpoint = f"{root}/actions/workflows/{selected['id']}/runs" if selected else f"{root}/actions/runs"
        endpoint += "?" + urlencode(filters)
        key, kind = "workflow_runs", "workflow_run"
        # Filtered GitHub run searches expose at most 1,000 results.
        extra = {"search_limit": 1000} if filters else {}
    elif action in {"deployment", "deployment_statuses"}:
        deployment = object_response(transport("GET", f"{root}/deployments/{number}"))
        if deployment.get("id") != number:
            raise Fault("github_invalid_response", "GitHub returned another deployment.")
        if action == "deployment":
            return {"data": project(action, deployment)}
        endpoint = f"{root}/deployments/{number}/statuses"
        extra = {"deployment": project("deployment", deployment)}
    else:
        query = {"sha" if key == "head_sha" else key: value for key, value in filters.items()}
        if ref:
            query["ref"] = ref
        endpoint, kind = f"{root}/deployments?{urlencode(query)}", "deployment"
    items, following = client.list_page(endpoint, page, limit, key, transport=transport)
    for item in items:
        if not positive(item.get("id")):
            raise Fault("github_invalid_response", "A returned object has no positive identity.")
        if action == "workflow_runs":
            validate_run(repository, item)
            if selected and item["workflow_id"] != selected["id"]:
                raise Fault("github_invalid_response", "A run belongs to another workflow.")
            for field, expected in filters.items():
                matches = (expected in {item.get("status"), item.get("conclusion")} if field == "status"
                           else item.get("head_branch" if field == "branch" else field) == expected)
                if not matches:
                    raise Fault("github_invalid_response", "A run does not match the requested filters.")
        if action == "deployments" and any(item.get(field) != expected for field, expected in query.items()):
            raise Fault("github_invalid_response", "A deployment does not match the requested filters.")
        if action == "workflow_jobs" and (item.get("run_id") != number or item.get("run_attempt") != filters["attempt"] or item.get("head_sha") != run["head_sha"]):
            raise Fault("github_invalid_response", "A job belongs to another run, attempt or commit.")
    return {"data": [project(kind, item) for item in items], "page": page,
            "next_page": following, "has_more": following is not None, **extra}


def validate_workflow(action, workflow, ref, inputs, number, expected_attempt, expected_head_sha):
    if (action not in {"dispatch", "rerun", "rerun_failed", "cancel"}
            or not isinstance(expected_head_sha, str) or not re.fullmatch(evidence.SHA, expected_head_sha)
            or type(number) is not int or type(expected_attempt) is not int):
        raise Fault("invalid_github_request", "Select a workflow action and exact observed HEAD SHA.")
    if action == "dispatch":
        if (not workflow_selector(workflow) or not valid_ref(ref) or re.fullmatch(evidence.SHA, ref)
                or number != 0 or expected_attempt != 0 or inputs is not None and type(inputs) is not dict):
            raise Fault("invalid_github_request", "Dispatch needs one workflow ID/filename and branch/tag ref, without run controls.")
        values = inputs if inputs is not None else {}
        if (len(values) > 25 or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", key)
                or type(value) not in {str, bool, int, float}
                or type(value) is float and not math.isfinite(value)
                or type(value) is int and abs(value) > MAX_ID for key, value in values.items())):
            raise Fault("invalid_github_request", "Inputs allow at most 25 named finite scalar values, never secrets.")
        encoded = json.dumps(values, allow_nan=False)
        if len(encoded.encode()) > 16384 or redact(encoded) != encoded or any(isinstance(value, str) and "\0" in value for value in values.values()):
            raise Fault("invalid_github_request", "Inputs must fit 16 KiB and contain no credentials or NUL bytes.")
    elif workflow != "" or ref != "" or inputs is not None or not positive(number) or not positive(expected_attempt):
        raise Fault("invalid_github_request", "Run controls need a positive run ID and observed attempt; omit dispatch arguments.")


def workflow(settings, action, repository, workflow="", ref="", inputs=None, number=0,
             expected_attempt=0, expected_head_sha="", *, transport):
    with connection.preflight():
        validate_workflow(action, workflow, ref, inputs, number, expected_attempt, expected_head_sha)
        actor, metadata = connection.access(settings, repository, write=True, transport=transport)
        root = f"repos/{repository}/actions"
        receipt = {"action": action, "expected_head_sha": expected_head_sha}
        if action == "dispatch":
            selected = workflow_metadata(repository, workflow, transport=transport)
            if selected.get("state") != "active":
                raise Fault("github_object_changed", "The selected workflow is not active.")
            commit = object_response(transport("GET", f"repos/{repository}/commits/{quote(ref, safe='')}"))
            if commit.get("sha") != expected_head_sha:
                raise Fault("github_candidate_changed", "The dispatch ref no longer resolves to the observed commit.")
            endpoint, expected_status = f"{root}/workflows/{selected['id']}/dispatches", 200
            payload = {"ref": ref, "inputs": inputs or {}, "return_run_details": True}
            receipt.update(workflow_id=selected["id"], ref=ref)
        else:
            run = run_metadata(repository, number, transport=transport)
            if run["head_sha"] != expected_head_sha or run["run_attempt"] != expected_attempt:
                raise Fault("github_candidate_changed", "The run HEAD or attempt changed; no control was submitted.")
            if (action == "cancel") == (run["status"] == "completed"):
                raise Fault("github_object_changed", "Cancel needs an active run; rerun needs a completed run.")
            suffix = {"rerun": "rerun", "rerun_failed": "rerun-failed-jobs", "cancel": "cancel"}[action]
            endpoint, expected_status, payload = f"{root}/runs/{number}/{suffix}", 202 if action == "cancel" else 201, {}
            receipt.update(run_id=number, workflow_id=run["workflow_id"], expected_attempt=expected_attempt)
        if connection.read_actor(settings, transport=transport) != actor:
            raise Fault("github_identity_changed", "The acting account changed before the workflow request.")
    result = {"repository": repository, "actor": actor, "receipt": receipt,
              "workflow_completion_verified": False}
    if action == "dispatch":
        result["detail"] = "Ref preflight is sequential, not atomic pinning. Releases require a workflow-owned exact-SHA guard before effects. No run ID is inferred."
    else:
        result["observed_run_before_request"] = project("workflow_run", run)
        result["detail"] = "SHA/attempt preflight is sequential, not an atomic provider compare-and-swap. Read the actual run after acknowledgement."
    try:
        response = connection.mutation_request("POST", endpoint, payload, transport=transport)
    except (Fault, OSError) as error:
        if isinstance(error, Fault) and error.effect == "known_no_effect":
            raise
        return {**result, "status": "write_outcome_unknown", "error_code": error.code if isinstance(error, Fault) else "github_response_unknown"}
    if action == "dispatch":
        run_id = response.get("workflow_run_id") if isinstance(response, dict) else None
        run_path = f"{metadata['full_name']}/actions/runs/{run_id}"
        urls = {"run_url": f"https://api.github.com/repos/{run_path}", "html_url": f"https://github.com/{run_path}"}
        if not positive(run_id) or any(response.get(key) != value for key, value in urls.items()):
            return {**result, "status": "write_outcome_unknown", "error_code": "github_response_unknown"}
        receipt.update(run_id=run_id, **urls)
    elif response != {"http_status": expected_status}:
        return {**result, "status": "write_outcome_unknown", "error_code": "github_response_unknown"}
    return {**result, "status": "request_accepted", "http_status": expected_status}
