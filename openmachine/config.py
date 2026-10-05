"""Explicit machines and workspaces; no implicit execution authority."""

import hashlib
import json
import os
from pathlib import Path
import re
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
    transport: Literal["local", "ssh"]
    execution_enabled: bool = False
    workspace_write_enabled: bool | None = None
    ssh_alias: Name | None = None
    ssh_worker: str | None = None
    ssh_identity: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = ""

    @model_validator(mode="after")
    def transport_settings(self):
        if self.transport == "local":
            if self.ssh_alias is not None or self.ssh_worker is not None or self.ssh_identity:
                raise ValueError("local machines do not accept SSH settings")
        elif (self.ssh_alias is None or self.ssh_worker is None
              or not self.ssh_worker.startswith("/") or "\0" in self.ssh_worker
              or len(self.ssh_worker) > 4096
              or not re.fullmatch(r"[0-9a-f]{64}\.pyz", Path(self.ssh_worker).name)):
            raise ValueError("SSH requires an alias and an absolute content-addressed worker archive")
        elif (self.execution_enabled or self.workspace_write_enabled is True) and not self.ssh_identity:
            raise ValueError("probe and explicitly pin the SSH identity before enabling execution")
        return self


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


class ChatRecoveryConfig(BaseModel):
    """One operator-selected existing browser route, not provider credentials."""
    model_config = ConfigDict(extra="forbid", strict=True)
    workspace: Name
    connection: str = Field(min_length=1, max_length=200)
    browser_account: str = Field(min_length=1, max_length=200)
    browser_workspace: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)

    @field_validator("connection", "browser_account", "browser_workspace", "model")
    @classmethod
    def visible_label(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("recovery labels must be explicit single-line selections")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    machines: dict[Name, Machine]
    workspaces: dict[Name, Workspace] = Field(default_factory=dict)
    github: GitHubConfig | None = None
    skills: SkillSources = Field(default_factory=SkillSources)
    chat_recovery: ChatRecoveryConfig | None = None

    @model_validator(mode="after")
    def configured_machines(self):
        if not self.machines or any(w.machine not in self.machines for w in self.workspaces.values()):
            raise ValueError("workspaces must refer to configured machines")
        if self.chat_recovery is not None:
            controller = self.workspaces.get(self.chat_recovery.workspace)
            if controller is None or self.machines[controller.machine].transport != "local":
                raise ValueError("recovery requires one configured local controller workspace")
        return self

    def machine(self, name: str, *, write: bool = False) -> Machine:
        if name not in self.machines:
            raise Fault("unknown_machine", "Select a machine returned by get_capabilities.")
        machine = self.machines[name]
        if write and not machine.execution_enabled:
            raise Fault("execution_disabled", "The operator must explicitly enable this machine in configuration.")
        return machine

    def configuration_sha256(self) -> str:
        # Null is the inherited legacy grant, not a change to old config identity.
        value = self.model_dump()
        if value["chat_recovery"] is None:
            del value["chat_recovery"]  # Keep existing configuration identities stable.
        for machine in value["machines"].values():
            if machine["workspace_write_enabled"] is None:
                del machine["workspace_write_enabled"]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def workspace_machine(self, name: str) -> Machine:
        machine = self.machine(name)
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
        raise Fault("invalid_config", "Check the schema, transport settings, absolute roots, machine references, and integration settings.") from error
    except OSError as error:
        raise Fault("config_unavailable", "Cannot read configuration; run init or select an existing --config file.") from error


def initialize(path: Path, workspace: Path | None = None, *, commands: bool = False,
               workspace_writes: bool = False) -> dict:
    """One first-run writer shared by init and management; never adopt or overwrite."""
    if type(commands) is not bool or type(workspace_writes) is not bool:
        raise Fault("invalid_config", "First-run grants must be explicit booleans.")
    text = ('schema_version = 1\n\n[machines.local]\ntransport = "local"\n'
            f'execution_enabled = {str(commands).lower()}\n'
            f'workspace_write_enabled = {str(workspace_writes).lower()}\n')
    if workspace is not None:
        if not workspace.is_absolute() or not workspace.is_dir():
            raise Fault("invalid_workspace", "The workspace must be an existing absolute directory.")
        text += '\n[workspaces.project]\nmachine = "local"\nroot = ' + json.dumps(str(workspace.resolve())) + '\n'
    Settings.model_validate(tomllib.loads(text))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    return {"created": str(path), "execution_enabled": commands, "workspace_write_enabled": workspace_writes,
            "configuration_sha256": load(path).configuration_sha256()}
