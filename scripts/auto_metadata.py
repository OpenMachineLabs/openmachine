#!/usr/bin/env python3
"""Conservatively fill missing primary labels and assignees on GitHub events."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen


MANAGED = {
    item["name"] for item in json.loads(
        (Path(__file__).resolve().parents[1] / ".github/labels.json").read_text()
    )
}
ISSUE_PREFIXES = {
    "SPEC": "spec", "SURVEY": "research", "BENCHMARK": "benchmark",
    "BUG": "bug", "REQUEST": "feature-request", "ROADMAP": "feature",
}
DOMAINS = {
    "bug": "bug", "fix": "bug", "docs": "documentation",
    "documentation": "documentation", "security": "security",
    "ci": "maintenance", "test": "maintenance", "build": "maintenance",
    "chore": "maintenance", "deps": "maintenance",
    "refactor": "maintenance", "governance": "maintenance",
    "maintenance": "maintenance", "implementation": "implementation",
    "feature": "feature", "research": "research",
}
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37})\Z")
DOMAIN = re.compile(r"^([a-z][a-z0-9-]*)\([a-z][a-z0-9-]*\): ")
ISSUE_REFERENCES = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(first: 20, includeClosedPrs: true) {
        pageInfo { hasNextPage }
        nodes { mergedAt author { login __typename } }
      }
    }
  }
}
"""
PULL_REFERENCES = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 20) {
        pageInfo { hasNextPage }
        nodes { number }
      }
    }
  }
}
"""


def title_label(title, *, issue=False):
    if issue:
        prefix = re.match(r"^\[([A-Z]+)\] ", title)
        if prefix:
            return ISSUE_PREFIXES.get(prefix[1])
    domain = DOMAIN.match(title)
    return DOMAINS.get(domain[1]) if domain else None


def issue_label(issue):
    title = title_label(issue.get("title") or "", issue=True)
    section = re.search(
        r"(?ms)^## Project metadata\s*\n(.*?)(?=^## |\Z)",
        issue.get("body") or "",
    )
    fields = re.findall(r"(?m)^- Primary label: ([a-z][a-z0-9-]*)\s*$",
                        section[1]) if section else []
    declared = fields[0] if len(fields) == 1 and fields[0] in MANAGED else None
    if title and declared and title != declared:
        return None
    return title or declared


def file_label(paths):
    if not paths:
        return None

    def category(path):
        if path.startswith(("docs/", "spec/", "assets/")) or path in {
            "README.md", "CONTRIBUTING.md", "MAINTAINERS.md",
            "CODE_OF_CONDUCT.md", "SECURITY.md",
        }:
            return "documentation"
        if path.startswith((".github/", "scripts/", "test/", "tests/")):
            return "maintenance"
        return None

    categories = {category(path) for path in paths}
    return next(iter(categories)) if len(categories) == 1 else None


def pull_label(pull, paths):
    from_title = title_label(pull.get("title") or "")
    from_files = file_label(paths)
    if from_title and from_files and from_title != from_files:
        return None
    return from_title or from_files


def human_login(author):
    if not isinstance(author, dict) or author.get("__typename", author.get("type")) != "User":
        return None
    login = author.get("login")
    return login if isinstance(login, str) and LOGIN.fullmatch(login) else None


class GitHub:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.owner, self.name = self.repo.split("/", 1)
        self.token = os.environ["GH_TOKEN"]

    def request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            "https://api.github.com/" + path.lstrip("/"),
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + self.token,
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "openmachine-auto-metadata",
            },
        )
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None

    def get(self, path):
        return self.request("GET", path)[1]

    def post(self, path, payload):
        return self.request("POST", path, payload)[1]

    def graphql(self, query, number):
        result = self.post("graphql", {
            "query": query,
            "variables": {"owner": self.owner, "name": self.name, "number": number},
        })
        if result.get("errors"):
            raise RuntimeError("GitHub GraphQL rejected metadata lookup: " + str(result["errors"]))
        return result["data"]["repository"]

def closed_near_merge(closed_at, merged_at):
    if not closed_at or not merged_at:
        return False
    closed = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
    merged = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    return -timedelta(minutes=1) <= closed - merged <= timedelta(minutes=10)


def merged_issue_author(api, issue):
    if issue.get("state") != "closed" or not issue.get("closed_at"):
        return None
    references = api.graphql(ISSUE_REFERENCES, issue["number"])["issue"]
    if not references:
        return None
    connection = references["closedByPullRequestsReferences"]
    if connection["pageInfo"]["hasNextPage"]:
        return None  # Do not infer a unique owner from a truncated page.
    authors = set()
    for pull in connection["nodes"]:
        if not pull or not pull.get("mergedAt"):
            continue
        if closed_near_merge(issue["closed_at"], pull["mergedAt"]):
            login = human_login(pull.get("author"))
            if not login:
                return None  # A bot or unknown author makes attribution ambiguous.
            authors.add(login)
    return next(iter(authors)) if len(authors) == 1 else None


def fill_missing(api, issue, label, assignee=None):
    number = issue["number"]
    base = f"repos/{api.repo}/issues/{number}"
    existing = {item["name"] for item in issue.get("labels", [])}
    if not (existing & MANAGED) and label in MANAGED:
        api.post(base + "/labels", {"labels": [label]})
        print(f"#{number}: added primary label {label}")
    if issue.get("assignees"):
        return
    if assignee:
        try:
            updated = api.post(base + "/assignees", {"assignees": [assignee]})
        except HTTPError as error:
            if error.code not in (404, 422):
                raise
            print(f"#{number}: GitHub rejected {assignee}; leaving unassigned")
            return
        if assignee.casefold() in {
            user["login"].casefold() for user in updated["assignees"]
        }:
            print(f"#{number}: assigned {assignee}")
        else:
            print(f"#{number}: GitHub did not assign {assignee}; leaving unassigned")


def pull_paths(api, number):
    base = f"repos/{api.repo}/pulls/{number}/files"
    paths = []
    for page in range(1, 4):
        files = api.get(f"{base}?per_page=100&page={page}")
        paths.extend(file["filename"] for file in files)
        if len(files) < 100:
            return paths
    return []  # Incomplete file coverage cannot support classification.


def process_issue(api, number, *, merged_at=None):
    issue = api.get(f"repos/{api.repo}/issues/{number}")
    if merged_at is not None and (issue.get("state") != "closed" or not closed_near_merge(issue.get("closed_at"), merged_at)):
        return
    if issue.get("pull_request"):
        return
    needs_owner = not issue.get("assignees") and issue.get("state") == "closed"
    author = merged_issue_author(api, issue) if needs_owner else None
    fill_missing(api, issue, issue_label(issue), author)


def process_pull(api, number):
    pull = api.get(f"repos/{api.repo}/pulls/{number}")
    issue = api.get(f"repos/{api.repo}/issues/{number}")
    label = None if set(item["name"] for item in issue.get("labels", [])) & MANAGED else (
        pull_label(pull, pull_paths(api, number))
    )
    fill_missing(api, issue, label, human_login(pull.get("user")))
    if not pull.get("merged_at"):
        return
    references = api.graphql(PULL_REFERENCES, number)["pullRequest"]
    if not references:
        return
    connection = references["closingIssuesReferences"]
    if connection["pageInfo"]["hasNextPage"]:
        print(f"#{number}: too many closing issues; skipping linked assignments")
        return
    for node in connection["nodes"]:
        if node:
            process_issue(api, node["number"], merged_at=pull["merged_at"])


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    kind = os.environ["GITHUB_EVENT_NAME"]
    api = GitHub()
    if kind == "issues":
        process_issue(api, event["issue"]["number"])
    elif kind == "pull_request_target":
        process_pull(api, event["pull_request"]["number"])
    else:
        raise ValueError(f"Unsupported metadata event: {kind}")


if __name__ == "__main__":
    main()
