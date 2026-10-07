#!/usr/bin/env python3
"""Verify and cache a clean candidate using the shared module-aware test runner."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("openmachine_test_runner", ROOT / "scripts/run_tests.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
CACHE_FORMAT = 2
DEFAULT_BASE = "origin/main"
resolve_commit = runner.resolve_commit
require_ancestor = runner.require_ancestor
require_clean = runner.require_clean


def cache_path(root: Path, head_sha: str, scope: str = "affected") -> Path:
    common = Path(runner.git(root, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = (root / common).resolve()
    return common / "openmachine" / "premerge" / f"{head_sha}-{scope}.json"


def fingerprint(root: Path) -> dict:
    files = ("requirements.lock", ".github/workflows/quality.yml", "scripts/premerge.py",
             "scripts/run_tests.py", "scripts/check_repository.py", runner.MANIFEST)
    return {
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "platform": sys.platform, "machine": platform.machine(),
        "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
    }


def cache_record(plan: dict, environment: dict) -> dict:
    return {
        "format": CACHE_FORMAT, "status": "passed",
        "head_sha": plan["head_sha"], "base_sha": plan["base_sha"],
        "scope": plan["scope"], "selection_sha256": plan["selection_sha256"],
        "environment": environment,
    }


def cache_matches(value: object, plan: dict, environment: dict) -> bool:
    return value == cache_record(plan, environment)


def read_cache(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_cache(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def execute_checks(root: Path, plan: dict) -> dict:
    return runner.execute_plan(root, plan)


def run_premerge(root: Path, base_ref: str, *, force: bool = False, full: bool = False,
                 ci: bool = False) -> dict:
    plan = runner.make_plan(root, base_ref, full=full, committed=True)
    environment = fingerprint(root)
    path = cache_path(root, plan["head_sha"], plan["scope"])
    # Hosted CI always obtains new hosted evidence and never trusts a developer's PASS cache.
    if not ci and not force and cache_matches(read_cache(path), plan, environment):
        runner.print_plan(plan)
        print(f"PASS: cached {plan['scope']} premerge for {plan['head_sha']} against {plan['base_sha']}")
        return {"status": "cached", "plan": plan, "cache": str(path)}
    if not ci:
        # A fresh failed/interrupted attempt must not leave an earlier PASS eligible.
        path.unlink(missing_ok=True)
    result = execute_checks(root, plan)
    if result.get("status") != "passed":
        raise ValueError("the selected checks did not complete successfully")
    require_clean(root)
    if resolve_commit(root, "HEAD") != plan["head_sha"] or resolve_commit(root, base_ref) != plan["base_sha"]:
        raise ValueError("HEAD or the requested base changed during verification")
    final = runner.make_plan(root, base_ref, full=full, committed=True)
    if final["selection_sha256"] != plan["selection_sha256"] or fingerprint(root) != environment:
        raise ValueError("candidate selection or verification inputs changed during verification")
    if not ci:
        write_cache(path, cache_record(plan, environment))
    print(f"PASS: {plan['scope']} premerge for {plan['head_sha']} against {plan['base_sha']}")
    return {**result, "status": "passed", "plan": plan, "cache": None if ci else str(path)}


def ci_candidate(root: Path, event_name: str, event: dict, github_sha: str) -> tuple[str, bool]:
    head = resolve_commit(root, "HEAD")
    if head != github_sha:
        raise ValueError("checkout HEAD does not match the event candidate SHA")
    if event_name == "pull_request":
        pr = event["pull_request"]
        base = resolve_commit(root, pr["base"]["sha"])
        branch_head = resolve_commit(root, pr["head"]["sha"])
        require_ancestor(root, base, head)
        require_ancestor(root, branch_head, head)
        return base, False
    if event_name == "push":
        if event.get("deleted") or event["after"] != head or event["before"] == "0" * 40:
            raise ValueError("push needs an existing exact before/after candidate range")
        base = resolve_commit(root, event["before"])
        require_ancestor(root, base, head)
        return base, False
    if event_name == "workflow_dispatch":
        # A manual full run does not need a fabricated previous commit.
        return head, True
    raise ValueError(f"unsupported code-verification event: {event_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE, help="local ancestor ref or full SHA (default: origin/main)")
    parser.add_argument("--full", action="store_true", help="request the complete layered suite")
    parser.add_argument("--force", action="store_true", help="rerun the selected scope even when it is cached")
    parser.add_argument("--ci", action="store_true", help="use the exact GitHub event candidate, without local PASS cache")
    args = parser.parse_args()
    try:
        base, full = args.base, args.full
        if args.ci:
            event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            base, event_full = ci_candidate(ROOT, os.environ["GITHUB_EVENT_NAME"], event, os.environ["GITHUB_SHA"])
            full = full or event_full
        run_premerge(ROOT, base, force=args.force, full=full, ci=args.ci)
    except (OSError, ValueError, KeyError, SyntaxError, subprocess.SubprocessError) as error:
        print(f"FAIL: premerge: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
