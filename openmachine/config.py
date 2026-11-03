"""Explicit machines and workspaces; no implicit execution authority."""

import hashlib
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from openmachine.foundation.sanitize import Fault

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Owner = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")]
REPOSITORY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$"
Repository = Annotated[str, Field(pattern=REPOSITORY_PATTERN)]
DEFAULT_CONFIG = Path.home() / ".config/openmachine/config.toml"
DEFAULT_STATE = Path.home() / ".local/state/openmachine"


class Machine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transport: Literal["local"]
    local_access: Literal["read_only", "full_access"] | None = None
    execution_enabled: bool = False
    workspace_write_enabled: bool | None = None


class Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    machine: Name
    root: str

    @field_validator("root")
    @classmethod
    def absolute_root(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\0" in value:
            raise ValueError("workspace root must be an absolute path")
        return value


class GitHubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # Legacy repositories retain their exact read/write grant, never an owner grant.
    repositories: list[Repository] = Field(default_factory=list, max_length=256)
    read_repositories: list[Repository] = Field(default_factory=list, max_length=256)
    write_repositories: list[Repository] = Field(default_factory=list, max_length=256)
    read_owners: list[Owner] = Field(default_factory=list, max_length=256)
    write_owners: list[Owner] = Field(default_factory=list, max_length=256)
    account: Owner | None = None
    attribution: bool = True

    @model_validator(mode="after")
    def explicit_scopes(self):
        if self.repositories and (self.read_repositories or self.write_repositories
                                  or self.read_owners or self.write_owners):
            raise ValueError("use either legacy repositories or explicit read/write scopes, not both")
        if any(item.rsplit("/", 1)[1] in {".", ".."}
               for item in self.repositories + self.read_repositories + self.write_repositories):
            raise ValueError("repository names cannot be path navigation")
        return self


class SkillSources(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    roots: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("roots")
    @classmethod
    def absolute_roots(cls, values: list[str]) -> list[str]:
        if any(not Path(value).is_absolute() or Path(value) == Path("/") or "\0" in value for value in values):
            raise ValueError("Skill sources must be explicit absolute non-root directories")
        return list(dict.fromkeys(values))


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    machines: dict[Name, Machine]
    workspaces: dict[Name, Workspace] = Field(default_factory=dict)
    github: GitHubConfig | None = None
    skills: SkillSources = Field(default_factory=SkillSources)
    check_for_updates: bool = True

    @model_validator(mode="after")
    def configured_machines(self):
        if not self.machines or any(w.machine not in self.machines for w in self.workspaces.values()):
            raise ValueError("workspaces must refer to configured machines")
        return self

    def machine(self, name: str, *, write: bool = False) -> Machine:
        if name not in self.machines:
            raise Fault("unknown_machine", "Select a machine returned by get_capabilities.")
        machine = self.machines[name]
        if write and machine.local_access is None:
            raise Fault("local_approval_required", "Explicitly select Read Only or Full Access; historical Workspace approval grants no permission.")
        if write and (not machine.execution_enabled or machine.local_access != "full_access"):
            raise Fault("execution_disabled", "The operator must explicitly enable this machine in configuration.")
        return machine

    def configuration_sha256(self) -> str:
        # Native mode is part of the effective grant and its configuration identity.
        value = self.model_dump()
        # An omitted/default advisory preference preserves legacy configuration identity.
        if value["check_for_updates"]:
            del value["check_for_updates"]
        for machine in value["machines"].values():
            if machine["workspace_write_enabled"] is None:
                del machine["workspace_write_enabled"]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def workspace_machine(self, name: str) -> Machine:
        machine = self.machine(name)
        if machine.local_access is None:
            raise Fault("local_approval_required", "Explicit local Full Access is required for native writes.")
        if machine.local_access == "read_only":
            raise Fault("workspace_write_disabled", "Whole-device Read Only does not authorize mutations.")
        if machine.workspace_write_enabled is None:
            return self.machine(name, write=True)
        if not machine.workspace_write_enabled:
            raise Fault("workspace_write_disabled", "Typed workspace writes are disabled for this machine.")
        return machine


def load(path: Path) -> Settings:
    try:
        with path.open("rb") as source:
            return Settings.model_validate(tomllib.load(source))
    except (ValidationError, tomllib.TOMLDecodeError) as error:
        # Validation exceptions contain input values; do not print them.
        raise Fault("invalid_config", "Check the schema, native permissions, absolute roots, machine references, and integration settings.") from error
    except OSError as error:
        raise Fault("config_unavailable", "Cannot read configuration; run init or select an existing --config file.") from error


def initialize(path: Path, workspace: Path | None = None, *, commands: bool = False,
               workspace_writes: bool = False, local_access: str = "read_only",
               verify_existing: bool = False) -> dict:
    """One first-run writer shared by init and management; never adopt or overwrite."""
    if any(type(value) is not bool for value in (commands, workspace_writes, verify_existing)):
        raise Fault("invalid_config", "First-run grants must be explicit booleans.")
    if not isinstance(local_access, str) or local_access not in {"read_only", "full_access"}:
        raise Fault("invalid_config", "Select explicit read_only or full_access local access.")
    text = ('schema_version = 1\n\n[machines.local]\ntransport = "local"\n'
            f'local_access = "{local_access}"\n'
            f'execution_enabled = {str(commands).lower()}\n'
            f'workspace_write_enabled = {str(workspace_writes).lower()}\n')
    if workspace is not None:
        if not workspace.is_absolute() or not workspace.is_dir():
            raise Fault("invalid_workspace", "The workspace must be an existing absolute directory.")
        text += '\n[workspaces.project]\nmachine = "local"\nroot = ' + json.dumps(str(workspace.resolve()), ensure_ascii=False) + '\n'
    try:
        expected = Settings.model_validate(tomllib.loads(text))
    except (ValidationError, tomllib.TOMLDecodeError) as error:
        raise Fault("invalid_config", "First-run settings must contain a valid local workspace.") from error
    if verify_existing:
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or load(path).configuration_sha256() != expected.configuration_sha256()):
            raise Fault("configuration_mismatch", "The existing private configuration does not match the accepted first-run inputs.")
        return {"verified": str(path), "configuration_sha256": expected.configuration_sha256()}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    return {"created": str(path), "execution_enabled": commands, "workspace_write_enabled": workspace_writes,
            "configuration_sha256": load(path).configuration_sha256()}
