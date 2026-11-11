#!/usr/bin/env python3
"""Check usable repository inputs; presentation conventions are advisory."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml", ".toml", ".sh"}


# Public Core must not publish links to sibling organization repositories or
# directly address private/shared chat sessions. This is a narrow release-scope
# guard, not a general-purpose secret scanner.
SIBLING_REPO = re.compile(r"\bOpenMachineLabs/([A-Za-z0-9_.-]+)\b", re.I)
CHAT_SESSION = re.compile(
    r"https?://(?:(?:chatgpt[.]com|chat[.]openai[.]com)/(?:c|share)"
    r"|claude[.]ai/(?:chat|share)|gemini[.]google[.]com/(?:app|share))/[A-Za-z0-9_-]+", re.I
)


def public_boundary_errors(text: str) -> list[str]:
    errors = []
    if any(match[1].lower().removesuffix(".git") != "openmachine"
           for match in SIBLING_REPO.finditer(text)):
        errors.append("remove sibling organization repository references from public Core")
    if CHAT_SESSION.search(text):
        errors.append("remove direct chat-session URLs from public Core")
    return errors


def title_errors(title: str, kind: str) -> list[str]:
    if not isinstance(title, str) or not title.strip() or not title.isprintable():
        return ["title must be nonempty printable text"]
    return []


def label_errors(labels: object) -> list[str]:
    if not isinstance(labels, list) or not labels:
        return ["labels must be a nonempty list"]
    names = set()
    for label in labels:
        if not isinstance(label, dict) or set(label) != {"name", "color", "description"}:
            return ["each label needs name, color, and description"]
        name, color, description = (label[key] for key in ("name", "color", "description"))
        if not all(isinstance(value, str) for value in (name, color, description)):
            return ["label fields must be strings"]
        if not name.strip() or len(name) > 50 or not name.isprintable() or name in names:
            return ["label names must be unique, nonempty printable text of at most 50 characters"]
        if not re.fullmatch(r"[0-9a-fA-F]{6}", color) or len(description) > 100:
            return ["labels need a six-digit color and a description of at most 100 characters"]
        names.add(name)
    return []


def file_errors(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors = public_boundary_errors(text)
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
    if not isinstance(obj, dict):
        return ["metadata must be a JSON object"]
    errors = title_errors(obj.get("title"), kind)
    body = obj.get("body")
    if not isinstance(body, str) or not body.strip():
        return errors + ["body must briefly explain the change or question"]
    if any(ord(char) < 32 and char not in "\t\n\r" for char in body):
        errors.append("body must not contain unsupported control characters")
    errors += public_boundary_errors(str(obj.get("title", ""))) + public_boundary_errors(body)
    return errors


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, timeout=30).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("all", "source", "metadata"), default="all")
    parser.add_argument("--event-name", choices=("pull_request", "issues", "push"))
    parser.add_argument("--event-path", type=Path)
    args = parser.parse_args(argv)
    if (args.event_name is None) != (args.event_path is None):
        parser.error("--event-name and --event-path must be supplied together")
    event_name = args.event_name or os.environ.get("GITHUB_EVENT_NAME")
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    metadata_event = args.mode != "source" and event_name in {"pull_request", "issues"}
    if args.mode == "metadata" and not metadata_event:
        parser.error("metadata mode requires a pull_request or issues event")
    errors = []
    if args.mode != "metadata":
        paths = git(ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
        for name in filter(None, paths):
            path = ROOT / name
            if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore"}):
                errors.extend(f"{name}: {error}" for error in file_errors(path, ROOT))
        errors += label_errors(json.loads((ROOT / ".github/labels.json").read_text()))
    if metadata_event:
        if not event_path:
            parser.error("metadata validation requires event JSON")
        event = json.loads(Path(event_path).read_text())
        kind = "pr" if event_name == "pull_request" else "issue"
        obj = event["pull_request"] if kind == "pr" else event["issue"]
        if kind != "issue" or not isinstance(obj, dict) or not obj.get("pull_request"):
            errors += metadata_errors(obj, kind)
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if not errors:
        print("PASS: repository checks (not product acceptance)")
    return bool(errors)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f"FAIL: repository check could not complete ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
