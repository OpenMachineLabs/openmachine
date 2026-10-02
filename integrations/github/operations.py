"""Scoped collaboration, provenance and destination reconciliation."""

from datetime import datetime, timezone
import json
import re
from typing import Literal, get_args
from urllib.parse import quote

from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault, redact

from . import client, connection, evidence
from .connection import object_response


ATTRIBUTION_URL = "https://github.com/OpenMachineLabs/openmachine"
CREATED_FOOTER = f"---\n\nCreated via [Open Machine]({ATTRIBUTION_URL})."
POSTED_FOOTER = f"---\n\nPosted via [Open Machine]({ATTRIBUTION_URL})."
FOOTERS = (CREATED_FOOTER, POSTED_FOOTER)
MAX_RESULT_BYTES = 256 * 1024
ReadAction = Literal["status", "repository", "branch", "commit", "file", "issue", "issues",
                     "pull_request", "pull_requests", "files", "comments", "reviews",
                     "review_comments", "threads", "checks", "merge_context"]
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
    value = object_response(value)
    result = {key: value[key] for key in FIELDS[kind].split() if key in value}
    if isinstance(result.get("user"), dict):
        result["user"] = {key: result["user"].get(key) for key in ("id", "login")}
    for key in ("head", "base"):
        if key in result:
            branch = object_response(result[key])
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


def _enabled(settings: Settings, repository: str, metadata: dict | None = None) -> bool:
    return connection.authorize(settings, repository).attribution


def read(settings: Settings, action: ReadAction, repository: str = "", number: int = 0,
         ref: str = "", path: str = "", page: int = 1, limit: int = 30, cursor: str = "",
         expected_head_sha: str = "", expected_base_sha: str = "", offset: int = 0, *, transport=None) -> dict:
    if action not in get_args(ReadAction) or not 1 <= page <= 1000 or not 1 <= limit <= 100 or len(cursor) > 1024:
        raise Fault("invalid_github_request", "Select a supported read action and bounded pagination.")
    if action == "status" and not repository:
        return _public({"status": "authenticated", "actor": connection.read_actor(settings, transport=transport), "backend": "gh",
                        "scopes": settings.github.model_dump(), "repository_access": "not_probed"})
    actor, metadata = connection.access(settings, repository, transport=transport)
    result = {"repository": metadata["full_name"], "actor": actor, "action": action,
              "observed_at": datetime.now(timezone.utc).isoformat()}
    root = f"repos/{repository}"
    pinned = action in {"files", "reviews", "review_comments", "threads", "merge_context"}
    pr = None
    if pinned:
        if not re.fullmatch(evidence.SHA, expected_head_sha):
            raise Fault("invalid_github_request", "Read the PR first; candidate reads require expected_head_sha.")
        pr = evidence.pull_request(repository, number, expected_head_sha, expected_base_sha, transport=transport)
        result.update(head_sha=pr["head"]["sha"], base_sha=pr["base"]["sha"])
        if (page > 1 or cursor) and not expected_base_sha:
            raise Fault("invalid_github_request", "Echo both HEAD and base SHAs when continuing candidate pages.")
    if action in {"status", "repository"}:
        result["data"] = _project("repository", metadata)
        result["configured_write"] = False
        try:
            connection.authorize(settings, repository)
            result["configured_write"] = True
        except Fault as error:
            if error.code != "github_not_allowed":
                raise
    elif action == "file":
        result["data"] = evidence.read_file(repository, ref, path, offset, transport=transport)
    elif action == "branch":
        if not ref or len(ref) > 255 or any(ord(char) < 32 for char in ref):
            raise Fault("invalid_github_request", "Select an explicit branch name.")
        value = object_response((transport or connection.request)("GET", f"{root}/branches/{quote(ref, safe='')}"))
        result["data"] = {"name": value.get("name"), "sha": object_response(value.get("commit")).get("sha"), "protected": value.get("protected")}
    elif action == "commit":
        if not re.fullmatch(evidence.SHA, ref):
            raise Fault("invalid_github_request", "Commit metadata reads require a full SHA.")
        value = object_response((transport or connection.request)("GET", f"{root}/commits/{ref}"))
        if value.get("sha") != ref:
            raise Fault("github_invalid_response", "The commit response does not match the requested SHA.")
        commit = object_response(value.get("commit"))
        result["data"] = {"sha": ref, "message": commit.get("message"), "tree": commit.get("tree"),
                          "parents": value.get("parents"), "html_url": value.get("html_url"), "file_diffs_included": False}
    elif action in {"issue", "pull_request"}:
        if number < 1:
            raise Fault("invalid_github_request", "Select a positive Issue or PR number.")
        value = (evidence.pull_request(repository, number, expected_head_sha, expected_base_sha, transport=transport) if action == "pull_request"
                 else object_response((transport or connection.request)("GET", f"{root}/issues/{number}")))
        result["data"] = _project(action, value)
    elif action == "merge_context":
        result["data"] = evidence.merge_gate(repository, number, expected_head_sha, expected_base_sha, transport=transport)
    elif action == "threads":
        items, following = evidence.thread_page(repository, number, result["head_sha"], result["base_sha"], cursor, limit, transport=transport)
        result.update(data=items, next_cursor=following, has_more=following is not None,
                      discussion_bodies="Read review_comments pages; thread comments contain only the first-comment locator.")
    elif action == "checks":
        if not re.fullmatch(evidence.SHA, ref):
            raise Fault("invalid_github_request", "Check reads require the exact commit SHA.")
        runs, next_runs = client.list_page(f"{root}/commits/{ref}/check-runs?filter=latest", page, limit, "check_runs", transport=transport)
        statuses, next_statuses = client.list_page(f"{root}/commits/{ref}/status", page, limit, "statuses", transport=transport)
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
        items, following = client.list_page(endpoints[action], page, limit, transport=transport)
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
        evidence.pull_request(repository, number, result["head_sha"], result["base_sha"], transport=transport)
    return _public(result)


