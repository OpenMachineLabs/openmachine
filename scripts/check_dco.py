#!/usr/bin/env python3
"""Check that every commit in BASE..HEAD, merges aside, is signed off by its author (Developer Certificate of Origin)."""

import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = re.compile(r"<([^<>\s]+)>\s*$")


def dco_errors(root: Path, base: str, head: str) -> list[str]:
    log = subprocess.run(
        ["git", "-C", str(root), "log", "--no-merges",
         "--format=%H%x00%an%x00%ae%x00%(trailers:key=Signed-off-by,valueonly,separator=%x01)%x1e",
         f"{base}..{head}"], check=True, capture_output=True, text=True, timeout=30).stdout
    errors = []
    for entry in filter(None, (item.strip("\n") for item in log.split("\x1e"))):
        commit, name, email, trailers = entry.split("\x00")
        signers = {found[1].casefold() for value in trailers.split("\x01") if (found := ADDRESS.search(value))}
        if email.casefold() not in signers:
            errors.append(f"{commit[:12]} has no 'Signed-off-by: {name} <{email}>'")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("head")
    args = parser.parse_args(argv)
    errors = dco_errors(ROOT, args.base, args.head)
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if errors:
        print("Sign off each commit with `git commit -s`; for existing commits, `git rebase --signoff "
              f"{args.base}` and push again. See CONTRIBUTING.md.", file=sys.stderr)
    else:
        print("PASS: every commit is signed off by its author")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
