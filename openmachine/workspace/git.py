"""Canonical Git identity and bounded, non-interactive Git capture."""

import os
from pathlib import Path
import selectors
import subprocess
import time

from openmachine.foundation.sanitize import Fault

MAX_READ = 65536
MAX_CAPTURE = 1024 * 1024


def context(cwd: Path) -> dict:
    """Capture Git identity without changing the worktree or selecting an environment."""
    result = {"cwd": str(cwd), "worktree": str(cwd), "head_sha": None, "branch": None,
              "agents_files": [str(p / "AGENTS.md") for p in reversed((cwd, *cwd.parents))
                               if (p / "AGENTS.md").is_file()]}
    try:
        probe = subprocess.run(["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=5, env=_git_environment())
        if probe.returncode:
            result["git_status"] = "not_a_worktree"
            return result
        result["worktree"] = str(Path(probe.stdout.strip()).resolve())
        for key, args in (("head_sha", ["rev-parse", "--verify", "HEAD"]),
                          ("branch", ["symbolic-ref", "--quiet", "--short", "HEAD"])):
            item = subprocess.run(["git", "--no-optional-locks", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(cwd), *args], capture_output=True,
                                  text=True, timeout=5, env=_git_environment())
            result[key] = item.stdout.strip() if item.returncode == 0 else None
        result["git_status"] = "observed"
    except (OSError, subprocess.TimeoutExpired):
        result["git_status"] = "unavailable"
    return result


def _git_environment() -> dict:
    routing = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
    return {key: value for key, value in os.environ.items() if key not in routing}


def capture(root: Path, args: list[str]) -> tuple[bytes, int, bool]:
    """Capture bounded output without a shell, pager, optional locks or fsmonitor."""
    command = ["git", "--no-optional-locks", "--no-pager", "--literal-pathspecs",
               "-c", "core.fsmonitor=false", "-C", str(root), *args]
    output, diagnostic = bytearray(), bytearray()
    clipped = False
    try:
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=_git_environment()) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, output)
                    selector.register(process.stderr, selectors.EVENT_READ, diagnostic)
                    deadline = time.monotonic() + 5
                    while selector.get_map():
                        if time.monotonic() >= deadline:
                            raise Fault("git_timeout", "Git inspection exceeded five seconds; narrow the path.")
                        for key, _ in selector.select(0.05):
                            chunk = os.read(key.fd, MAX_READ)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            budget = MAX_CAPTURE if key.data is output else 4096
                            room = budget - len(key.data)
                            key.data.extend(chunk[:room])
                            if len(chunk) > room:
                                if key.data is diagnostic:
                                    raise Fault("git_failed", "Git diagnostics exceeded the safe limit; inspect locally.")
                                clipped = True
                                break
                        if clipped:
                            break
                if clipped:
                    process.kill()
                code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    except FileNotFoundError as error:
        raise Fault("missing_dependency", "Install Git to inspect a Git workspace.") from error
    except subprocess.TimeoutExpired as error:
        raise Fault("git_timeout", "Git did not finish within the inspection budget.") from error
    return bytes(output), code, clipped