def _merge_destination(root: str, sha: str, base_ref: str, *, transport=None) -> tuple[dict, str] | None:
    """Verify the original commit in the destination without any mutation."""
    commit = object_response((transport or connection.request)("GET", f"{root}/commits/{sha}"))
    branch = object_response((transport or connection.request)("GET", f"{root}/branches/{quote(base_ref, safe='')}"))
    tip = object_response(branch.get("commit")).get("sha", "")
    if not re.fullmatch(evidence.SHA, tip):
        return None
    comparison = object_response((transport or connection.request)("GET", f"{root}/compare/{sha}...{tip}"))
    if (commit.get("sha") != sha or comparison.get("status") not in {"ahead", "identical"}
            or (comparison.get("merge_base_commit") or {}).get("sha") != sha):
        return None
    return commit, tip


def reconcile(settings: Settings, locator: dict, request: dict | None = None, *, transport=None) -> dict:
    """Bounded original-destination observation. Never resubmit a publication."""
    kind, repository = locator.get("object_type"), locator.get("repository", "")
    number, parent, anchor = locator.get("number"), locator.get("parent_number"), locator.get("anchor", "")
    if (kind not in {"issue", "pull_request", "comment", "review", "merge"}
            or not isinstance(anchor, str) or not re.fullmatch(r"om-task:[A-Za-z0-9][A-Za-z0-9_-]{0,63}:[0-9a-f]{32}", anchor)
            or any(value is not None and (type(value) is not int or value < 1) for value in (number, parent))):
        raise Fault("invalid_github_request", "Reconciliation requires the original bounded publication locator.")
    actor, _ = connection.access(settings, repository, transport=transport)
    root, marker = f"repos/{repository}", f"<!-- {anchor} -->"
    unknown = {"status": "write_outcome_unknown", "repository": repository,
               "detail": "Original destination is unresolved; no publication was repeated."}
    if kind == "merge":
        if number is None:
            return unknown
        value = evidence.pull_request(repository, number, (request or {}).get("expected_head_sha", ""), transport=transport)
        sha = value.get("merge_commit_sha", "")
        if value.get("merged") is not True or not isinstance(sha, str) or not re.fullmatch(evidence.SHA, sha):
            return unknown
        head = object_response(value.get("head")).get("sha")
        base_ref = object_response(value.get("base")).get("ref")
        if not isinstance(base_ref, str) or not isinstance(head, str) or not re.fullmatch(evidence.SHA, head):
            return unknown
        destination = _merge_destination(root, sha, base_ref, transport=transport)
        if destination is None:
            return unknown
        commit, _ = destination
        message = object_response(commit.get("commit")).get("message", "")
        if request is not None and (
                request.get("commit_title") and message.split("\n", 1)[0] != request["commit_title"]
                or request.get("commit_message") and message.partition("\n\n")[2].rstrip("\n") != request["commit_message"].rstrip("\n")):
            return unknown
        if connection.read_actor(settings, transport=transport) != actor:
            raise Fault("github_identity_changed", "The acting account changed during original-result observation.")
        return {"status": "merged_verified" if request is not None else "observed", "repository": repository,
                "receipt": {"number": number, "head_sha": head, "merge_commit_sha": sha}, "destination_verified": True}
    if parent is None and (kind == "review" or kind == "comment" and number is None):
        return unknown
    collection = {"issue": f"{root}/issues?state=all&sort=created&direction=desc",
                  "pull_request": f"{root}/pulls?state=all&sort=created&direction=desc",
                  "comment": f"{root}/issues/{parent}/comments", "review": f"{root}/pulls/{parent}/reviews"}[kind]
    coverage = "exact"
    if number is None:
        found = []
        following = 1
        for page in range(1, 4):
            items, following = client.list_page(collection, page, 100, transport=transport)
            found.extend(item for item in items if marker in (item.get("body") or "")
                         and not (kind == "issue" and "pull_request" in item))
            if following is None:
                break
        coverage = "incomplete" if following is not None else "complete"
        if len(found) != 1 or following is not None:
            return {**unknown, "coverage": coverage, "matching_destinations": len(found)}
        number = found[0].get("number" if kind in {"issue", "pull_request"} else "id")
        if type(number) is not int or number < 1:
            return unknown
    endpoint = {"issue": f"{root}/issues/{number}", "pull_request": f"{root}/pulls/{number}",
                "comment": f"{root}/issues/comments/{number}", "review": f"{root}/pulls/{parent}/reviews/{number}"}[kind]
    value = object_response((transport or connection.request)("GET", endpoint))
    identity = "number" if kind in {"issue", "pull_request"} else "id"
    if value.get(identity) != number or kind == "issue" and "pull_request" in value:
        raise Fault("github_wrong_object_type", "The original locator did not resolve to the expected publication.")
    if kind == "comment" and parent is not None and not str(value.get("issue_url", "")).endswith(f"/issues/{parent}"):
        raise Fault("github_wrong_object_type", "The original comment belongs to another parent.")
    matches = request is not None and marker in (value.get("body") or "")
    if request is not None:
        if request.get("action") == "create":
            matches &= (value.get("user") or {}).get("id") == actor["id"]
        for key in ("title", "state"):
            if request.get(key) is not None:
                matches &= value.get(key) == request[key]
        if request.get("body") is not None:
            body = request["body"]
            matches &= value.get("body") in {body, *(_append(body, footer) for footer in FOOTERS)}
        if kind == "pull_request":
            if request.get("action") == "create":
                matches &= value.get("draft") == request.get("draft", False)
                for key in ("head", "base"):
                    branch = object_response(value.get(key))
                    matches &= request[key] in {branch.get("ref"), branch.get("label")}
            if request.get("expected_head_sha"):
                matches &= object_response(value.get("head")).get("sha") == request["expected_head_sha"]
        if kind == "review" and request.get("action") == "create":
            matches &= value.get("commit_id") == request.get("expected_head_sha")
            matches &= value.get("state") == {"COMMENT": "COMMENTED", "APPROVE": "APPROVED",
                                              "REQUEST_CHANGES": "CHANGES_REQUESTED"}.get(request.get("event"))
    if connection.read_actor(settings, transport=transport) != actor:
        raise Fault("github_identity_changed", "The acting account changed during original-result observation.")
    return _public({"status": "verified" if matches else "observed", "repository": repository, "actor": actor,
                    "receipt": _receipt(value), "fields_verified": bool(matches), "coverage": coverage})


