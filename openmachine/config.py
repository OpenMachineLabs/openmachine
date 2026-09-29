"""Explicit local machines and workspaces; no implicit execution authority."""

from pathlib import Path
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from openmachine.sanitize import Fault

Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
DEFAULT_CONFIG = Path.home() / ".config/openmachine/config.toml"
DEFAULT_STATE = Path.home() / ".local/state/openmachine"


class Machine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transport: Literal["local"]
    execution_enabled: bool = False


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


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    machines: dict[Name, Machine]
    workspaces: dict[Name, Workspace] = Field(default_factory=dict)

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
        raise Fault("invalid_config", "Check schema version, local transport, absolute roots and machine references.") from error
    except OSError as error:
        raise Fault("config_unavailable", "Cannot read configuration; run init or select an existing --config file.") from error
