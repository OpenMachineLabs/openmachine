"""Cloud-delivered Selected Projects scope: validate it, re-verify every binding and check its shape."""

from dataclasses import dataclass
import os
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openmachine.access import catalog, git, macos
from openmachine.config import DEFAULT_CONFIG, DEFAULT_STATE
from openmachine.foundation.sanitize import Fault

MAX_ROOTS = 32
SAFE_INTEGER = 2**53 - 1


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
    """The Selected Projects map Cloud delivers with a session."""
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[1]
    authority: ScopeAuthority
    roots: Annotated[list[ScopeRoot], Field(min_length=1, max_length=MAX_ROOTS)]


@dataclass(frozen=True)
class Root:
    path: Path
    mode: str
    root_id: tuple[int, int]
    kind: str


@dataclass(frozen=True)
class Scope:
    """Selected roots; the control paths stay denied inside them."""
    roots: tuple[Root, ...]
    control: tuple[Path, ...]

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

    @staticmethod
    def _reciprocal(path: Path, common: str) -> bool:
        """Registration metadata is writable by scoped work; only the worktree's own pointer proves it."""
        observed = git.context(path)
        return (observed["git_status"] == "observed" and observed["worktree"] == str(path)
                and observed["common_dir"] == common and observed["git_dir"] != common)

    def grants(self) -> list[tuple[Path, str]]:
        """Each selected root, and each registered worktree a selected repository widens, with its mode."""
        found = [(root.path, root.mode) for root in self.roots]
        for root in self.roots:
            observed = git.context(root.path)
            if (observed["git_status"] != "observed" or Path(observed["worktree"]) != root.path
                    or observed["git_dir"] != observed["common_dir"]):
                continue
            for item in git.registered_worktrees(root.path):
                path = Path(item["worktree"])
                if (item["prunable"] or item["bare"] or path == root.path or not path.is_dir()
                        or self._controlled(path) or not self._widens(self._owner(path), root.mode)
                        or not self._reciprocal(path, observed["common_dir"])):
                    continue
                found.append((path, root.mode))
        return found


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
    if sys.platform == "darwin":  # macOS volumes match names in any case; refuse every spelling.
        path, other = Path(str(path).casefold()), Path(str(other).casefold())
    return path == other or path.is_relative_to(other)


def bind(config, machine: str, items: list[ScopeRoot], control: tuple[Path, ...]) -> tuple[Root, ...]:
    """Re-verify every binding, then apply the per-binding and pairwise shape rules; no authority is needed."""
    if any(item.machine != machine for item in items) or len({item.target_id for item in items}) != len(items):
        raise Fault("scope_invalid", "Scope bindings must target this machine and be distinct.", effect="known_no_effect")
    home = macos.account_home()
    roots = []
    for item in items:
        try:
            project = catalog.inspect(config, machine, item.workspace_name, "inspect", item.relative_path,
                                      expected_target_id=item.target_id)["project"]
            base = catalog.root_for(item.workspace_name)
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
        roots.append(Root(path, item.mode, (info.st_dev, info.st_ino), project["kind"]))
    for root in roots:
        for other in roots:
            if root is other:
                continue
            if root.root_id == other.root_id and root.mode != other.mode:
                raise Fault("scope_conflict", "One directory cannot have two modes.", effect="known_no_effect")
            if root.mode == "read_only" and other.mode == "full_access" and _inside(root.path, other.path):
                raise Fault("scope_conflict", "A Read-only directory inside a Full directory cannot restrict it; "
                            "narrow the Full selection instead.", effect="known_no_effect")
    return tuple(roots)


def compile(config, state: Path, machine: str, value: dict) -> Scope:
    """Validate the whole effective map and re-verify every binding before any effect."""
    try:
        model = OperationScope.model_validate(value)
    except ValidationError as error:
        raise Fault("scope_invalid", "Use scope protocol 1 with 1-32 exact bindings.",
                    effect="known_no_effect") from error
    control = control_paths(state)
    return Scope(bind(config, machine, model.roots, control), control)
