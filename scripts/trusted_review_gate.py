#!/usr/bin/env python3
"""Publish trusted maintainer approval to an exact PR HEAD, never using fork code."""
import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

REPO = "OpenMachineLabs/openmachine"
REPO_ID = 1392535998
MAINTAINERS = {70309026, 191080179}  # LuozhuZhang and bella-chai
CONTEXT = "om/trusted-review-gate"
SIGNAL_WORKFLOW_ID = 386447341  # Immutable ID of review-policy-signal
SHA = re.compile(r"[0-9a-f]{40}\Z")
TITLE = re.compile(r"Review policy PR #([1-9][0-9]{0,8})\Z")


def uid(value):
    return value if type(value) is int and value > 0 else None


def official(repo):
    return (isinstance(repo, dict) and uid(repo.get("id")) == REPO_ID
            and str(repo.get("full_name", "")).lower() == REPO.lower())


def decide(pr, reviews):
    """Return (state, reason); never accept a stale or untrusted approval."""
    if pr.get("state") != "open" or pr.get("draft") is not False:
        return "pending", "PR is closed or draft"
    head, base, author = (pr.get(name) for name in ("head", "base", "user"))
    if not all(isinstance(obj, dict) for obj in (head, base, author)):
        return "error", "Missing PR identities"
    sha = head.get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        return "error", "Invalid PR HEAD"
    if base.get("ref") != "main" or not official(base.get("repo")):
        return "error", "Unknown destination"
    trusted_internal = uid(author.get("id")) in MAINTAINERS and official(head.get("repo"))
    latest = {}
    for review in reviews:
        if not isinstance(review, dict):
            return "error", "Malformed review"
        user = review.get("user")
        reviewer = uid(user.get("id")) if isinstance(user, dict) else None
        if reviewer not in MAINTAINERS:
            continue
        state = review.get("state")
        if state == "COMMENTED":
            continue
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            return "error", "Unknown trusted review state"
        latest[reviewer] = (state, review.get("commit_id"))
    if any(state == "CHANGES_REQUESTED" for state, _ in latest.values()):
        return "failure", "Maintainer requested changes"
    if trusted_internal:
        return "success", "Trusted internal maintainer; review waived"
    if any(state == "APPROVED" and commit == sha for state, commit in latest.values()):
        return "success", "Trusted maintainer approved current HEAD"
    return "pending", "Waiting for trusted approval on current HEAD"


class API:
    def __init__(self, token):
        if not token:
            raise ValueError("missing token")
        self.token = token

    def get(self, path, body=None):
        if not path.startswith(f"/repos/{REPO}/"):
            raise ValueError("out-of-repo request")
        data = json.dumps(body).encode() if body is not None else None
        req = Request("https://api.github.com" + path, data=data,
                      method="POST" if data is not None else "GET",
                      headers={"Accept": "application/vnd.github+json",
                               "Authorization": f"Bearer {self.token}",
                               "X-GitHub-Api-Version": "2022-11-28",
                               "User-Agent": "om-review-gate",
                               **({"Content-Type": "application/json"} if data else {})})
        try:
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError) as error:
            raise RuntimeError(f"GitHub API error ({type(error).__name__})") from None

    def pages(self, path):
        output = []
        for page in range(1, 11):
            sep = "&" if "?" in path else "?"
            part = self.get(f"{path}{sep}per_page=100&page={page}")
            if not isinstance(part, list):
                raise RuntimeError("invalid API list")
            output.extend(part)
            if len(part) < 100:
                return output
        raise RuntimeError("API pagination incomplete")

    def pull(self, number):
        return self.get(f"/repos/{REPO}/pulls/{number}")

    def reviews(self, number):
        return self.pages(f"/repos/{REPO}/pulls/{number}/reviews")

    def open_pulls(self):
        pulls = self.pages(f"/repos/{REPO}/pulls?state=open&base=main")
        if not all(isinstance(p, dict) and uid(p.get("number")) for p in pulls):
            raise RuntimeError("invalid open PR list")
        return pulls

    def opens(self):
        return [p["number"] for p in self.open_pulls()]

    def status(self, sha):
        # Newest first. The combined /status endpoint omits creators, so it can't dedupe.
        statuses = self.get(f"/repos/{REPO}/commits/{sha}/statuses?per_page=100")
        if not isinstance(statuses, list):
            raise RuntimeError("invalid status response")
        return next((s for s in statuses
                     if isinstance(s, dict) and s.get("context") == CONTEXT), None)

    def publish(self, sha, number, state, reason):
        self.get(f"/repos/{REPO}/statuses/{sha}", {
            "state": state, "context": CONTEXT, "description": reason,
            "target_url": f"https://github.com/{REPO}/pull/{number}"})


