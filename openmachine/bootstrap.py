"""Explicit component assembly; constructing an application never probes or writes."""

from pathlib import Path

from openmachine.app import Application
from openmachine.app.machines import Machines
from openmachine.config import Settings


def create_application(config: Settings, state: Path) -> Application:
    ssh_call = None
    if any(machine.transport == "ssh" for machine in config.machines.values()):
        from integrations.ssh.client import call
        ssh_call = call
    provider = None
    if config.github is not None:
        from openmachine import github
        provider = github
    return Application(config, state, Machines(config, state, ssh_call), provider)
