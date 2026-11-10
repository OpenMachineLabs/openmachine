"""Install registration and the selection change; the caller owns the operation lock."""

import os
from pathlib import Path

from openmachine.foundation.state import save_json

from . import state as service_state


def register(config_path: Path, state: Path, identity: str) -> None:
    """Pin the management entry to this installation's payload interpreter, then record the first selection."""
    installed = service_state._installed(state, identity)
    python = state / "service/payloads" / installed["payload_sha256"] / "python/bin/python3"
    binding = {"identity": identity, "launch_python": str(python.resolve(strict=True))}
    entry = service_state.MANAGER.replace("BINDING", repr((str(state), str(config_path), binding))).encode()
    fd = os.open(state / "service/manage.py", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(entry)
        output.flush()
        os.fsync(output.fileno())
    save_json(state / "service/service.json", {"config": str(config_path), "current": identity, "management": binding})


def select(state: Path, identity: str) -> None:
    service_state._installed(state, identity)
    save_json(state / "service/service.json", service_state._record(state) | {"current": identity})
