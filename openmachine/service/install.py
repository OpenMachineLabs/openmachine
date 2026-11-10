"""Build an immutable installation from a retained payload and check it; never select it."""

import json
from pathlib import Path

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory, save_json

from . import payload, processes
from . import state as service_state


def prepare(state: Path, retained: Path, payload_sha256: str) -> dict:
    """Build the installation's environment at its final path, offline: Python environments must not be moved."""
    manifest = payload.verify(retained, payload_sha256)
    env = payload.environment()
    python = retained / "python/bin/python3"
    identity, version = payload_sha256, manifest["core"]["version"]
    destination = private_directory(state / "service/versions") / identity
    if destination.exists():
        return {"identity": identity, "version": service_state._installed(state, identity)["version"]}
    destination.mkdir(mode=0o700)
    pip = [str(destination / "bin/python"), "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
           "--no-deps", "--no-index", "--find-links", str(retained / "wheels"), "--only-binary=:all:"]
    # Keep failed candidates for diagnosis. Never overwrite or garbage-collect an installation.
    for argv in ([str(python), "-I", "-m", "venv", str(destination)], [*pip, "-r", str(retained / "requirements.lock")],
                 [*pip, str(retained / manifest["core"]["wheel"])], [str(destination / "bin/python"), "-I", "-m", "pip", "check"]):
        if processes.run(argv, timeout=180, env=env).returncode:
            raise Fault("installation_failed", "Candidate installation failed; the selection is untouched and the candidate is retained.")
    # The candidate check: the installed access launcher answers with the declared version.
    checked = processes.run([str(destination / "bin/python"), "-I", "-m", "openmachine.access", "capabilities"], env=env)
    try:
        if checked.returncode or json.loads(checked.stdout)["version"] != version:
            raise ValueError("candidate version")
    except (ValueError, KeyError, TypeError) as error:
        raise Fault("installation_identity_mismatch", "The installed Core does not answer access capabilities with the declared version.") from error
    save_json(destination / "installation.json", {
        "identity": identity, "version": version, "protocol": service_state.PROTOCOL,
        "state_contract": service_state.STATE_CONTRACT, "management_contract": service_state.MANAGEMENT_CONTRACT,
        "payload_sha256": payload_sha256, "commit": manifest["core"]["commit"]})
    return {"identity": identity, "version": version}
