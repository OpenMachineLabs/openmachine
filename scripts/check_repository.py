#!/usr/bin/env python3
"""Small, dependency-free repository checks; not a product runtime."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U000323af]")
TITLE = re.compile(r"([a-z][a-z0-9-]*)\(([a-z][a-z0-9-]*)\): ([\x20-\x7e]+)")
SHA = re.compile(r"[0-9a-f]{40}")
TEXT_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml", ".toml", ".sh"}
ISSUE_HEADINGS = ("Classification", "Why", "Outcome", "Scope", "Project metadata",
                  "Acceptance", "Dependencies and boundary", "Evidence")
PR_HEADINGS = ("Classification", "Why and outcome", "Scope", "Traceability",
               "Project metadata", "Validation", "Risk, boundary, and rollback", "Review guide")


def english_errors(text: str) -> list[str]:
    """Catch common accidental CJK prose; English meaning still needs review."""
    return ["use English; CJK text was found"] if CJK.search(text) else []


def title_errors(title: str, kind: str) -> list[str]:
    limit = 72 if kind == "commit" else 100
    if not title or len(title) > limit or not all(32 <= ord(c) <= 126 for c in title):
        return [f"title must be printable ASCII and at most {limit} characters"]
    if title != title.strip() or title.endswith("."):
        return ["title must not have surrounding spaces or a final period"]
    prefix = ""
    if kind == "issue" and title.startswith("["):
        match = re.fullmatch(r"\[(SPEC|SURVEY|BENCHMARK|Program)\] (\S.*)", title)
        if not match:
            return ["use an exact supported Issue prefix, followed by one space"]
        prefix, title = match.groups()
        if prefix == "Program":
            return []
    match = TITLE.fullmatch(title)
    if not match or match[3] != match[3].strip() or match[3].lower() in {"update", "misc", "todo"}:
        return ["use domain(scope): concrete English outcome"]
    domain = match[1]
    if prefix == "SURVEY" and domain not in {"research", "experiment"}:
        return ["Survey domain must be research or experiment"]
    if (prefix == "BENCHMARK") != (domain == "eval"):
        return ["eval is required for, and reserved for, Benchmark Issues"]
    return []


def label_errors(labels: object) -> list[str]:
    if not isinstance(labels, list) or not labels:
        return ["labels must be a nonempty list"]
    names = set()
    for label in labels:
        if not isinstance(label, dict) or set(label) != {"name", "color", "description"}:
            return ["each label needs only name, color, and description"]
        name, color, description = (label[key] for key in ("name", "color", "description"))
        if not all(isinstance(value, str) for value in (name, color, description)):
            return ["label fields must be strings"]
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,49}", name) or name in names:
            return ["label names must be unique lowercase kebab-case"]
        if not re.fullmatch(r"[0-9a-fA-F]{6}", color) or not 1 <= len(description.strip()) <= 100:
            return ["labels need a six-digit color and a nonempty short description"]
        if english_errors(description):
            return ["label descriptions must be English"]
        names.add(name)
    return []


def file_errors(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors = english_errors(text) + english_errors(str(path.relative_to(root)))
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or (raw and not raw.endswith(b"\n")):
        errors.append("use UTF-8 without BOM, LF, and a final newline")
    if any(line.rstrip(" \t") != line for line in text.splitlines()):
        errors.append("remove trailing whitespace")
    if path.suffix == ".md":
        prose = re.sub(r"```.*?```", "", text, flags=re.S)
        for link in re.findall(r"\[[^\]\n]*\]\(([^\s)]+)\)", prose):
            url = urlsplit(link.strip("<>"))
            if url.scheme or url.netloc or not url.path:
                continue
            target = (root if url.path.startswith("/") else path.parent) / unquote(url.path.lstrip("/"))
            if not target.resolve().is_relative_to(root.resolve()) or not target.exists():
                errors.append(f"missing or out-of-repository Markdown target: {url.path}")
    return errors


def metadata_errors(obj: dict, kind: str) -> list[str]:
    errors = title_errors(obj.get("title", ""), kind)
    body = obj.get("body") or ""
    errors += english_errors(body)
    required = PR_HEADINGS if kind == "pr" else ISSUE_HEADINGS
    headings = re.findall(r"^## (.+)$", body, flags=re.M)
    errors += [f"body needs exactly one '{heading}' section" for heading in required
               if headings.count(heading) != 1]
    return errors


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, timeout=30).stdout


def commit_errors(root: Path, base: str, head: str) -> list[str]:
    if not SHA.fullmatch(base) or not SHA.fullmatch(head) or base == "0" * 40:
        return ["commit audit needs an existing full base SHA and full head SHA"]
    errors = []
    for message in git(root, "log", "--format=%B%x00", f"{base}..{head}").split("\0"):
        message = message.strip()
        if message:
            errors += title_errors(message.splitlines()[0], "commit") + english_errors(message)
    return errors


def main() -> int:
    errors = []
    paths = git(ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    for name in filter(None, paths):
        path = ROOT / name
        if not path.exists():
            continue  # A tracked deletion is part of the current candidate.
        if path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore"}:
            errors.extend(f"{name}: {error}" for error in file_errors(path, ROOT))
    errors += label_errors(json.loads((ROOT / ".github/labels.json").read_text()))
    event_path, event_name = os.environ.get("GITHUB_EVENT_PATH"), os.environ.get("GITHUB_EVENT_NAME")
    if event_path and event_name:
        event = json.loads(Path(event_path).read_text())
        if event_name == "pull_request":
            pr = event["pull_request"]
            errors += metadata_errors(pr, "pr")
        elif event_name == "issues":
            errors += metadata_errors(event["issue"], "issue")
        elif event_name == "push" and not event.get("deleted"):
            errors += commit_errors(ROOT, event["before"], event["after"])
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if not errors:
        print("PASS: repository checks (not product acceptance)")
    return bool(errors)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"FAIL: repository check could not complete ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
