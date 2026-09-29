"""Scoped GitHub operations shared by MCP and local callers; gh owns credentials."""

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Literal, get_args
from urllib.parse import quote

from openmachine.config import REPOSITORY_PATTERN, Settings
from openmachine.sanitize import Fault, redact
from openmachine.workspace import excluded

ATTRIBUTION_URL = "https://github.com/OpenMachineLabs/openmachine"
CREATED_FOOTER = f"---\n\nCreated via [Open Machine]({ATTRIBUTION_URL})."
POSTED_FOOTER = f"---\n\nPosted via [Open Machine]({ATTRIBUTION_URL})."
FOOTERS = (CREATED_FOOTER, POSTED_FOOTER)
MAX_API_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_GATE_PAGES = 10
SHA = r"[0-9a-f]{40}"
ReadAction = Literal["status", "repository", "branch", "commit", "file", "issue", "issues",
                     "pull_request", "pull_requests", "files", "comments", "reviews",
                     "review_comments", "threads", "checks", "merge_context"]
DEFINITE_FAILURES = {"github_unavailable", "github_authentication_required", "github_access_denied",
                     "github_not_found", "github_conflict", "github_rejected"}
FIELDS = {
    "repository": "id full_name private default_branch archived permissions html_url",
    "issue": "id number title body state state_reason html_url created_at updated_at closed_at user assignees labels comments pull_request",
    "pull_request": "id number title body state draft html_url created_at updated_at user head base merged merge_commit_sha mergeable mergeable_state commits changed_files comments review_comments",
    "files": "filename previous_filename status sha additions deletions changes patch blob_url",
    "comments": "id body html_url issue_url user created_at updated_at",
    "reviews": "id body html_url user state commit_id submitted_at",
    "review_comments": "id body html_url user path line start_line side original_line commit_id original_commit_id pull_request_review_id in_reply_to_id created_at updated_at",
}


def _append(body: str, footer: str) -> str:
    if footer in body:
        return body
    if not body:
        return footer
    separator = "" if body.endswith("\n\n") else "\n" if body.endswith("\n") else "\n\n"
    return body + separator + footer


def _preserve(current: str, replacement: str) -> str:
    for footer in FOOTERS:
        if footer in current:
            return _append(replacement, footer)
    return replacement


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise Fault("github_invalid_response", "GitHub returned an unexpected object; no result was verified.")
    return value


def _public(value: dict) -> dict:
    def clean(item):
        if isinstance(item, str):
            return redact(item)
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        return item
    result = clean(value)
    result["content_redacted"] = result != value
    if len(json.dumps(result).encode()) > MAX_RESULT_BYTES:
        raise Fault("github_output_too_large", "The observation exceeds 256 KiB after JSON escaping. Reduce the list limit, or use fixed-SHA file pages; this is incomplete evidence.")
    return result


def _project(kind: str, value: dict) -> dict:
    value = _object(value)
    result = {key: value[key] for key in FIELDS[kind].split() if key in value}
    if isinstance(result.get("user"), dict):
        result["user"] = {key: result["user"].get(key) for key in ("id", "login")}
    for key in ("head", "base"):
        if key in result:
            branch = _object(result[key])
            result[key] = {name: branch.get(name) for name in ("sha", "ref", "label")}
            result[key]["repository"] = (branch.get("repo") or {}).get("full_name")
    if "assignees" in result:
        result["assignees"] = [{"id": item.get("id"), "login": item.get("login")} for item in result["assignees"]]
    if "labels" in result:
        result["labels"] = [item.get("name") for item in result["labels"]]
    if kind == "files":
        result["patch_available"] = isinstance(value.get("patch"), str)
        result["patch_is_complete_evidence"] = False
    return result


def _authorize(settings: Settings, repository: str, *, write: bool = True):
    config = settings.github
    if not re.fullmatch(REPOSITORY_PATTERN, repository) or repository.rsplit("/", 1)[-1] in {".", ".."}:
        raise Fault("invalid_github_request", "Use an explicit owner/repository, not a URL or path.")
    if config is None:
        raise Fault("github_not_allowed", "GitHub is not enabled in this Open Machine configuration.")
    repositories = config.repositories + config.write_repositories
    owners = list(config.write_owners)
    if not write:
        repositories += config.read_repositories
        owners += config.read_owners
    if (repository.casefold() not in {item.casefold() for item in repositories}
            and repository.split("/")[0].casefold() not in {item.casefold() for item in owners}):
        raise Fault("github_not_allowed", "The selected repository is outside the configured GitHub read/write scope.")
    return config


