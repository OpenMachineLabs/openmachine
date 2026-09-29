"""Minimal GitHub collaboration writes through the operator's existing gh login."""

import json
import re
import subprocess
from typing import Literal

from openmachine.config import Settings
from openmachine.sanitize import Fault

ATTRIBUTION_URL = "https://github.com/OpenMachineLabs/openmachine"
CREATED_FOOTER = f"---\n\nCreated via [Open Machine]({ATTRIBUTION_URL})."
POSTED_FOOTER = f"---\n\nPosted via [Open Machine]({ATTRIBUTION_URL})."
FOOTERS = (CREATED_FOOTER, POSTED_FOOTER)


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


def _authorize(settings: Settings, repository: str):
    config = settings.github
    if config is None or repository.casefold() not in {item.casefold() for item in config.repositories}:
        raise Fault("github_not_allowed", "Select a repository explicitly enabled in the Open Machine configuration.")
    return config


def _api(method: str, endpoint: str, payload: dict | None = None) -> dict:
    argv = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
    data = None
    if payload is not None:
        argv += ["--input", "-"]
        data = json.dumps(payload)
    try:
        result = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as error:
        raise Fault("github_unavailable", "GitHub CLI is unavailable; local Open Machine features remain usable.") from error
    except subprocess.TimeoutExpired as error:
        raise Fault("github_unavailable", "GitHub did not respond within the bounded request timeout.") from error
    if result.returncode:
        raise Fault("github_failed", "GitHub rejected the request; check gh authentication, repository access, and protections.")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise Fault("github_invalid_response", "GitHub returned an unreadable response.") from error
    if not isinstance(value, dict):
        raise Fault("github_invalid_response", "GitHub returned an unexpected response shape.")
    return value


def _enabled(settings: Settings, repository: str) -> bool:
    config = _authorize(settings, repository)
    if config.attribution is not None:
        return config.attribution
    metadata = _api("GET", f"repos/{repository}")
    if not isinstance(metadata.get("private"), bool):
        raise Fault("github_invalid_response", "GitHub did not return repository visibility.")
    return not metadata["private"]


def _receipt(result: dict) -> dict:
    return {key: result[key] for key in ("id", "number", "html_url", "state", "submitted_at") if key in result}


def _mutate(repository: str, method: str, endpoint: str, payload: dict, readback) -> dict:
    try:
        result = _api(method, endpoint, payload)
    except Fault:
        return {"status": "write_outcome_unknown", "repository": repository,
                "detail": "The write has no confirmed response. Read the destination before any retry."}
    receipt = _receipt(result)
    try:
        observed = _api("GET", readback(result))
    except Fault:
        return {"status": "write_unverified", "repository": repository, "receipt": receipt,
                "detail": "GitHub accepted the request, but destination readback was not verified."}
    expected = payload.get("body")
    if expected is not None and observed.get("body") != expected:
        return {"status": "write_unverified", "repository": repository, "receipt": receipt,
                "detail": "Destination readback did not match the submitted body."}
    return {"status": "verified", "repository": repository, "receipt": _receipt(observed) or receipt,
            "body_verified": expected is None or observed.get("body") == expected}


def issue(settings: Settings, action: Literal["create", "update"], repository: str,
          title: str | None, body: str | None, number: int = 0) -> dict:
    _authorize(settings, repository)
    root = f"repos/{repository}/issues"
    if action == "create":
        if not title:
            raise Fault("invalid_github_request", "Creating an Issue requires a title.")
        submitted = _append(body or "", CREATED_FOOTER) if _enabled(settings, repository) else body or ""
        return _mutate(repository, "POST", root, {"title": title, "body": submitted},
                       lambda result: f"{root}/{result['number']}")
    if number < 1 or (title is None and body is None):
        raise Fault("invalid_github_request", "Updating an Issue requires its number and at least one field.")
    payload = {"title": title} if title is not None else {}
    if body is not None:
        current = _api("GET", f"{root}/{number}")
        payload["body"] = _preserve(current.get("body") or "", body)
    return _mutate(repository, "PATCH", f"{root}/{number}", payload, lambda _: f"{root}/{number}")


