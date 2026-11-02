"""Explicit component assembly; constructing an application never probes or writes."""

from pathlib import Path

import integrations.codex as codex
from integrations.connections import Connections
from openmachine.app import Application
from openmachine.app.machines import Machines
from openmachine.config import Settings


def create_application(config: Settings, state: Path) -> Application:
    provider = None
    native = None
    if config.github is not None:
        from integrations import github
        from integrations.github import auth
        native = auth
        provider = github
    connections = Connections(config, state, native)
    return Application(config, state, Machines(config, state), provider,
                       connections=connections, codex=codex)