def _api(method: str, endpoint: str, payload: dict | None = None):
    # This is the only authenticated request boundary. Never extract gh's token.
    argv = ["gh", "api", "--hostname", "github.com", "--method", method,
            "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28", endpoint]
    data = None
    if payload is not None:
        argv += ["--input", "-"]
        data = json.dumps(payload).encode()
    environment = {key: value for key, value in os.environ.items() if key != "GH_DEBUG"}
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
            result = subprocess.run(argv, input=data, stdout=output, stderr=diagnostic, timeout=30, env=environment)
            output.seek(0)
            raw = output.read(MAX_API_BYTES + 1)
            diagnostic.seek(0)
            error_text = diagnostic.read(16384).decode("utf-8", errors="replace")
    except FileNotFoundError as error:
        raise Fault("github_unavailable", "GitHub CLI could not start; local and SSH operations remain usable.") from error
    except OSError as error:
        raise Fault("github_transport_failed", "GitHub request I/O failed; a dispatched write may have completed. Read its destination before retrying.") from error
    except subprocess.TimeoutExpired as error:
        raise Fault("github_timeout", "GitHub response timed out; a submitted mutation may have completed. Read its destination before retrying.") from error
    if result.returncode:
        status = re.search(r"HTTP (\d{3})", error_text)
        code = int(status[1]) if status else 0
        faults = {401: ("github_authentication_required", "The selected gh identity is not authenticated. Reconnect that account locally."),
                  403: ("github_access_denied", "GitHub denied access or rate-limited the request; check the selected account and grants, not another identity."),
                  404: ("github_not_found", "The GitHub object is absent or not visible to the selected account."),
                  409: ("github_conflict", "GitHub rejected the current candidate or resource state; read it again."),
                  405: ("github_rejected", "GitHub refused the operation under its current repository rules."),
                  422: ("github_rejected", "GitHub rejected the request fields or current resource state.")}
        if code in faults:
            raise Fault(*faults[code])
        if "gh auth login" in error_text:
            raise Fault("github_authentication_required", "No usable gh login is selected; authenticate locally, without sending credentials to OM tools.")
        raise Fault("github_network_unavailable", "No conclusive GitHub response was received; a mutation's outcome is unknown.")
    if len(raw) > MAX_API_BYTES:
        raise Fault("github_response_too_large", "GitHub returned more than 2 MiB; narrow the read. A submitted write still requires destination reconciliation.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise Fault("github_invalid_response", "GitHub returned unreadable JSON; no outcome was verified.") from error
    if not isinstance(value, (dict, list)):
        raise Fault("github_invalid_response", "GitHub returned an unexpected response shape.")
    return value


def _actor(settings: Settings) -> dict:
    if settings.github is None:
        raise Fault("github_not_allowed", "Enable GitHub before probing its connection.")
    value = _object(_api("GET", "user"))
    if not isinstance(value.get("login"), str) or type(value.get("id")) is not int:
        raise Fault("github_invalid_response", "The authenticated GitHub identity could not be verified.")
    if settings.github.account and value["login"].casefold() != settings.github.account.casefold():
        raise Fault("github_identity_changed", "The active gh identity differs from the configured account. Select the intended account locally; OM does not switch accounts.")
    return {"id": value["id"], "login": value["login"]}


def _access(settings: Settings, repository: str, *, write: bool = False) -> tuple[dict, dict]:
    _authorize(settings, repository, write=write)
    actor = _actor(settings)
    metadata = _object(_api("GET", f"repos/{repository}"))
    if str(metadata.get("full_name", "")).casefold() != repository.casefold():
        raise Fault("github_repository_changed", "The resolved repository identity differs from the configured target; review its move or rename locally.")
    return actor, metadata


def _enabled(settings: Settings, repository: str, metadata: dict | None = None) -> bool:
    config = _authorize(settings, repository)
    if config.attribution is not None:
        return config.attribution
    metadata = _object(_api("GET", f"repos/{repository}")) if metadata is None else metadata
    if not isinstance(metadata.get("private"), bool):
        raise Fault("github_invalid_response", "GitHub did not return repository visibility.")
    return not metadata["private"]


def _pr(repository: str, number: int, head: str = "", base: str = "", *, open_only: bool = False) -> dict:
    if number < 1 or (head and not re.fullmatch(SHA, head)) or (base and not re.fullmatch(SHA, base)):
        raise Fault("invalid_github_request", "Use a positive PR number and full observed commit SHAs.")
    value = _object(_api("GET", f"repos/{repository}/pulls/{number}"))
    if value.get("number") != number:
        raise Fault("github_invalid_response", "GitHub returned a different PR identity.")
    actual_head = _object(value.get("head")).get("sha", "")
    actual_base = _object(value.get("base")).get("sha", "")
    if not re.fullmatch(SHA, actual_head) or not re.fullmatch(SHA, actual_base):
        raise Fault("github_invalid_response", "The PR source identities are missing.")
    if ((head and head != actual_head) or (base and base != actual_base)
            or (open_only and value.get("state") != "open")):
        raise Fault("github_candidate_changed", "The PR closed or its inspected HEAD/base changed; read the current candidate.")
    return value


def _list_page(endpoint: str, page: int, limit: int, key: str = "") -> tuple[list, int | None]:
    separator = "&" if "?" in endpoint else "?"
    value = _api("GET", f"{endpoint}{separator}per_page={limit}&page={page}")
    items = _object(value).get(key) if key else value
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise Fault("github_invalid_response", "GitHub pagination returned an incomplete or invalid list.")
    if len(items) > limit:
        raise Fault("github_invalid_response", "GitHub exceeded the requested list limit.")
    if key == "statuses" and value.get("sha") != endpoint.split("/commits/", 1)[1].split("/", 1)[0]:
        raise Fault("github_evidence_unavailable", "Status checks refer to a different commit.")
    total = value.get("total_count") if key else None
    if key and (type(total) is not int or total < 0):
        raise Fault("github_invalid_response", "GitHub returned an invalid list count.")
    if total is not None and len(items) != min(limit, max(0, total - (page - 1) * limit)):
        raise Fault("github_evidence_unavailable", "The returned page does not match its reported count; re-read the changed or incomplete evidence.")
    more = page * limit < total if total is not None else len(items) == limit
    if more and not items:
        raise Fault("github_evidence_unavailable", "GitHub omitted a required page; do not treat missing evidence as empty.")
    return items, page + 1 if more else None


def _all(endpoint: str, key: str = "") -> list:
    items = []
    for page in range(1, MAX_GATE_PAGES + 1):
        batch, following = _list_page(endpoint, page, 100, key)
        items.extend(batch)
        if following is None:
            return items
    raise Fault("github_evidence_unavailable", "The gate exceeds 1,000 records; evidence is incomplete and no review or merge was submitted.")


def _graphql_pr(repository: str, number: int, fields: str, cursor: str = "", limit: int = 100) -> dict:
    owner, name = repository.split("/")
    paging = ", $cursor:String, $limit:Int!" if "$cursor" in fields else ""
    query = ("query($owner:String!, $name:String!, $number:Int!" + paging + "){repository(owner:$owner,name:$name){"
             "pullRequest(number:$number){headRefOid baseRefOid " + fields + "}}}")
    variables = {"owner": owner, "name": name, "number": number}
    if paging:
        variables.update(cursor=cursor or None, limit=limit)
    result = _object(_api("POST", "graphql", {"query": query, "variables": variables}))
    if result.get("errors"):
        raise Fault("github_evidence_unavailable", "GitHub could not verify requested policy/discussion evidence; no partial GraphQL success is accepted.")
    try:
        return _object(result["data"]["repository"]["pullRequest"])
    except (KeyError, TypeError) as error:
        raise Fault("github_evidence_unavailable", "The requested PR policy/discussion evidence is unavailable.") from error


def _thread_page(repository: str, number: int, head: str, base: str, cursor: str, limit: int) -> tuple[list, str | None]:
    value = _graphql_pr(repository, number,
                        "reviewThreads(first:$limit,after:$cursor){nodes{id isResolved isOutdated path line "
                        "comments(first:1){nodes{databaseId url}}} pageInfo{endCursor hasNextPage}}", cursor, limit)
    if value.get("headRefOid") != head or value.get("baseRefOid") != base:
        raise Fault("github_candidate_changed", "The PR changed while reading discussions.")
    connection = _object(value.get("reviewThreads"))
    nodes, page_info = connection.get("nodes"), _object(connection.get("pageInfo"))
    if not isinstance(nodes, list) or type(page_info.get("hasNextPage")) is not bool:
        raise Fault("github_evidence_unavailable", "GitHub did not return complete discussion pagination.")
    for item in nodes:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                or type(item.get("isResolved")) is not bool
                or type(item.get("isOutdated")) is not bool):
            raise Fault("github_evidence_unavailable", "A discussion's resolution state is unknown.")
    next_cursor = page_info.get("endCursor") if page_info["hasNextPage"] else None
    if page_info["hasNextPage"] and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor):
        raise Fault("github_evidence_unavailable", "The discussion continuation cursor is missing or unchanged.")
    return nodes, next_cursor


