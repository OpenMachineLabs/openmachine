"""Pinned source, complete PR evidence and current-candidate merge gates."""

import base64
import binascii
import hashlib
from pathlib import Path
import re

from openmachine.foundation.files import excluded
from openmachine.foundation.sanitize import Fault

from . import client
from .connection import object_response


SHA = r"[0-9a-f]{40}"


def pull_request(repository: str, number: int, head: str = "", base: str = "", *, open_only: bool = False, transport) -> dict:
    if number < 1 or (head and not re.fullmatch(SHA, head)) or (base and not re.fullmatch(SHA, base)):
        raise Fault("invalid_github_request", "Use a positive PR number and full observed commit SHAs.")
    value = object_response(transport("GET", f"repos/{repository}/pulls/{number}"))
    if value.get("number") != number:
        raise Fault("github_invalid_response", "GitHub returned a different PR identity.")
    actual_head = object_response(value.get("head")).get("sha", "")
    actual_base = object_response(value.get("base")).get("sha", "")
    if not re.fullmatch(SHA, actual_head) or not re.fullmatch(SHA, actual_base):
        raise Fault("github_invalid_response", "The PR source identities are missing.")
    if ((head and head != actual_head) or (base and base != actual_base)
            or (open_only and value.get("state") != "open")):
        raise Fault("github_candidate_changed", "The PR closed or its inspected HEAD/base changed; read the current candidate.")
    return value


def thread_page(repository: str, number: int, head: str, base: str, cursor: str, limit: int, *, transport) -> tuple[list, str | None]:
    value = client.graphql_pr(repository, number,
                        "reviewThreads(first:$limit,after:$cursor){nodes{id isResolved isOutdated path line "
                        "comments(first:1){nodes{databaseId url}}} pageInfo{endCursor hasNextPage}}", cursor, limit, transport=transport)
    if value.get("headRefOid") != head or value.get("baseRefOid") != base:
        raise Fault("github_candidate_changed", "The PR changed while reading discussions.")
    connection = object_response(value.get("reviewThreads"))
    nodes, page_info = connection.get("nodes"), object_response(connection.get("pageInfo"))
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

def review_thread(repository: str, number: int, head: str, base: str, thread_id: str, *, transport) -> dict:
    cursor = ""
    for _ in range(client.MAX_GATE_PAGES):
        threads, following = thread_page(repository, number, head, base, cursor, 100, transport=transport)
        found = [item for item in threads if item["id"] == thread_id]
        if found:
            return found[0]
        if following is None:
            raise Fault("github_not_found", "The thread does not belong to the inspected pull request.")
        cursor = following
    raise Fault("github_evidence_unavailable", "The thread exceeds the 1,000-discussion verification limit.")


def _checks(repository: str, head: str, *, transport) -> dict:
    runs = client.all_pages(f"repos/{repository}/commits/{head}/check-runs?filter=latest", "check_runs", transport=transport)
    statuses = client.all_pages(f"repos/{repository}/commits/{head}/status", "statuses", transport=transport)
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


def merge_gate(repository: str, number: int, head: str, base: str = "", *, transport) -> dict:
    if not re.fullmatch(SHA, head):
        raise Fault("invalid_github_request", "Read the PR first and supply its exact HEAD SHA.")
    pr = pull_request(repository, number, head, base, open_only=True, transport=transport)
    base = pr["base"]["sha"]
    checks = _checks(repository, head, transport=transport)
    reviews = client.all_pages(f"repos/{repository}/pulls/{number}/reviews", transport=transport)
    unresolved, cursor = [], ""
    for _ in range(client.MAX_GATE_PAGES):
        threads, following = thread_page(repository, number, head, base, cursor, 100, transport=transport)
        unresolved.extend(item["id"] for item in threads if not item["isResolved"])
        if following is None:
            break
        cursor = following
    else:
        raise Fault("github_evidence_unavailable", "More than 1,000 discussions prevent complete gate verification.")
    policy = client.graphql_pr(repository, number, "state isDraft mergeable mergeStateStatus reviewDecision mergeQueueEntry{id}", transport=transport)
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
    pull_request(repository, number, head, base, open_only=True, transport=transport)
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


def read_file(repository: str, ref: str, path: str, offset: int, *, transport) -> dict:
    if (not re.fullmatch(SHA, ref) or not path or path.startswith("/") or "\\" in path or "\0" in path
            or any(part in {"", ".", ".."} for part in path.split("/")) or excluded(Path(path.casefold()))):
        raise Fault("invalid_github_request", "File reads require an exact commit SHA and relative non-credential text path.")
    commit = object_response(transport("GET", f"repos/{repository}/commits/{ref}"))
    if commit.get("sha") != ref:
        raise Fault("github_invalid_response", "The file's commit identity changed.")
    tree_sha = object_response(object_response(commit.get("commit")).get("tree")).get("sha", "")
    parts = path.split("/")
    for index, part in enumerate(parts):
        if not re.fullmatch(SHA, tree_sha):
            raise Fault("github_invalid_response", "The file's tree identity is missing.")
        tree = object_response(transport("GET", f"repos/{repository}/git/trees/{tree_sha}"))
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
    value = object_response(transport("GET", f"repos/{repository}/git/blobs/{tree_sha}"))
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
