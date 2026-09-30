"""Canonical Git identity and bounded, non-interactive Git capture."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path
import selectors
import subprocess
import time

from openmachine.foundation.sanitize import Fault

MAX_READ = 65536
MAX_CAPTURE = 1024 * 1024


def context(cwd: Path) -> dict:
    """Observe a canonical root and branch; Git failure never grants non-Git access."""
    result = {"cwd": str(cwd), "worktree": str(cwd), "head_sha": None, "branch": None,
              "git_state": "unavailable", "git_status": "unavailable",
              "workspace_identity": None, "workspace_identity_version": 1,
              "agents_files": [str(p / "AGENTS.md") for p in reversed((cwd, *cwd.parents))
                               if (p / "AGENTS.md").is_file()]}
    try:
        root_info = cwd.stat()
        if not stat.S_ISDIR(root_info.st_mode):
            return result
        # Inspect all ancestors explicitly, including broken links and bare Git roots.
        controlled = False
        for parent in (cwd, *cwd.parents):
            for name in (".git", "HEAD"):
                try:
                    (parent / name).lstat()
                except FileNotFoundError:
                    continue
                if name == ".git" or (parent / "objects").is_dir():
                    controlled = True
        identity = {"version": 1, "root": str(cwd),
                    "root_id": [root_info.st_dev, root_info.st_ino]}
        result["root_id"] = identity["root_id"]
        if not controlled:
            result.update(git_state="non_git", git_status="not_a_worktree")
            identity["git_state"] = "non_git"
        else:
            raw, code, clipped = capture(cwd, ["rev-parse", "--path-format=absolute", "--show-toplevel",
                                               "--git-dir", "--git-common-dir", "--show-object-format"])
            values = raw.decode("utf-8").splitlines()
            if code or clipped or len(values) != 4:
                return result
            worktree, git_dir, common_dir, object_format = values
            if object_format != "sha1":
                result["context_error"] = "unsupported_git_object_format"
                return result
            paths = [Path(value).resolve(strict=True) for value in (worktree, git_dir, common_dir)]
            raw, code, clipped = capture(cwd, ["symbolic-ref", "--quiet", "HEAD"])
            if clipped or code not in (0, 1):
                return result
            symbolic = raw.decode("utf-8").strip() if code == 0 else None
            if symbolic is not None and not symbolic.startswith("refs/heads/"):
                return result
            raw, code, clipped = capture(cwd, ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
            head = raw.decode("ascii").strip() if code == 0 else None
            if clipped or (head is not None and not re.fullmatch(r"[0-9a-f]{40}", head)):
                return result
            if head is None:
                if symbolic is None:
                    return result
                _, missing, clipped = capture(cwd, ["show-ref", "--verify", "--quiet", symbolic])
                if missing != 1 or clipped:
                    return result
            kind = "unborn" if head is None else "worktree" if symbolic else "detached"
            result.update(worktree=str(paths[0]), git_dir=str(paths[1]), common_dir=str(paths[2]),
                          head_sha=head, branch=symbolic.removeprefix("refs/heads/") if symbolic else None,
                          git_state=kind, git_status="observed")
            identity.update(git_state=kind, symbolic_head=symbolic,
                            git_paths=[[str(p), p.stat().st_dev, p.stat().st_ino] for p in paths])
        after = cwd.stat()
        if (after.st_dev, after.st_ino) != tuple(identity["root_id"]):
            result.update(git_state="unavailable", git_status="unavailable")
            return result
        result["workspace_identity"] = hashlib.sha256(json.dumps(identity, sort_keys=True,
            ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    except (OSError, UnicodeError, Fault, ValueError):
        result.update(git_state="unavailable", git_status="unavailable", workspace_identity=None)
    return result


def lock_root(config, machine: str, cwd: Path, observed: dict) -> str:
    """Preserve Git lock names; one unambiguous configured root owns non-Git work."""
    if observed["git_status"] == "unavailable":
        raise Fault("context_unavailable", "Git/workspace identity could not be verified.")
    if observed["git_status"] == "observed":
        return observed["worktree"]
    roots = set()
    for item in config.workspaces.values():
        if config.machine(item.machine).transport == "local":
            roots.add(Path(item.root).resolve())
    candidates = [root for root in roots if cwd.is_relative_to(root)]
    if len(candidates) != 1:
        raise Fault("ambiguous_workspace", "Select one unambiguous configured non-Git root.")
    root = candidates[0]
    if any(other != root and (other.is_relative_to(root) or root.is_relative_to(other)) for other in roots):
        raise Fault("ambiguous_workspace", "Overlapping non-Git roots cannot admit mutations.")
    return str(root)


def _git_environment() -> dict:
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


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