def _checks(repository: str, head: str) -> dict:
    runs = _all(f"repos/{repository}/commits/{head}/check-runs?filter=latest", "check_runs")
    statuses = _all(f"repos/{repository}/commits/{head}/status", "statuses")
    latest_runs, latest_statuses = {}, {}
    for item in runs:
        if (item.get("head_sha") != head or type(item.get("id")) is not int
                or not isinstance(item.get("name"), str) or type((item.get("app") or {}).get("id")) is not int):
            raise Fault("github_evidence_unavailable", "A check's candidate or producer identity is unknown.")
        key = (item["app"]["id"], item["name"])
        if key not in latest_runs or item["id"] > latest_runs[key]["id"]:
            latest_runs[key] = item
    for item in statuses:
        if not isinstance(item.get("context"), str) or type(item.get("id")) is not int:
            raise Fault("github_evidence_unavailable", "A status context has no verifiable identity.")
        if item["context"] not in latest_statuses or item["id"] > latest_statuses[item["context"]]["id"]:
            latest_statuses[item["context"]] = item
    checks = [{"id": value["id"], "name": value["name"], "app_id": value["app"]["id"],
               "status": value.get("status"), "conclusion": value.get("conclusion"), "html_url": value.get("html_url")}
              for value in latest_runs.values()]
    contexts = [{key: value.get(key) for key in ("id", "context", "state", "target_url")}
                for value in latest_statuses.values()]
    passed = (all(item["status"] == "completed" and item["conclusion"] in {"success", "neutral", "skipped"} for item in checks)
              and all(item["state"] == "success" for item in contexts))
    return {"state": "no_checks" if not checks and not contexts else "passed" if passed else "blocked",
            "check_runs": checks, "statuses": contexts, "complete": True,
            "selection": "latest run ID per app/name; latest status ID per context; native policy is checked separately"}


