"""Explicit component assembly; constructing an application never probes or writes."""

from pathlib import Path

import integrations.chatgpt as chatgpt
import integrations.codex as codex
from integrations.connections import Connections
from openmachine.app import Application
from openmachine.app.machines import Machines
from openmachine.config import Settings


def create_application(config: Settings, state: Path) -> Application:
    ssh_call = None
    ssh_recovery = None
    if any(machine.transport == "ssh" for machine in config.machines.values()):
        from integrations.ssh.client import call
        from integrations.ssh import recovery
        ssh_call = call
        ssh_recovery = recovery
    provider = None
    native = None
    if config.github is not None:
        from integrations import github
        from integrations.github import auth
        native = auth
        provider = github
    connections = Connections(config, state, native)
    return Application(config, state, Machines(config, state, ssh_call, ssh_recovery), provider,
                       connections=connections, codex=codex, chatgpt=chatgpt)
