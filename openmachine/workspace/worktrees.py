"""Git-owned worktree discovery, explicit selection and bounded creation plans."""

from copy import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from openmachine.foundation.files import excluded
from openmachine.foundation.sanitize import Fault

from .git import capture, context

MAX_FILTERS = 8


def _absolute(value: str, *, allow_empty: bool = False, control_path: bool = False) -> str:
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
            raise ValueError
        if not value and allow_empty:
            return ""
        path = Path(value)
        if (not path.is_absolute() or path.anchor != "/" or ".." in path.parts or "\\" in value
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or not control_path and excluded(Path(*(part.casefold() for part in path.parts)))):
            raise ValueError
        return str(path)
    except (UnicodeError, ValueError, TypeError) as error:
        raise Fault("path_denied", "Use a bounded absolute non-credential path without traversal.") from error


def _root(config, machine: str, workspace: str) -> Path:
    if config.machine(machine).transport != "local":
        raise Fault("remote_path", "Resolve remote worktrees on their original SSH target.")
    item = config.workspaces.get(workspace)
    if item is None or item.machine != machine:
        raise Fault("unknown_workspace", "Select a workspace configured on this machine.")
    try:
        root = Path(item.root).resolve(strict=True)
        if not root.is_dir():
            raise OSError
    except (OSError, RuntimeError) as error:
        raise Fault("source_unavailable", "The configured workspace is not an available directory.") from error
    return root