def _gate(repository: str, number: int, head: str, base: str = "") -> dict:
    if not re.fullmatch(SHA, head):
        raise Fault("invalid_github_request", "Read the PR first and supply its exact HEAD SHA.")
    pr = _pr(repository, number, head, base, open_only=True)
    base = pr["base"]["sha"]
    checks = _checks(repository, head)
    reviews = _all(f"repos/{repository}/pulls/{number}/reviews")
    unresolved, cursor = [], ""
    for _ in range(MAX_GATE_PAGES):
        threads, following = _thread_page(repository, number, head, base, cursor, 100)
        unresolved.extend(item["id"] for item in threads if not item["isResolved"])
        if following is None:
            break
        cursor = following
    else:
        raise Fault("github_evidence_unavailable", "More than 1,000 discussions prevent complete gate verification.")
    policy = _graphql_pr(repository, number, "state isDraft mergeable mergeStateStatus reviewDecision mergeQueueEntry{id}")
    required = {"state", "isDraft", "mergeable", "mergeStateStatus", "reviewDecision", "mergeQueueEntry"}
    if (not required.issubset(policy) or type(policy["isDraft"]) is not bool
            or policy["reviewDecision"] not in {None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}):
        raise Fault("github_evidence_unavailable", "GitHub did not return the native merge policy state.")
    if policy.get("headRefOid") != head or policy.get("baseRefOid") != base:
        raise Fault("github_candidate_changed", "The PR changed while checking merge policy.")
    for review in reviews:
        if (type(review.get("id")) is not int or review["id"] < 1
                or review.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED", "PENDING"}
                or not isinstance(review.get("user"), dict) or type(review["user"].get("id")) is not int):
            raise Fault("github_evidence_unavailable", "A review's identity or state could not be verified.")
    effective, current = {}, []
    for review in sorted(reviews, key=lambda item: item["id"]):
        state = review["state"]
        if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            effective[review["user"]["id"]] = state
        if review.get("commit_id") == head and review.get("submitted_at") and state in {"COMMENTED", "APPROVED"}:
            current.append({key: review.get(key) for key in ("id", "state", "commit_id", "html_url")})
    _pr(repository, number, head, base, open_only=True)
    blockers = []
    if checks["state"] == "blocked":
        blockers.append("current_checks_not_passed")
    if unresolved:
        blockers.append("unresolved_review_threads")
    if "CHANGES_REQUESTED" in effective.values() or policy["reviewDecision"] in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
        blockers.append("review_requirements_not_met")
    if not current:
        blockers.append("no_submitted_current_head_review")
    if (policy["state"] != "OPEN" or policy["isDraft"] or policy["mergeable"] != "MERGEABLE"
            or policy["mergeStateStatus"] != "CLEAN" or policy["mergeQueueEntry"] is not None):
        blockers.append("native_policy_not_clean_or_queue_unsupported")
    return {"head_sha": head, "base_sha": base, "base_ref": pr["base"]["ref"], "complete": True,
            "checks": checks, "policy": policy, "current_head_reviews": current,
            "review_count": len(reviews), "unresolved_thread_ids": unresolved, "blockers": blockers,
            "self_review_is_independent_approval": False,
            "boundary": "Sequential observations, not an atomic snapshot. GitHub enforces live protections at submission."}