def event_numbers(event, api):
    name = os.environ.get("GITHUB_EVENT_NAME")
    if name == "schedule":
        return []
    if name == "workflow_dispatch":
        value = (event.get("inputs") or {}).get("pull_number")
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,8}", value):
            raise ValueError("invalid manual PR number")
        return [int(value)]
    if name != "workflow_run":
        raise ValueError("unexpected privileged event")
    run = event.get("workflow_run")
    if not isinstance(run, dict) or run.get("workflow_id") != SIGNAL_WORKFLOW_ID:
        raise ValueError("unknown signal workflow")
    if run.get("event") not in {"pull_request", "pull_request_review"}:
        raise ValueError("unknown signal event")
    title = run.get("display_title")
    match = TITLE.fullmatch(title) if isinstance(title, str) else None
    if match:
        return [int(match.group(1))]
    linked = run.get("pull_requests", [])
    if not isinstance(linked, list):
        return []
    return sorted({n for p in linked if isinstance(p, dict)
                   if (n := uid(p.get("number"))) and n < 10**9})[:5]


def targets(event, api):
    """Hinted PRs first, then every open PR, so a superseded queued run loses nothing."""
    return list(dict.fromkeys(event_numbers(event, api) + api.opens()))


# Open PRs sharing a HEAD share one commit status: any block wins, then any approval.
PRIORITY = ("failure", "error", "success", "pending")


def decide_head(api, pr, number, sha):
    decisions = [decide(pr, api.reviews(number))]
    for peer in api.open_pulls():
        head = peer.get("head")
        if peer["number"] != number and isinstance(head, dict) and head.get("sha") == sha:
            decisions.append(decide(peer, api.reviews(peer["number"])))
    return min(decisions, key=lambda decision: PRIORITY.index(decision[0]))


def process(api, number):
    pr = api.pull(number)
    base = pr.get("base")
    if pr.get("state") != "open" or not isinstance(base, dict) or base.get("ref") != "main":
        return
    head = pr.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise RuntimeError("bad HEAD")
    try:
        state, reason = decide_head(api, pr, number, sha)
    except (ValueError, RuntimeError):
        state, reason = "error", "Review evidence unavailable"
    refreshed = api.pull(number)
    if not isinstance(refreshed.get("head"), dict) or refreshed["head"].get("sha") != sha:
        print(f"PR #{number}: HEAD changed; stale result not published")
        return
    old = api.status(sha)
    if (old and old.get("state") == state and old.get("description") == reason
            and isinstance(old.get("creator"), dict)
            and old["creator"].get("login") == "github-actions[bot]"):
        return
    api.publish(sha, number, state, reason)
    print(f"PR #{number}: {state} ({reason})")
    if state == "error":
        raise RuntimeError("review evidence unavailable")


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPO:
        raise ValueError("unexpected repository")
    path = Path(os.environ["GITHUB_EVENT_PATH"])
    if path.stat().st_size > 2_000_000:
        raise ValueError("event too large")
    event = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(event, dict):
        raise ValueError("invalid event")
    api = API(os.environ.get("GH_TOKEN"))
    errors = 0
    for number in targets(event, api):
        try:
            process(api, number)
        except (ValueError, RuntimeError) as error:
            print(f"PR #{number}: {error}", file=sys.stderr)
            errors += 1
    return int(errors > 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as error:
        print(f"Gate failed closed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
