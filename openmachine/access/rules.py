"""Cloud-delivered operation scope: validate, re-verify identities and check targets."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openmachine.access import macos
from openmachine.chat import projects, workspace
from openmachine.config import DEFAULT_CONFIG, DEFAULT_STATE
from openmachine.foundation.sanitize import Fault

PROTOCOL = 1
MAX_ROOTS = 32
TOOLS = ("apply_workspace_patch", "control_task", "create_task", "create_worktree", "edit_workspace_file",
         "inspect_context", "inspect_workspace", "read_workspace_file", "start_codex_task", "start_execution",
         "write_workspace_file")
SAFE_INTEGER = 2**53 - 1
PROTECTED_DIRECTORIES = {".vscode", ".idea"}


class ScopeAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    policy_revision: Annotated[int, Field(ge=1, le=SAFE_INTEGER)]
    grant_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
    grant_revision: Annotated[int, Field(ge=1, le=SAFE_INTEGER)]
    session_generation: Annotated[int, Field(ge=0, le=SAFE_INTEGER)]


class ScopeRoot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    machine: Annotated[str, Field(min_length=1, max_length=128)]
    workspace_name: Annotated[str, Field(min_length=1, max_length=1024)]
    relative_path: Annotated[str, Field(min_length=1, max_length=1024)]
    target_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    mode: Literal["read_only", "full_access"]


class OperationScope(BaseModel):
    """Selected Projects map injected by the Connector; public MCP callers never supply it."""
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[1]
    authority: ScopeAuthority
    roots: Annotated[list[ScopeRoot], Field(min_length=1, max_length=MAX_ROOTS)]


def capabilities() -> dict:
    reason = macos.unavailable_reason()
    process = ({"backend": macos.BACKEND, "status": "available"} if reason is None else
               {"backend": None, "status": "unavailable", "reason": reason})
    return {"protocol": PROTOCOL, "tools": list(TOOLS), "max_roots": MAX_ROOTS, "typed": "core", "process": process}


def _denied(message: str) -> Fault:
    return Fault("path_out_of_scope", message, effect="known_no_effect")


def protected(path: Path) -> bool:
    """Files Git and IDEs execute later outside the sandbox; the process profile denies the same set.

    Names compare case-insensitively, as macOS volumes and Seatbelt resolve them.
    """
    text = path.as_posix().casefold()
    return (any(part in PROTECTED_DIRECTORIES for part in text.split("/"))
            or re.search(r"(^|/)\.git(/" + macos.PROTECTED_GIT + ")?$", text) is not None)


@dataclass(frozen=True)
class Root:
    path: Path
    mode: str
    target_id: str
    root_id: tuple[int, int]

    def receipt(self) -> dict:
        return {"target_id": self.target_id, "mode": self.mode, "path": str(self.path), "root_id": list(self.root_id)}


@dataclass(frozen=True)
class Scope:
    """Reads cover the machine except default-denied areas; writes only Full roots."""
    authority: dict
    roots: tuple[Root, ...]
    control: tuple[Path, ...]
    denied: tuple[Path, ...]
    exceptions: tuple[Path, ...]
    home: Path

    def _controlled(self, path: Path) -> bool:
        return any(path == item or path.is_relative_to(item) for item in self.control)

    def _owner(self, path: Path) -> Root | None:
        """The deepest selected root containing path."""
        if self._controlled(path):
            return None
        matches = [root for root in self.roots if path == root.path or path.is_relative_to(root.path)]
        return max(matches, key=lambda root: len(root.path.parts), default=None)

    def _widens(self, owner: Root | None, mode: str) -> bool:
        return owner is None or owner.mode == "read_only" and mode == "full_access"

    def _worktree(self, path: Path) -> tuple[Path, Root] | None:
        """A registered worktree inherits the mode of the root that holds its common Git dir."""
        observed = workspace.context(path)
        if observed["git_status"] != "observed":
            return None
        top, common = Path(observed["worktree"]), Path(observed["common_dir"])
        owner = self._owner(common)
        if owner is None or top == owner.path or owner.path.is_relative_to(top):
            return None
        source = workspace.context(owner.path)
        if (source["git_status"] != "observed" or Path(source["worktree"]) != owner.path
                or Path(source["common_dir"]) != common):
            return None
        if not any(item["worktree"] == str(top) and not item["prunable"] and not item["bare"]
                   for item in workspace.registered_worktrees(owner.path)):
            return None
        return top, owner

    @staticmethod
    def _reciprocal(path: Path, common: str) -> bool:
        """Registration metadata is writable by scoped work; only the worktree's own pointer proves it."""
        observed = workspace.context(path)
        return (observed["git_status"] == "observed" and observed["worktree"] == str(path)
                and observed["common_dir"] == common and observed["git_dir"] != common)

    def mode(self, path: Path) -> str | None:
        """A registered worktree is more specific than an enclosing selected parent."""
        if self._controlled(path):
            return None
        owner = self._owner(path)
        if owner is not None and owner.mode == "full_access":
            return owner.mode
        derived = self._worktree(path)
        if derived is not None and self._widens(owner, derived[1].mode):
            return derived[1].mode
        return owner.mode if owner is not None else None

    def _readable(self, path: Path) -> bool:
        if self._controlled(path):
            return False
        if self.mode(path) is not None:
            return True  # A selected root re-allows reading inside a default-denied area.
        if any(_inside(path, item) for item in self.denied) and not any(_inside(path, item) for item in self.exceptions):
            return False
        # A directory holding a denied area or Open Machine state cannot be inspected as a whole.
        return not any(_inside(item, path) for item in (*self.denied, *self.control))

    def require(self, path: Path, *, write: bool = False) -> None:
        if not write:
            if not self._readable(path):
                raise _denied("Credentials, ~/Library, media and cloud-synced folders, external volumes and "
                              "Open Machine state are not readable under Selected Projects scope.")
            return
        mode = self.mode(path)
        if mode != "full_access":
            raise _denied("Writes need a Full Cloud-selected project; ask the owner to select this project as Full.")
        if protected(path):
            raise _denied("Git hooks/config and IDE settings are protected under Selected Projects scope.")

    def require_worktree_parent(self, parent: Path, source: Path) -> None:
        """A new worktree is a write: beside its source repository, in a .worktrees folder there, or
        inside a selected folder. Never directly in the home or above it."""
        base = source.parent
        beside = () if _inside(self.home, base) else (base, base / ".worktrees")
        if parent not in beside and self.mode(parent) is None:
            raise _denied("Create the worktree beside its repository, in a .worktrees folder there, or inside a "
                          "Cloud-selected folder.")

    def process_request(self, *, destination: Path | None = None) -> dict:
        """Plain map for execution admission: roots, inherited worktrees and control denials."""
        trees = []
        for root in self.roots:
            observed = workspace.context(root.path)
            if (observed["git_status"] != "observed" or Path(observed["worktree"]) != root.path
                    or observed["git_dir"] != observed["common_dir"]):
                continue
            for item in workspace.registered_worktrees(root.path):
                path = Path(item["worktree"])
                if (item["prunable"] or item["bare"] or path == root.path or not path.is_dir()
                        or self._controlled(path) or not self._widens(self._owner(path), root.mode)
                        or not self._reciprocal(path, observed["common_dir"])):
                    continue
                trees.append({"path": str(path), "source_target_id": root.target_id, "mode": root.mode})
        return {"authority": self.authority, "roots": [root.receipt() for root in self.roots],
                "worktrees": trees, "destination": str(destination) if destination else None,
                "control": [str(path) for path in self.control], "denied": [str(path) for path in self.denied],
                "exceptions": [str(path) for path in self.exceptions]}