def _file(repository: str, ref: str, path: str, offset: int) -> dict:
    if (not re.fullmatch(SHA, ref) or not path or path.startswith("/") or "\\" in path or "\0" in path
            or any(part in {"", ".", ".."} for part in path.split("/")) or excluded(Path(path.casefold()))):
        raise Fault("invalid_github_request", "File reads require an exact commit SHA and relative non-credential text path.")
    commit = _object(_api("GET", f"repos/{repository}/commits/{ref}"))
    if commit.get("sha") != ref:
        raise Fault("github_invalid_response", "The file's commit identity changed.")
    tree_sha = _object(_object(commit.get("commit")).get("tree")).get("sha", "")
    parts = path.split("/")
    for index, part in enumerate(parts):
        if not re.fullmatch(SHA, tree_sha):
            raise Fault("github_invalid_response", "The file's tree identity is missing.")
        tree = _object(_api("GET", f"repos/{repository}/git/trees/{tree_sha}"))
        if tree.get("sha") != tree_sha or tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
            raise Fault("github_evidence_unavailable", "The file's tree listing is incomplete.")
        matches = [item for item in tree["tree"] if isinstance(item, dict) and item.get("path") == part]
        if len(matches) != 1:
            raise Fault("github_not_found", "The requested file path is absent from the pinned Git tree.")
        entry = matches[0]
        allowed_modes = {"100644", "100755"} if index == len(parts) - 1 else {"040000"}
        if entry.get("mode") not in allowed_modes:
            raise Fault("github_unsupported_file", "GitHub readers do not follow symlinks or submodules.")
        tree_sha = entry.get("sha", "")
    if not re.fullmatch(SHA, tree_sha):
        raise Fault("github_invalid_response", "The file's blob identity is missing.")
    value = _object(_api("GET", f"repos/{repository}/git/blobs/{tree_sha}"))
    if value.get("encoding") != "base64" or value.get("sha") != tree_sha:
        raise Fault("github_unsupported_file", "Only complete base64-encoded GitHub blobs are supported.")
    try:
        raw = base64.b64decode(value["content"].replace("\n", ""), validate=True)
        raw.decode("utf-8")
    except (KeyError, AttributeError, ValueError, binascii.Error) as error:
        raise Fault("github_unsupported_file", "The file is not complete UTF-8 content.") from error
    if len(raw) > 1024 * 1024 or b"\0" in raw:
        raise Fault("github_unsupported_file", "GitHub text sources are bounded to 1 MiB and exclude binary files.")
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if value.get("sha") != blob or value.get("size") != len(raw):
        raise Fault("github_invalid_response", "The Git blob identity or size did not match its returned bytes.")
    if not 0 <= offset <= len(raw):
        raise Fault("invalid_range", "The byte offset is outside the file.")
    chunk = raw[offset:offset + 16384]
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.end != len(chunk) or error.reason != "unexpected end of data":
            raise Fault("invalid_range", "Continue at the returned UTF-8 byte offset.") from error
        chunk = chunk[:error.start]
        text = chunk.decode("utf-8")
    return {"path": path, "ref": ref, "blob_sha": blob, "sha256": hashlib.sha256(raw).hexdigest(),
            "text": text, "size_bytes": len(raw), "offset": offset, "next_offset": offset + len(chunk),
            "has_more": offset + len(chunk) < len(raw)}


