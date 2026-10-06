#!/usr/bin/env python3
"""Run and cache the repository's local pre-merge verification."""

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CACHE_FORMAT = 1
DEFAULT_BASE = "origin/main"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], check=check,
                          capture_output=True, text=True, timeout=30)


def _run(argv: list[str], *, cwd: Path) -> None:
    print("+", " ".join(argv), flush=True)
    subprocess.run(argv, cwd=cwd, check=True, timeout=600)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_commit(root: Path, ref: str) -> str:
    if not ref or ref.startswith("-") or any(ord(char) < 32 for char in ref):
        raise ValueError("base must be an explicit local Git ref or full SHA")
    result = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    sha = result.stdout.strip()
    if result.returncode or len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError(f"base {ref!r} is not available locally; fetch it before premerge")
    return sha


def require_ancestor(root: Path, base_sha: str, head_sha: str) -> None:
    result = _git(root, "merge-base", "--is-ancestor", base_sha, head_sha, check=False)
    if result.returncode == 1:
        raise ValueError("base is not an ancestor of HEAD; select the exact candidate base")
    if result.returncode:
        raise ValueError("Git could not verify the requested candidate range")


def require_clean(root: Path) -> None:
    status = _git(root, "status", "--porcelain", "--untracked-files=all").stdout
    if status:
        raise ValueError("premerge requires a clean committed worktree")


def cache_path(root: Path, head_sha: str) -> Path:
    common = Path(_git(root, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = (root / common).resolve()
    return common / "openmachine" / "premerge" / f"{head_sha}.json"


def fingerprint(root: Path) -> dict:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "requirements_sha256": _sha256(root / "requirements.lock"),
        "workflow_sha256": _sha256(root / ".github/workflows/quality.yml"),
        "script_sha256": _sha256(root / "scripts/premerge.py"),
    }


def cache_record(head_sha: str, base_sha: str, environment: dict) -> dict:
    return {
        "format": CACHE_FORMAT,
        "status": "passed",
        "head_sha": head_sha,
        "base_sha": base_sha,
        "environment": environment,
    }


def cache_matches(value: object, head_sha: str, base_sha: str, environment: dict) -> bool:
    return value == cache_record(head_sha, base_sha, environment)


def read_cache(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None


def write_cache(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def execute_checks(root: Path) -> None:
    _run([sys.executable, "-m", "unittest", "discover", "-s", "test/system", "-p", "test_*.py"], cwd=root)
    _run([sys.executable, "scripts/check_repository.py"], cwd=root)
    with tempfile.TemporaryDirectory(prefix="openmachine-premerge-") as directory:
        temporary = Path(directory)
        environment = temporary / "venv"
        _run([sys.executable, "-m", "venv", str(environment)], cwd=root)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        _run([str(python), "-m", "pip", "install", "-r", str(root / "requirements.lock")], cwd=root)
        wheel_dir = temporary / "wheel"
        wheel_dir.mkdir()
        _run([str(python), "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", ".",
              "--wheel-dir", str(wheel_dir)], cwd=root)
        wheels = list(wheel_dir.glob("openmachine-*.whl"))
        if len(wheels) != 1:
            raise ValueError("premerge expected exactly one Open Machine wheel")
        _run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheels[0])], cwd=root)
        outside = temporary / "outside"
        outside.mkdir()
        _run([str(python), "-m", "unittest", "discover", "-s", str(root / "test/unit"), "-p", "test_*.py"],
             cwd=outside)
        _run([str(python), "-m", "unittest", "discover", "-s", str(root / "test/contract"), "-p", "test_*.py"],
             cwd=outside)
    _run(["git", "diff", "--check"], cwd=root)


def run_premerge(root: Path, base_ref: str, *, force: bool = False) -> dict:
    require_clean(root)
    head_sha = resolve_commit(root, "HEAD")
    base_sha = resolve_commit(root, base_ref)
    require_ancestor(root, base_sha, head_sha)
    environment = fingerprint(root)
    path = cache_path(root, head_sha)
    if not force and cache_matches(read_cache(path), head_sha, base_sha, environment):
        print(f"PASS: cached premerge for {head_sha} against {base_sha}")
        return {"status": "cached", "head_sha": head_sha, "base_sha": base_sha, "cache": str(path)}

    execute_checks(root)
    require_clean(root)
    record = cache_record(head_sha, base_sha, environment)
    write_cache(path, record)
    print(f"PASS: premerge for {head_sha} against {base_sha}")
    return {"status": "passed", "head_sha": head_sha, "base_sha": base_sha, "cache": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="local base ref or full SHA for candidate identity validation (default: origin/main)")
    parser.add_argument("--force", action="store_true", help="rerun checks even when this exact candidate is cached")
    args = parser.parse_args()
    try:
        run_premerge(ROOT, args.base, force=args.force)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"FAIL: premerge: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