def _receipt(result: dict) -> dict:
    return {key: result[key] for key in ("id", "number", "html_url", "state", "submitted_at", "updated_at", "commit_id", "merged", "merge_commit_sha") if key in result}


def _mutate(repository: str, method: str, endpoint: str, payload: dict, readback,
            *, settings: Settings | None = None, actor: dict | None = None, transport=None) -> dict:
    try:
        result = object_response((transport or connection.request)(method, endpoint, payload))
    except Fault as error:
        if error.code in connection.DEFINITE_FAILURES:
            raise
        return {"status": "write_outcome_unknown", "repository": repository, "endpoint": endpoint,
                "error_code": error.code, "detail": "No conclusive write response. Read the original destination before any retry."}
    receipt = _receipt(result)
    if not any(type(result.get(key)) is int and result[key] > 0 for key in ("id", "number")):
        return {"status": "write_unverified", "repository": repository, "receipt": receipt,
                "detail": "The write response has no usable object identity; reconcile its destination before retrying."}
    try:
        observed = object_response((transport or connection.request)("GET", readback(result)))
        matches = all(observed.get(key) == result[key] for key in ("id", "number") if key in result)
        for key, expected in payload.items():
            if key in {"head", "base"}:
                branch = object_response(observed.get(key))
                matches &= expected in {branch.get("ref"), branch.get("label")}
            elif key == "event":
                matches &= observed.get("state") == {"COMMENT": "COMMENTED", "APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}[expected]
            else:
                matches &= observed.get(key) == expected
        if settings is not None:
            matches &= connection.read_actor(settings, transport=transport) == actor
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
          state: Literal["open", "closed"] | None = None, expected_updated_at: str = "", *, transport=None) -> dict:
    actor, metadata = connection.access(settings, repository, write=True, transport=transport)
    root = f"repos/{repository}/issues"
    if action == "create":
        if not title or state is not None:
            raise Fault("invalid_github_request", "Creating an Issue requires a title; state is update-only.")
        submitted = _append(body or "", CREATED_FOOTER) if _enabled(settings, repository, metadata) else body or ""
        return _mutate(repository, "POST", root, {"title": title, "body": submitted},
                       lambda result: f"{root}/{result['number']}", settings=settings, actor=actor, transport=transport)
    if action != "update" or number < 1:
        raise Fault("invalid_github_request", "Updating an Issue requires its positive number.")
    current = object_response((transport or connection.request)("GET", f"{root}/{number}"))
    if "pull_request" in current:
        raise Fault("github_wrong_object_type", "This number is a Pull Request; use github_pull_request with its candidate checks.")
    payload = _update_fields(current, title, body, state, expected_updated_at)
    return _mutate(repository, "PATCH", f"{root}/{number}", payload, lambda _: f"{root}/{number}", settings=settings, actor=actor, transport=transport)