def control_paths(state: Path) -> tuple[Path, ...]:
    """Core, Connector and Code host configuration and state; denied to scoped work even inside a grant."""
    candidates = [state, DEFAULT_CONFIG.parent, DEFAULT_STATE, DEFAULT_STATE.parent / "openmachine-connector",
                  DEFAULT_STATE.parent / "openmachine-code"]
    found = []
    for path in candidates:
        resolved = Path(os.path.realpath(path))
        if resolved not in found:
            found.append(resolved)
    return tuple(found)


def _inside(path: Path, other: Path) -> bool:
    return path == other or path.is_relative_to(other)


def compile(config, state: Path, machine: str, value: dict) -> Scope:
    """Validate the whole effective map and re-verify every binding before any effect."""
    try:
        model = OperationScope.model_validate(value)
    except ValidationError as error:
        raise Fault("scope_invalid", "Use operation_scope protocol 1 with 1-32 exact bindings.",
                    effect="known_no_effect") from error
    if (any(item.machine != machine for item in model.roots)
            or len({item.target_id for item in model.roots}) != len(model.roots)):
        raise Fault("scope_invalid", "Scope bindings must target this machine and be distinct.", effect="known_no_effect")
    home = macos.account_home()
    control = control_paths(state)
    roots = []
    for item in model.roots:
        try:
            projects.inspect(config, machine, item.workspace_name, "inspect", item.relative_path,
                             expected_target_id=item.target_id)
            base = workspace.root_for(config, machine, item.workspace_name)
            path = (base if item.relative_path == "." else base.joinpath(*item.relative_path.split("/"))).resolve(strict=True)
            info = path.stat()
        except (Fault, OSError, RuntimeError) as error:
            raise Fault("scope_target_changed", "A Cloud-selected directory is missing, replaced or changed; "
                        "the owner must select it again.", effect="known_no_effect") from error
        if path == Path("/") or _inside(home, path):
            raise Fault("scope_too_broad", "Use device Read Only or Full Access for / or the account home.",
                        effect="known_no_effect")
        if any(_inside(path, item) or _inside(item, path) for item in control):
            raise Fault("scope_conflict", "A selected directory overlaps Open Machine configuration or state.",
                        effect="known_no_effect")
        roots.append(Root(path, item.mode, item.target_id, (info.st_dev, info.st_ino)))
    for root in roots:
        for other in roots:
            if root is other:
                continue
            if root.root_id == other.root_id and root.mode != other.mode:
                raise Fault("scope_conflict", "One directory cannot have two modes.", effect="known_no_effect")
            if root.mode == "read_only" and other.mode == "full_access" and _inside(root.path, other.path):
                raise Fault("scope_conflict", "A Read-only directory inside a Full directory cannot restrict it; "
                            "narrow the Full selection instead.", effect="known_no_effect")
    denied, exceptions = macos.read_denials(home)
    return Scope(model.authority.model_dump(), tuple(roots), control, tuple(denied), tuple(exceptions), home)
