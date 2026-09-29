"""Explicit machines and workspaces; no implicit execution authority."""

from pathlib import Path
import re
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from openmachine.sanitize import Fault

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
DEFAULT_CONFIG = Path.home() / ".config/openmachine/config.toml"
DEFAULT_STATE = Path.home() / ".local/state/openmachine"


class Machine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transport: Literal["local", "ssh"]
    execution_enabled: bool = False
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
        elif self.execution_enabled and not self.ssh_identity:
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
    repositories: list[Repository] = Field(min_length=1)
    attribution: bool | None = None


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

    @model_validator(mode="after")
    def configured_machines(self):
        if not self.machines or any(w.machine not in self.machines for w in self.workspaces.values()):
            raise ValueError("workspaces must refer to configured machines")
        return self

    def machine(self, name: str, *, write: bool = False) -> Machine:
        if name not in self.machines:
            raise Fault("unknown_machine", "Select a machine returned by get_capabilities.")
        machine = self.machines[name]
        if write and not machine.execution_enabled:
            raise Fault("execution_disabled", "The operator must explicitly enable this machine in configuration.")
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
