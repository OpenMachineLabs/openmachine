"""The machines-only Core configuration: [machines.local] with transport "local"."""

import os
from pathlib import Path
import stat
import tomllib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openmachine.foundation.sanitize import Fault

DEFAULT_CONFIG = Path.home() / ".config/openmachine/config.toml"
DEFAULT_STATE = Path.home() / ".local/state/openmachine"
TEXT = '[machines.local]\ntransport = "local"\n'


class Machine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transport: Literal["local"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    machines: Annotated[dict[Literal["local"], Machine], Field(min_length=1)]

    def machine(self, name: str) -> Machine:
        if name not in self.machines:
            raise Fault("unknown_machine", "Select the machine named in the Core configuration.")
        return self.machines[name]


def load(path: Path) -> Settings:
    try:
        with path.open("rb") as source:
            return Settings.model_validate(tomllib.load(source))
    except (ValidationError, tomllib.TOMLDecodeError) as error:
        # Validation exceptions contain input values; do not print them.
        raise Fault("invalid_config", 'The configuration must hold exactly [machines.local] with transport "local".') from error
    except OSError as error:
        raise Fault("config_unavailable", "Cannot read configuration; run manage configure or select an existing --config file.") from error


def initialize(path: Path, *, verify_existing: bool = False) -> dict:
    """The one first-run writer; never adopt or overwrite."""
    if verify_existing:
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or path.read_bytes() != TEXT.encode()):
            raise Fault("configuration_mismatch", "The existing private configuration is not the first-run configuration.")
        return {"verified": str(path)}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(TEXT)
        output.flush()
        os.fsync(output.fileno())
    return {"created": str(path)}