def read(settings: Settings, action: ReadAction, repository: str = "", number: int = 0,
         ref: str = "", path: str = "", page: int = 1, limit: int = 30, cursor: str = "",
         expected_head_sha: str = "", expected_base_sha: str = "", offset: int = 0) -> dict:
    if action not in get_args(ReadAction) or not 1 <= page <= 1000 or not 1 <= limit <= 100 or len(cursor) > 1024:
        raise Fault("invalid_github_request", "Select a supported read action and bounded pagination.")
    if action == "status" and not repository:
        return _public({"status": "authenticated", "actor": _actor(settings), "backend": "gh",
                        "scopes": settings.github.model_dump(), "repository_access": "not_probed"})
    actor, metadata = _access(settings, repository)
    result = {"repository": metadata["full_name"], "actor": actor, "action": action,
              "observed_at": datetime.now(timezone.utc).isoformat()}
    root = f"repos/{repository}"
    pinned = action in {"files", "reviews", "review_comments", "threads", "merge_context"}
    pr = None
    if pinned:
        if not re.fullmatch(SHA, expected_head_sha):
            raise Fault("invalid_github_request", "Read the PR first; candidate reads require expected_head_sha.")
        pr = _pr(repository, number, expected_head_sha, expected_base_sha)
        result.update(head_sha=pr["head"]["sha"], base_sha=pr["base"]["sha"])
        if (page > 1 or cursor) and not expected_base_sha:
            raise Fault("invalid_github_request", "Echo both HEAD and base SHAs when continuing candidate pages.")
    if action in {"status", "repository"}:
        result["data"] = _project("repository", metadata)
        result["configured_write"] = False
        try:
            _authorize(settings, repository)
            result["configured_write"] = True
        except Fault as error:
            if error.code != "github_not_allowed":
                raise
    elif action == "file":
        result["data"] = _file(repository, ref, path, offset)
    elif action == "branch":
        if not ref or len(ref) > 255 or any(ord(char) < 32 for char in ref):
            raise Fault("invalid_github_request", "Select an explicit branch name.")
        value = _object(_api("GET", f"{root}/branches/{quote(ref, safe='')}"))
        result["data"] = {"name": value.get("name"), "sha": _object(value.get("commit")).get("sha"), "protected": value.get("protected")}
    elif action == "commit":
        if not re.fullmatch(SHA, ref):
            raise Fault("invalid_github_request", "Commit metadata reads require a full SHA.")
        value = _object(_api("GET", f"{root}/commits/{ref}"))
        if value.get("sha") != ref:
            raise Fault("github_invalid_response", "The commit response does not match the requested SHA.")
        commit = _object(value.get("commit"))
        result["data"] = {"sha": ref, "message": commit.get("message"), "tree": commit.get("tree"),
                          "parents": value.get("parents"), "html_url": value.get("html_url"), "file_diffs_included": False}
    elif action in {"issue", "pull_request"}:
        if number < 1:
            raise Fault("invalid_github_request", "Select a positive Issue or PR number.")
        value = (_pr(repository, number, expected_head_sha, expected_base_sha) if action == "pull_request"
                 else _object(_api("GET", f"{root}/issues/{number}")))
        result["data"] = _project(action, value)
    elif action == "merge_context":
        result["data"] = _gate(repository, number, expected_head_sha, expected_base_sha)
    elif action == "threads":
        items, following = _thread_page(repository, number, result["head_sha"], result["base_sha"], cursor, limit)
        result.update(data=items, next_cursor=following, has_more=following is not None,
                      discussion_bodies="Read review_comments pages; thread comments contain only the first-comment locator.")
    elif action == "checks":
        if not re.fullmatch(SHA, ref):
            raise Fault("invalid_github_request", "Check reads require the exact commit SHA.")
        runs, next_runs = _list_page(f"{root}/commits/{ref}/check-runs?filter=latest", page, limit, "check_runs")
        statuses, next_statuses = _list_page(f"{root}/commits/{ref}/status", page, limit, "statuses")
        if any(item.get("head_sha") != ref for item in runs):
            raise Fault("github_evidence_unavailable", "The checks do not match the inspected candidate.")
        result.update(data={"check_runs": [{key: item.get(key) for key in ("id", "name", "head_sha", "status", "conclusion", "html_url", "app")} for item in runs],
                            "statuses": [{key: item.get(key) for key in ("id", "context", "state", "target_url")} for item in statuses]},
                      ref=ref, page=page, next_page=next_runs or next_statuses, has_more=bool(next_runs or next_statuses))
    else:
        if action not in {"issues", "pull_requests"} and number < 1:
            raise Fault("invalid_github_request", "Select a positive Issue or PR number.")
        endpoints = {"issues": f"{root}/issues?state=all&sort=created&direction=desc",
                     "pull_requests": f"{root}/pulls?state=all&sort=created&direction=desc",
                     "comments": f"{root}/issues/{number}/comments", "files": f"{root}/pulls/{number}/files",
                     "reviews": f"{root}/pulls/{number}/reviews", "review_comments": f"{root}/pulls/{number}/comments"}
        items, following = _list_page(endpoints[action], page, limit)
        kind = {"issues": "issue", "pull_requests": "pull_request"}.get(action, action)
        result.update(data=[_project(kind, item) for item in items], page=page, next_page=following, has_more=following is not None)
        if action == "files":
            total = pr.get("changed_files")
            if type(total) is not int:
                raise Fault("github_evidence_unavailable", "The changed-file count is unavailable.")
            expected_count = min(limit, max(0, min(total, 3000) - (page - 1) * limit))
            if len(items) != expected_count:
                raise Fault("github_evidence_unavailable", "The returned files do not cover the requested candidate page.")
            following = page + 1 if page * limit < min(total, 3000) else None
            result.update(next_page=following, has_more=following is not None)
            result.update(changed_files=total, github_file_limit=3000, coverage_gap=total > 3000,
                          patch_warning="API patches can be omitted or incomplete. Use base/head file pages or the existing native Git execution for full relevant diffs.")
    if pr is not None:
        _pr(repository, number, result["head_sha"], result["base_sha"])
    return _public(result)


def _receipt(result: dict) -> dict:
    return {key: result[key] for key in ("id", "number", "html_url", "state", "submitted_at", "updated_at", "commit_id", "merged", "merge_commit_sha") if key in result}