def pull_request(settings: Settings, action: Literal["create", "update"], repository: str,
                 title: str | None, body: str | None, number: int = 0,
                 head: str = "", base: str = "", draft: bool = False,
                 state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                 expected_head_sha: str = "", *, transport=None) -> dict:
    actor, metadata = connection.access(settings, repository, write=True, transport=transport)
    root = f"repos/{repository}/pulls"
    if action == "create":
        if not title or not head or not base or head == base or state is not None:
            raise Fault("invalid_github_request", "Creating a PR requires title and distinct head/base; state is update-only.")
        if expected_head_sha:
            if not re.fullmatch(evidence.SHA, expected_head_sha) or ":" in head:
                raise Fault("invalid_github_request", "HEAD-pinned creation requires a same-repository branch and full SHA.")
            branch = object_response((transport or connection.request)("GET", f"repos/{repository}/branches/{quote(head, safe='')}"))
            if object_response(branch.get("commit")).get("sha") != expected_head_sha:
                raise Fault("github_candidate_changed", "The pushed branch changed before PR creation.")
        submitted = _append(body or "", CREATED_FOOTER) if _enabled(settings, repository, metadata) else body or ""
        payload = {"title": title, "body": submitted, "head": head, "base": base, "draft": draft}
        result = _mutate(repository, "POST", root, payload, lambda value: f"{root}/{value['number']}", settings=settings, actor=actor, transport=transport)
        if result["status"] == "verified" and expected_head_sha:
            try:
                evidence.pull_request(repository, result["receipt"]["number"], expected_head_sha, transport=transport)
            except Fault:
                result.update(status="write_unverified", detail="The PR exists, but its candidate does not match the requested HEAD. Read it; do not create another PR.")
        return result
    if action != "update" or number < 1:
        raise Fault("invalid_github_request", "Updating a PR requires its positive number.")
    current = evidence.pull_request(repository, number, expected_head_sha, transport=transport)
    payload = _update_fields(current, title, body, state, expected_updated_at)
    return _mutate(repository, "PATCH", f"{root}/{number}", payload, lambda _: f"{root}/{number}", settings=settings, actor=actor, transport=transport)


def comment(settings: Settings, action: Literal["create", "update"], repository: str,
            body: str, number: int = 0, comment_id: int = 0, expected_updated_at: str = "", *, transport=None) -> dict:
    actor, metadata = connection.access(settings, repository, write=True, transport=transport)
    if action == "create":
        if number < 1:
            raise Fault("invalid_github_request", "Posting a comment requires an Issue or PR number.")
        object_response((transport or connection.request)("GET", f"repos/{repository}/issues/{number}"))
        submitted = _append(body, POSTED_FOOTER) if _enabled(settings, repository, metadata) else body
        endpoint = f"repos/{repository}/issues/{number}/comments"
        return _mutate(repository, "POST", endpoint, {"body": submitted},
                       lambda result: f"repos/{repository}/issues/comments/{result['id']}", settings=settings, actor=actor, transport=transport)
    if action != "update" or comment_id < 1:
        raise Fault("invalid_github_request", "Updating a comment requires its positive ID.")
    endpoint = f"repos/{repository}/issues/comments/{comment_id}"
    current = object_response((transport or connection.request)("GET", endpoint))
    payload = _update_fields(current, None, body, None, expected_updated_at)
    return _mutate(repository, "PATCH", endpoint, payload, lambda _: endpoint, settings=settings, actor=actor, transport=transport)