def pull_request(settings: Settings, action: Literal["create", "update"], repository: str,
                 title: str | None, body: str | None, number: int = 0,
                 head: str = "", base: str = "", draft: bool = False) -> dict:
    _authorize(settings, repository)
    root = f"repos/{repository}/pulls"
    if action == "create":
        if not title or not head or not base or head == base:
            raise Fault("invalid_github_request", "Creating a Pull Request requires title, distinct head, and base branches.")
        submitted = _append(body or "", CREATED_FOOTER) if _enabled(settings, repository) else body or ""
        payload = {"title": title, "body": submitted, "head": head, "base": base, "draft": draft}
        return _mutate(repository, "POST", root, payload, lambda result: f"{root}/{result['number']}")
    if number < 1 or (title is None and body is None):
        raise Fault("invalid_github_request", "Updating a Pull Request requires its number and at least one field.")
    payload = {"title": title} if title is not None else {}
    if body is not None:
        current = _api("GET", f"{root}/{number}")
        payload["body"] = _preserve(current.get("body") or "", body)
    return _mutate(repository, "PATCH", f"{root}/{number}", payload, lambda _: f"{root}/{number}")


def comment(settings: Settings, action: Literal["create", "update"], repository: str,
            body: str, number: int = 0, comment_id: int = 0) -> dict:
    _authorize(settings, repository)
    if action == "create":
        if number < 1:
            raise Fault("invalid_github_request", "Posting a comment requires an Issue or Pull Request number.")
        submitted = _append(body, POSTED_FOOTER) if _enabled(settings, repository) else body
        endpoint = f"repos/{repository}/issues/{number}/comments"
        return _mutate(repository, "POST", endpoint, {"body": submitted},
                       lambda result: f"repos/{repository}/issues/comments/{result['id']}")
    if comment_id < 1:
        raise Fault("invalid_github_request", "Updating a comment requires its comment ID.")
    endpoint = f"repos/{repository}/issues/comments/{comment_id}"
    current = _api("GET", endpoint)
    return _mutate(repository, "PATCH", endpoint, {"body": _preserve(current.get("body") or "", body)}, lambda _: endpoint)


def review(settings: Settings, action: Literal["create", "update"], repository: str,
           body: str, number: int = 0, review_id: int = 0, expected_head_sha: str = "",
           event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT") -> dict:
    _authorize(settings, repository)
    if action == "create":
        if number < 1 or not re.fullmatch(r"[0-9a-f]{40}", expected_head_sha):
            raise Fault("invalid_github_request", "Creating a review requires the Pull Request number and exact 40-character HEAD SHA.")
        current = _api("GET", f"repos/{repository}/pulls/{number}")
        if current.get("state") != "open" or current.get("head", {}).get("sha") != expected_head_sha:
            raise Fault("github_candidate_changed", "The Pull Request is closed or its HEAD changed; read and review the current candidate.")
        submitted = _append(body, POSTED_FOOTER) if _enabled(settings, repository) else body
        endpoint = f"repos/{repository}/pulls/{number}/reviews"
        payload = {"commit_id": expected_head_sha, "event": event, "body": submitted}
        return _mutate(repository, "POST", endpoint, payload,
                       lambda result: f"repos/{repository}/pulls/{number}/reviews/{result['id']}")
    if number < 1 or review_id < 1:
        raise Fault("invalid_github_request", "Updating a review requires the Pull Request number and review ID.")
    endpoint = f"repos/{repository}/pulls/{number}/reviews/{review_id}"
    current = _api("GET", endpoint)
    return _mutate(repository, "PUT", endpoint, {"body": _preserve(current.get("body") or "", body)}, lambda _: endpoint)