def _mutate(repository: str, method: str, endpoint: str, payload: dict, readback,
            *, settings: Settings | None = None, actor: dict | None = None) -> dict:
    try:
        result = _object(_api(method, endpoint, payload))
    except Fault as error:
        if error.code in DEFINITE_FAILURES:
            raise
        return {"status": "write_outcome_unknown", "repository": repository, "endpoint": endpoint,
                "error_code": error.code, "detail": "No conclusive write response. Read the original destination before any retry."}
    receipt = _receipt(result)
    if not any(type(result.get(key)) is int and result[key] > 0 for key in ("id", "number")):
        return {"status": "write_unverified", "repository": repository, "receipt": receipt,
                "detail": "The write response has no usable object identity; reconcile its destination before retrying."}
    try:
        observed = _object(_api("GET", readback(result)))
        matches = all(observed.get(key) == result[key] for key in ("id", "number") if key in result)
        for key, expected in payload.items():
            if key in {"head", "base"}:
                branch = _object(observed.get(key))
                matches &= expected in {branch.get("ref"), branch.get("label")}
            elif key == "event":
                matches &= observed.get("state") == {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[expected]
            else:
                matches &= observed.get(key) == expected
        if settings is not None:
            matches &= _actor(settings) == actor
    except (Fault, KeyError, TypeError) as error:
        return {"status": "write_unverified", "repository": repository, "receipt": receipt,
                "detail": "A write response was received, but destination/identity readback is incomplete. Do not replay."}
    if not matches:
        return {"status": "write_unverified", "repository": repository, "receipt": receipt,
                "detail": "Destination fields, object identity, or acting account did not match. Read before retrying."}
    return {"status": "verified", "repository": repository, "actor": actor, "receipt": _receipt(observed) or receipt,
            "verified_fields": list(payload), "fields_verified": True, "body_verified": "body" not in payload or observed.get("body") == payload["body"]}


def _update_fields(current: dict, title: str | None, body: str | None, state: str | None, expected_updated_at: str) -> dict:
    if expected_updated_at and current.get("updated_at") != expected_updated_at:
        raise Fault("github_object_changed", "The object changed since the supplied observation; read before replacing its fields.")
    if state is not None and state not in {"open", "closed"}:
        raise Fault("invalid_github_request", "Issue and PR state must be open or closed.")
    payload = {key: value for key, value in (("title", title), ("state", state)) if value is not None}
    if body is not None:
        payload["body"] = _preserve(current.get("body") or "", body)
    if not payload:
        raise Fault("invalid_github_request", "An update requires at least one field.")
    return payload


def issue(settings: Settings, action: Literal["create", "update"], repository: str,
          title: str | None, body: str | None, number: int = 0,
          state: Literal["open", "closed"] | None = None, expected_updated_at: str = "") -> dict:
    actor, metadata = _access(settings, repository, write=True)
    root = f"repos/{repository}/issues"
    if action == "create":
        if not title or state is not None:
            raise Fault("invalid_github_request", "Creating an Issue requires a title; state is update-only.")
        submitted = _append(body or "", CREATED_FOOTER) if _enabled(settings, repository, metadata) else body or ""
        return _mutate(repository, "POST", root, {"title": title, "body": submitted},
                       lambda result: f"{root}/{result['number']}", settings=settings, actor=actor)
    if action != "update" or number < 1:
        raise Fault("invalid_github_request", "Updating an Issue requires its positive number.")
    current = _object(_api("GET", f"{root}/{number}"))
    if "pull_request" in current:
        raise Fault("github_wrong_object_type", "This number is a Pull Request; use github_pull_request with its candidate checks.")
    payload = _update_fields(current, title, body, state, expected_updated_at)
    return _mutate(repository, "PATCH", f"{root}/{number}", payload, lambda _: f"{root}/{number}", settings=settings, actor=actor)


def pull_request(settings: Settings, action: Literal["create", "update"], repository: str,
                 title: str | None, body: str | None, number: int = 0,
                 head: str = "", base: str = "", draft: bool = False,
                 state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                 expected_head_sha: str = "") -> dict:
    actor, metadata = _access(settings, repository, write=True)
    root = f"repos/{repository}/pulls"
    if action == "create":
        if not title or not head or not base or head == base or state is not None:
            raise Fault("invalid_github_request", "Creating a PR requires title and distinct head/base; state is update-only.")
        if expected_head_sha:
            if not re.fullmatch(SHA, expected_head_sha) or ":" in head:
                raise Fault("invalid_github_request", "HEAD-pinned creation requires a same-repository branch and full SHA.")
            branch = _object(_api("GET", f"repos/{repository}/branches/{quote(head, safe='')}"))
            if _object(branch.get("commit")).get("sha") != expected_head_sha:
                raise Fault("github_candidate_changed", "The pushed branch changed before PR creation.")
        submitted = _append(body or "", CREATED_FOOTER) if _enabled(settings, repository, metadata) else body or ""
        payload = {"title": title, "body": submitted, "head": head, "base": base, "draft": draft}
        result = _mutate(repository, "POST", root, payload, lambda value: f"{root}/{value['number']}", settings=settings, actor=actor)
        if result["status"] == "verified" and expected_head_sha:
            try:
                _pr(repository, result["receipt"]["number"], expected_head_sha)
            except Fault:
                result.update(status="write_unverified", detail="The PR exists, but its candidate does not match the requested HEAD. Read it; do not create another PR.")
        return result
    if action != "update" or number < 1:
        raise Fault("invalid_github_request", "Updating a PR requires its positive number.")
    current = _pr(repository, number, expected_head_sha)
    payload = _update_fields(current, title, body, state, expected_updated_at)
    return _mutate(repository, "PATCH", f"{root}/{number}", payload, lambda _: f"{root}/{number}", settings=settings, actor=actor)


def comment(settings: Settings, action: Literal["create", "update"], repository: str,
            body: str, number: int = 0, comment_id: int = 0, expected_updated_at: str = "") -> dict:
    actor, metadata = _access(settings, repository, write=True)
    if action == "create":
        if number < 1:
            raise Fault("invalid_github_request", "Posting a comment requires an Issue or PR number.")
        _object(_api("GET", f"repos/{repository}/issues/{number}"))
        submitted = _append(body, POSTED_FOOTER) if _enabled(settings, repository, metadata) else body
        endpoint = f"repos/{repository}/issues/{number}/comments"
        return _mutate(repository, "POST", endpoint, {"body": submitted},
                       lambda result: f"repos/{repository}/issues/comments/{result['id']}", settings=settings, actor=actor)
    if action != "update" or comment_id < 1:
        raise Fault("invalid_github_request", "Updating a comment requires its positive ID.")
    endpoint = f"repos/{repository}/issues/comments/{comment_id}"
    current = _object(_api("GET", endpoint))
    payload = _update_fields(current, None, body, None, expected_updated_at)
    return _mutate(repository, "PATCH", endpoint, payload, lambda _: endpoint, settings=settings, actor=actor)


def review(settings: Settings, action: Literal["create", "update"], repository: str,
           body: str, number: int = 0, review_id: int = 0, expected_head_sha: str = "",
           event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT") -> dict:
    actor, metadata = _access(settings, repository, write=True)
    if action == "create":
        if number < 1 or not re.fullmatch(SHA, expected_head_sha) or event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise Fault("invalid_github_request", "Creating a review requires a positive PR number, exact HEAD and supported event.")
        current = _pr(repository, number, expected_head_sha, open_only=True)
        if event != "COMMENT" and (current.get("user") or {}).get("id") == actor["id"]:
            raise Fault("github_self_review", "GitHub does not permit self-approval or self-requested changes; submit COMMENT without claiming independent approval.")
        # A review may report failed checks. Missing evidence, unlike a known failure, rejects submission.
        _gate(repository, number, expected_head_sha, current["base"]["sha"])
        submitted = _append(body, POSTED_FOOTER) if _enabled(settings, repository, metadata) else body
        endpoint = f"repos/{repository}/pulls/{number}/reviews"
        return _mutate(repository, "POST", endpoint, {"commit_id": expected_head_sha, "event": event, "body": submitted},
                       lambda result: f"{endpoint}/{result['id']}", settings=settings, actor=actor)
    if action != "update" or number < 1 or review_id < 1:
        raise Fault("invalid_github_request", "Updating a review requires positive PR and review IDs.")
    endpoint = f"repos/{repository}/pulls/{number}/reviews/{review_id}"
    current = _object(_api("GET", endpoint))
    return _mutate(repository, "PUT", endpoint, {"body": _preserve(current.get("body") or "", body)},
                   lambda _: endpoint, settings=settings, actor=actor)


def merge(settings: Settings, repository: str, number: int, expected_head_sha: str,
          expected_base_sha: str = "", method: Literal["squash", "merge", "rebase"] = "squash",
          commit_title: str = "", commit_message: str = "") -> dict:
    actor, _ = _access(settings, repository, write=True)
    if method not in {"squash", "merge", "rebase"}:
        raise Fault("invalid_github_request", "Select squash, merge or rebase; queues and auto-merge are not implemented.")
    gate = _gate(repository, number, expected_head_sha, expected_base_sha)
    if gate["blockers"]:
        return _public({"status": "merge_blocked", "repository": repository, "actor": actor, "gate": gate})
    if _actor(settings) != actor:
        raise Fault("github_identity_changed", "The active gh account changed before merge; no merge was submitted.")
    payload = {"sha": expected_head_sha, "merge_method": method}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message
    endpoint = f"repos/{repository}/pulls/{number}/merge"
    try:
        result = _object(_api("PUT", endpoint, payload))
    except Fault as error:
        if error.code in DEFINITE_FAILURES:
            raise
        return {"status": "merge_outcome_unknown", "repository": repository, "number": number,
                "expected_head_sha": expected_head_sha, "detail": "Read the original PR and destination; never replay an uncertain merge."}
    receipt = {"status": "merge_unverified", "repository": repository, "number": number,
               "actor": actor, "head_sha": expected_head_sha, "base_sha": gate["base_sha"],
               "merge_commit_sha": result.get("sha"), "method": method}
    try:
        merged = _pr(repository, number, expected_head_sha)
        sha = result.get("sha", "")
        if result.get("merged") is not True or not re.fullmatch(SHA, sha) or merged.get("merged") is not True or merged.get("merge_commit_sha") != sha:
            return receipt
        commit = _object(_api("GET", f"repos/{repository}/commits/{sha}"))
        branch = _object(_api("GET", f"repos/{repository}/branches/{quote(gate['base_ref'], safe='')}"))
        tip = _object(branch.get("commit")).get("sha", "")
        if not re.fullmatch(SHA, tip):
            return receipt
        comparison = _object(_api("GET", f"repos/{repository}/compare/{sha}...{tip}"))
        if (commit.get("sha") != sha or comparison.get("status") not in {"ahead", "identical"}
                or (comparison.get("merge_base_commit") or {}).get("sha") != sha or _actor(settings) != actor):
            return receipt
        receipt.update(status="merged_verified", base_ref=gate["base_ref"], observed_base_tip=tip,
                       html_url=merged.get("html_url"), destination_verified=True,
                       checks_state=gate["checks"]["state"], self_review_is_independent_approval=False)
    except (Fault, KeyError, TypeError):
        return receipt
    return receipt