def ensure_local_objects(root: Path) -> None:
    """Reject automatic object retrieval before any Git object inspection."""
    names, code, clipped = capture(root, ["config", "--null", "--name-only", "--get-regexp",
        r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$"])
    if clipped or code not in (0, 1) or names:
        raise Fault("git_config_unavailable", "Partial or promisor repositories are unsupported for bounded worktree operations; no objects were fetched.")


def _repository(root: Path) -> dict:
    ensure_local_objects(root)
    observed = context(root)
    if observed["git_status"] != "observed" or observed["worktree"] != str(root):
        raise Fault("git_unavailable", "Worktree selection requires a configured Git top-level directory.")
    return observed


def _identity(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino]


def _canonical(path: Path, *, missing: bool = False) -> Path:
    """Refuse links in every existing component, including the final destination."""
    parent = path.parent if missing else path
    try:
        for candidate in (parent, *parent.parents):
            if stat.S_ISLNK(candidate.lstat().st_mode):
                raise Fault("path_denied", "Worktree paths must not contain filesystem links.")
        resolved = parent.resolve(strict=True)
        if not resolved.is_dir():
            raise OSError
    except (OSError, RuntimeError) as error:
        raise Fault("source_unavailable", "Select an existing directory or an absent child of one.") from error
    return resolved / path.name if missing else resolved


def _authorized(config, machine: str, path: Path) -> bool:
    if excluded(Path(*(part.casefold() for part in path.parts))):
        return False
    for item in config.workspaces.values():
        if item.machine == machine:
            try:
                if path.is_relative_to(Path(item.root).resolve(strict=True)):
                    return True
            except (OSError, RuntimeError):
                continue
    return False


def _creation_destination(path: Path) -> Path:
    target = _canonical(path, missing=True)
    ensure_local_objects(target.parent)
    parent = context(target.parent)
    if parent["git_status"] == "unavailable":
        raise Fault("context_unavailable", "The destination parent must be verifiably outside any Git worktree.")
    if parent["git_status"] != "not_a_worktree":
        raise Fault("nested_workspace", "Create outside existing Git worktrees so their active writer locks remain respected.")
    return target


def _registered(root: Path) -> tuple[list[dict], bytes]:
    raw, code, clipped = capture(root, ["worktree", "list", "--porcelain", "-z"])
    if code or clipped:
        raise Fault("git_unavailable", "Git worktree registration could not be captured completely.")
    entries = []
    try:
        for record in raw.split(b"\0\0"):
            if not record:
                continue
            item = {"branch": None, "head_sha": None, "detached": False,
                    "locked": False, "prunable": False, "bare": False}
            for field in record.split(b"\0"):
                key, _, value = field.partition(b" ")
                if key == b"worktree":
                    item["worktree"] = os.fsdecode(value)
                elif key == b"HEAD":
                    item["head_sha"] = value.decode("ascii")
                elif key == b"branch":
                    item["branch"] = os.fsdecode(value).removeprefix("refs/heads/")
                elif key in {b"detached", b"locked", b"prunable", b"bare"}:
                    item[key.decode("ascii")] = True
            if "worktree" not in item or not Path(item["worktree"]).is_absolute():
                raise ValueError
            entries.append(item)
    except (UnicodeError, ValueError) as error:
        raise Fault("git_unavailable", "Git returned an invalid worktree registration record.") from error
    return entries, raw


def has_registered_worktrees(root: Path) -> bool:
    """Detect other registered worktrees before removing a root's read grant."""
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise Fault("workspace_reference_unavailable", "The original workspace root is unavailable.") from error
    observed = context(resolved)
    if observed["git_status"] == "unavailable":
        raise Fault("workspace_reference_unavailable", "The original repository could not be inspected.")
    if observed["git_status"] != "observed" or observed["worktree"] != str(resolved):
        return False
    return any(item["worktree"] != str(resolved) and not item["prunable"] and not item["bare"]
               for item in _registered(resolved)[0])


def resolve_root(config, machine: str, workspace: str, worktree: str = "") -> Path:
    """Resolve one registered tree without adding a path grant or default binding."""
    selected = _absolute(worktree, allow_empty=True)
    root = _root(config, machine, workspace)
    if not selected:
        return root
    observed = _repository(root)
    common_identity = _identity(Path(observed["common_dir"]))
    if not _authorized(config, machine, Path(selected)):
        raise Fault("path_denied", "The selected worktree is outside this machine's configured roots.")
    candidate = _canonical(Path(selected))
    if not _authorized(config, machine, candidate):
        raise Fault("path_denied", "The selected worktree is outside this machine's configured roots.")
    entries, _ = _registered(root)
    if not any(item["worktree"] == str(candidate) and not item["bare"] and not item["prunable"] for item in entries):
        raise Fault("unknown_worktree", "Select an available worktree registered to this repository.")
    selected_context = _repository(candidate)
    after = _repository(root)
    if (selected_context["common_dir"] != observed["common_dir"]
            or after["workspace_identity"] != observed["workspace_identity"]
            or _identity(Path(selected_context["common_dir"])) != common_identity):
        raise Fault("context_mismatch", "The selected worktree no longer belongs to the observed repository.")
    return candidate


def select(config, machine: str, workspace: str, worktree: str = ""):
    """Use a request-local root view; never mutate shared configuration."""
    root = resolve_root(config, machine, workspace, worktree)
    if not worktree:
        return config
    selected = copy(config)
    selected.workspaces = dict(config.workspaces)
    item = copy(config.workspaces[workspace])
    item.root = str(root)
    selected.workspaces[workspace] = item
    return selected


def list_worktrees(config, machine: str, workspace: str, offset: int = 0, limit: int = 100,
                  expected_snapshot: str = "", *, worktree: str = "") -> dict:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise Fault("invalid_range", "Use a nonnegative offset and a limit from 1 to 200.")
    # Resolve the selected root without replacing any configured path grants.
    root = resolve_root(config, machine, workspace, worktree)
    observed = _repository(root)
    entries, raw = _registered(root)
    for item in entries:
        try:
            path = Path(_absolute(item["worktree"]))
            item["authorized"] = _authorized(config, machine, path)
        except Fault:
            item["authorized"] = False
    after = _repository(root)
    if after["workspace_identity"] != observed["workspace_identity"]:
        raise Fault("source_changed", "The source repository changed during worktree discovery.")
    fingerprint = hashlib.sha256(json.dumps([observed, entries], sort_keys=True,
                                          ensure_ascii=True).encode() + raw).hexdigest()
    if expected_snapshot and expected_snapshot != fingerprint:
        raise Fault("source_changed", "Worktree registration changed between pages; restart at offset zero.")
    if offset > len(entries):
        raise Fault("invalid_range", "The offset exceeds the registered worktree list.")
    return {"machine": machine, "workspace": workspace, "context": observed,
            "entries": entries[offset:offset + limit], "offset": offset,
            "next_offset": offset + limit if offset + limit < len(entries) else None,
            "has_more": offset + limit < len(entries), "snapshot": fingerprint,
            "truncated": False, "ordering": "Git worktree registration",
            "warnings": ["Registration and path grants do not establish task ownership or a clean worktree."]}


def _branch(value: str) -> str:
    if (not isinstance(value, str) or len(value.encode("utf-8")) > 256 or value == "HEAD"
            or value.startswith(("-", "/")) or value.endswith((".", "/"))
            or ".." in value or "@{" in value or any(char in value for char in " ~^:?*[\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or value and any(not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise Fault("invalid_branch", "Use an absent, valid local branch name, or empty for a detached worktree.")
    return value


def normalize_creation(destination: str, base_ref: str, new_branch: str,
                       expected_workspace_identity: str, worktree: str = "") -> dict:
    """Validate immutable input without Git, path reads, allocation or effects."""
    destination = _absolute(destination)
    worktree = _absolute(worktree, allow_empty=True)
    try:
        if (not isinstance(base_ref, str) or not 1 <= len(base_ref.encode("utf-8")) <= 256
                or base_ref.startswith("-") or any(ord(char) < 32 or ord(char) == 127 for char in base_ref)):
            raise ValueError
        branch = _branch(new_branch)
    except (UnicodeError, ValueError) as error:
        raise Fault("invalid_ref", "Supply a bounded explicit Git commit reference and valid branch name.") from error
    if not isinstance(expected_workspace_identity, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_workspace_identity):
        raise Fault("invalid_precondition", "Retain the complete workspace identity from source inspection.")
    return {"destination": destination, "base_ref": base_ref, "new_branch": branch,
            "expected_workspace_identity": expected_workspace_identity, "worktree": worktree}


def _commit(root: Path, ref: str) -> str:
    raw, code, clipped = capture(root, ["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"])
    value = raw.decode("ascii", errors="replace").strip()
    if code or clipped or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise Fault("invalid_ref", "The explicit base must resolve to an existing local Git commit; no fetch was attempted.")
    return value


def _absent_branch(root: Path, branch: str) -> None:
    if not branch:
        return
    _, code, clipped = capture(root, ["check-ref-format", "--branch", branch])
    if code or clipped:
        raise Fault("invalid_branch", "Select a valid local branch name.")
    _, code, clipped = capture(root, ["show-ref", "--verify", "--quiet", "refs/heads/" + branch])
    if clipped or code not in (0, 1):
        raise Fault("git_unavailable", "The requested branch could not be checked.")
    if code == 0:
        raise Fault("branch_exists", "The branch already exists; discover a reusable worktree or choose an absent branch.")


def _filters(root: Path) -> list[str]:
    # A conditional include can add a previously unseen driver when Git's
    # checkout subprocess enters the new gitdir or branch.
    conditional, code, clipped = capture(root, ["config", "--null", "--name-only", "--get-regexp",
                                              r"^includeif\..*\.path$"])
    if clipped or code not in (0, 1) or conditional:
        raise Fault("git_config_unavailable", "Conditional Git includes cannot be neutralized safely during bounded worktree creation.")
    raw, code, clipped = capture(root, ["config", "--null", "--name-only", "--get-regexp",
                                      r"^filter\..*\.(clean|smudge|process)$"])
    if clipped or code not in (0, 1):
        raise Fault("git_config_unavailable", "External filter configuration could not be checked.")
    try:
        names = sorted({name.decode("utf-8").rsplit(".", 1)[0] for name in raw.split(b"\0") if name})
        if len(names) > MAX_FILTERS or any(not _filter_name(name) for name in names):
            raise ValueError
    except (UnicodeError, ValueError) as error:
        raise Fault("git_config_unavailable", "External filter names exceed the bounded worktree creation contract.") from error
    return names


def _filter_name(value: str) -> bool:
    return (isinstance(value, str) and value.startswith("filter.") and 7 < len(value) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in value) and "=" not in value)


def creation_argv(plan: dict) -> list[str]:
    flags = [item for name in plan["filters"] for setting in ("clean=", "smudge=", "process=", "required=false")
             for item in ("-c", name + "." + setting)]
    return ["git", "--no-optional-locks", "--no-pager", "--literal-pathspecs",
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "submodule.recurse=false", "-c", "core.sparseCheckout=false", *flags,
            "-C", plan["source_root"], "worktree", "add",
            *(["-b", plan["branch"]] if plan["branch"] else ["--detach"]),
            "--", plan["destination"], plan["base_commit"]]


def validate_creation_plan(plan: dict) -> None:
    """Validate a retained plan and its exact command without observing the filesystem."""
    required = {"version", "source_root", "source_identity", "source_root_id", "source_git_dir",
                "destination", "parent_identity", "base_commit", "branch", "common_dir",
                "common_identity", "filters", "resource_keys", "argv"}
    try:
        if not isinstance(plan, dict) or set(plan) != required or type(plan["version"]) is not int or plan["version"] != 1:
            raise ValueError
        for name in ("source_root", "destination", "source_git_dir", "common_dir"):
            if _absolute(plan[name], control_path=name in {"source_git_dir", "common_dir"}) != plan[name]:
                raise ValueError
        for name in ("source_root_id", "parent_identity", "common_identity"):
            if (not isinstance(plan[name], list) or len(plan[name]) != 2
                    or any(type(value) is not int or value < 0 for value in plan[name])):
                raise ValueError
        if (not isinstance(plan["base_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", plan["base_commit"])
                or not isinstance(plan["source_identity"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["source_identity"])):
            raise ValueError
        _branch(plan["branch"])
        names = plan["filters"]
        if (not isinstance(names, list) or len(names) > MAX_FILTERS or any(not _filter_name(name) for name in names)
                or names != sorted(set(names))):
            raise ValueError
        key = "git-common:" + hashlib.sha256(plan["common_dir"].encode()).hexdigest()
        if plan["resource_keys"] != [key] or plan["argv"] != creation_argv(plan):
            raise ValueError
    except (Fault, TypeError, ValueError, UnicodeError) as error:
        raise Fault("invalid_creation_plan", "The retained worktree plan or fixed Git command is invalid.") from error


def prepare_creation(config, machine: str, workspace: str, destination: str, base_ref: str,
                     new_branch: str, expected_workspace_identity: str, worktree: str = "") -> dict:
    request = normalize_creation(destination, base_ref, new_branch, expected_workspace_identity, worktree)
    source = resolve_root(config, machine, workspace, request["worktree"])
    observed = _repository(source)
    if observed["workspace_identity"] != request["expected_workspace_identity"]:
        raise Fault("context_mismatch", "The source workspace identity changed; inspect it before creating a worktree.")
    if not _authorized(config, machine, Path(request["destination"])):
        raise Fault("path_denied", "The destination must stay inside a configured root on this machine.")
    target = _creation_destination(Path(request["destination"]))
    if not _authorized(config, machine, target):
        raise Fault("path_denied", "The destination must stay inside a configured root on this machine.")
    if os.path.lexists(target):
        raise Fault("destination_exists", "Preserve the existing destination and discover its state before retrying.")
    entries, _ = _registered(source)
    if any(item["worktree"] == str(target) for item in entries):
        raise Fault("destination_exists", "Git already registers this destination; inspect the original worktree.")
    common = Path(observed["common_dir"])
    if target.is_relative_to(common) or common.is_relative_to(target):
        raise Fault("path_denied", "The destination must not contain or enter repository control metadata.")
    base = _commit(source, request["base_ref"])
    _absent_branch(source, request["new_branch"])
    plan = {"version": 1, "source_root": str(source), "source_identity": observed["workspace_identity"],
            "source_root_id": observed["root_id"], "source_git_dir": observed["git_dir"],
            "destination": str(target), "parent_identity": _identity(target.parent),
            "base_commit": base, "branch": request["new_branch"], "common_dir": str(common),
            "common_identity": _identity(common), "filters": _filters(source),
            "resource_keys": ["git-common:" + hashlib.sha256(str(common).encode()).hexdigest()]}
    plan["argv"] = creation_argv(plan)
    validate_creation_plan(plan)
    validate_creation(plan)
    return plan


def _source_context(plan: dict) -> dict:
    source = _canonical(Path(plan["source_root"]))
    observed = _repository(source)
    if (observed["root_id"] != plan["source_root_id"] or observed["git_dir"] != plan["source_git_dir"]
            or observed["common_dir"] != plan["common_dir"]
            or _identity(Path(plan["common_dir"])) != plan["common_identity"]):
        raise Fault("context_mismatch", "The source repository identity changed after creation was prepared.")
    return observed


def validate_creation(plan: dict) -> None:
    """Recheck only required source facts; a normal source HEAD advance is allowed."""
    validate_creation_plan(plan)
    _source_context(plan)
    source, target = Path(plan["source_root"]), Path(plan["destination"])
    if (_creation_destination(target) != target or _identity(target.parent) != plan["parent_identity"]):
        raise Fault("context_mismatch", "The destination parent changed after creation was prepared.")
    if os.path.lexists(target):
        raise Fault("destination_exists", "The destination appeared before creation; preserve its contents.")
    entries, _ = _registered(source)
    if any(item["worktree"] == str(target) for item in entries):
        raise Fault("destination_exists", "Git already registers the destination; inspect the original operation.")
    if _commit(source, plan["base_commit"]) != plan["base_commit"] or _filters(source) != plan["filters"]:
        raise Fault("context_mismatch", "The pinned base or external filter configuration changed before creation.")
    _absent_branch(source, plan["branch"])


def verify_creation(plan: dict) -> dict:
    """Confirm registration and the created checkout; process exit alone is insufficient."""
    validate_creation_plan(plan)
    _source_context(plan)
    root = _canonical(Path(plan["destination"]))
    _creation_destination(root)
    if _identity(root.parent) != plan["parent_identity"]:
        raise Fault("context_mismatch", "The destination parent changed during creation.")
    entries, _ = _registered(Path(plan["source_root"]))
    if not any(item["worktree"] == str(root) and not item["prunable"] and not item["bare"] for item in entries):
        raise Fault("unknown_worktree", "The created directory is not a verified Git worktree registration.")
    observed = _repository(root)
    if (observed["head_sha"] != plan["base_commit"] or observed["branch"] != (plan["branch"] or None)
            or observed["common_dir"] != plan["common_dir"]
            or _identity(Path(observed["common_dir"])) != plan["common_identity"]):
        raise Fault("context_mismatch", "The created worktree does not match its original pinned plan.")
    return observed