def review(settings: Settings, action: Literal["create", "update"], repository: str,
           body: str, number: int = 0, review_id: int = 0, expected_head_sha: str = "",
           event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT", *, transport=None) -> dict:
    actor, metadata = connection.access(settings, repository, write=True, transport=transport)
    if action == "create":
        if number < 1 or not re.fullmatch(evidence.SHA, expected_head_sha) or event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise Fault("invalid_github_request", "Creating a review requires a positive PR number, exact HEAD and supported event.")
        current = evidence.pull_request(repository, number, expected_head_sha, open_only=True, transport=transport)
        if event != "COMMENT" and (current.get("user") or {}).get("id") == actor["id"]:
            raise Fault("github_self_review", "GitHub does not permit self-approval or self-requested changes; submit COMMENT without claiming independent approval.")
        # A review may report failed checks. Missing evidence, unlike a known failure, rejects submission.
        evidence.merge_gate(repository, number, expected_head_sha, current["base"]["sha"], transport=transport)
        submitted = _append(body, POSTED_FOOTER) if _enabled(settings, repository, metadata) else body
        endpoint = f"repos/{repository}/pulls/{number}/reviews"
        return _mutate(repository, "POST", endpoint, {"commit_id": expected_head_sha, "event": event, "body": submitted},
                       lambda result: f"{endpoint}/{result['id']}", settings=settings, actor=actor, transport=transport)
    if action != "update" or number < 1 or review_id < 1:
        raise Fault("invalid_github_request", "Updating a review requires positive PR and review IDs.")
    endpoint = f"repos/{repository}/pulls/{number}/reviews/{review_id}"
    current = object_response((transport or connection.request)("GET", endpoint))
    return _mutate(repository, "PUT", endpoint, {"body": _preserve(current.get("body") or "", body)},
                   lambda _: endpoint, settings=settings, actor=actor, transport=transport)


def merge(settings: Settings, repository: str, number: int, expected_head_sha: str,
          expected_base_sha: str = "", method: Literal["squash", "merge", "rebase"] = "squash",
          commit_title: str = "", commit_message: str = "", *, transport=None) -> dict:
    actor, _ = connection.access(settings, repository, write=True, transport=transport)
    if method not in {"squash", "merge", "rebase"}:
        raise Fault("invalid_github_request", "Select squash, merge or rebase; queues and auto-merge are not implemented.")
    gate = evidence.merge_gate(repository, number, expected_head_sha, expected_base_sha, transport=transport)
    if gate["blockers"]:
        return _public({"status": "merge_blocked", "repository": repository, "actor": actor, "gate": gate})
    if connection.read_actor(settings, transport=transport) != actor:
        raise Fault("github_identity_changed", "The active gh account changed before merge; no merge was submitted.")
    payload = {"sha": expected_head_sha, "merge_method": method}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message
    endpoint = f"repos/{repository}/pulls/{number}/merge"
    try:
        result = object_response((transport or connection.request)("PUT", endpoint, payload))
    except Fault as error:
        if error.code in connection.DEFINITE_FAILURES:
            raise
        return {"status": "merge_outcome_unknown", "repository": repository, "number": number,
                "expected_head_sha": expected_head_sha, "detail": "Read the original PR and destination; never replay an uncertain merge."}
    receipt = {"status": "merge_unverified", "repository": repository, "number": number,
               "actor": actor, "head_sha": expected_head_sha, "base_sha": gate["base_sha"],
               "merge_commit_sha": result.get("sha"), "method": method}
    try:
        merged = evidence.pull_request(repository, number, expected_head_sha, transport=transport)
        sha = result.get("sha", "")
        if result.get("merged") is not True or not re.fullmatch(evidence.SHA, sha) or merged.get("merged") is not True or merged.get("merge_commit_sha") != sha:
            return receipt
        destination = _merge_destination(f"repos/{repository}", sha, gate["base_ref"], transport=transport)
        if destination is None or connection.read_actor(settings, transport=transport) != actor:
            return receipt
        _, tip = destination
        receipt.update(status="merged_verified", base_ref=gate["base_ref"], observed_base_tip=tip,
                       html_url=merged.get("html_url"), destination_verified=True,
                       checks_state=gate["checks"]["state"], self_review_is_independent_approval=False)
    except (Fault, KeyError, TypeError):
        return receipt
    return receipt
