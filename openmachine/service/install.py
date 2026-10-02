"""Prepare and verify immutable runtime installations; never select a version."""

from email.parser import BytesParser
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

from openmachine.config import load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import private_directory, save_json

from . import state as service_state
from . import checks


def _prepare(state: Path, wheel: Path, requirements: Path, config_path: Path) -> str:
    """Build a venv at its final path: Python environments must not be moved."""
    try:
        wheel = wheel.resolve(strict=True)
        requirements = requirements.resolve(strict=True)
        if wheel.suffix != ".whl" or not wheel.is_file() or not requirements.is_file():
            raise ValueError("expected local artifacts")
        with wheel.open("rb") as source:
            wheel_hash = hashlib.file_digest(source, "sha256").hexdigest()
        locked = requirements.read_bytes()
        lines = [line.strip() for line in locked.decode().splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not lines or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+!-]*", line) for line in lines):
            raise ValueError("requirements must pin every dependency")
        with zipfile.ZipFile(wheel) as archive:
            metadata = [name for name in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", name)]
            if len(metadata) != 1:
                raise ValueError("expected one distribution")
            package = BytesParser().parsebytes(archive.read(metadata[0]))
            if package["Name"] != "openmachine" or not package["Version"]:
                raise ValueError("wrong distribution")
            version = package["Version"]
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile) as error:
        raise Fault("invalid_installation_artifact", "Select a trusted local Open Machine wheel and fully pinned requirements file.") from error
    digest = hashlib.sha256((wheel_hash + hashlib.sha256(locked).hexdigest() + sys.version).encode()).hexdigest()
    versions = private_directory(state / "service/versions")
    destination = versions / digest
    if destination.exists():
        original = service_state._installed(state, digest)
        if original.get("wheel_sha256") != wheel_hash or original.get("version") != version:
            raise Fault("installation_conflict", "The retained installation does not match the selected artifact.")
        return digest
    destination.mkdir(mode=0o700)
    artifacts = private_directory(destination / "artifacts")
    with wheel.open("rb") as source, (artifacts / wheel.name).open("xb") as output:
        shutil.copyfileobj(source, output)
    wheel = artifacts / wheel.name
    with wheel.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != wheel_hash:
            raise Fault("installation_artifact_changed", "The wheel changed during preparation; the active installation is untouched.")
    requirements = artifacts / "requirements.lock"
    requirements.write_bytes(locked)
    # Keep failed candidates for diagnosis. Never overwrite or garbage-collect a runtime.
    for argv in ([sys.executable, "-I", "-m", "venv", str(destination)],
                 [str(destination / "bin/python"), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                  "--no-input", "--no-deps", "-r", str(requirements)],
                 [str(destination / "bin/python"), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                  "--no-input", "--no-deps", str(wheel)],
                 [str(destination / "bin/python"), "-I", "-m", "pip", "check"]):
        if checks._run(argv, timeout=180).returncode:
            raise Fault("installation_failed", "Candidate installation failed; the active runtime is untouched and the candidate is retained.")
    observed = checks._candidate_state(destination / "bin/python", config_path, state)
    if observed["version"] != version or observed["prefix"] != str(destination):
        raise Fault("installation_identity_mismatch", "Wheel metadata, installed runtime and environment identity disagree.")
    save_json(destination / "installation.json", {
        "identity": digest, "version": version, "protocol": observed["protocol"], "state_contract": observed["state_contract"],
        "wheel_sha256": wheel_hash, "requirements_sha256": hashlib.sha256(locked).hexdigest(),
    })
    return digest


def _verify_candidate(state: Path, record: dict, identity: str, *, check_remote: bool = False) -> None:
    installed = service_state._installed(state, identity)
    python = service_state._runtime(state, identity) / "bin/python"
    observed = checks._candidate_state(python, Path(record["config"]), state, installed["state_contract"])
    if observed["version"] != installed["version"] or observed["prefix"] != str(python.parent.parent):
        raise Fault("installation_identity_mismatch", "The retained runtime changed identity; it will not be activated.")
    if check_remote and installed["state_contract"] >= 4:
        for name, target in load(Path(record["config"])).machines.items():
            if target.transport == "ssh":
                checks._remote_workspace_state(python, Path(record["config"]), name)
    with tempfile.TemporaryDirectory(prefix="om-ready-", dir="/tmp") as temporary:
        test_state = Path(temporary) / "state"
        process = subprocess.Popen([str(python), "-I", "-m", "openmachine.cli", "--config", record["config"],
                                    "--state-dir", str(test_state), "serve"], cwd="/",
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not (test_state / "mcp.sock").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise Fault("candidate_unready", "The candidate did not create its private MCP listener; the active service is untouched.")
                time.sleep(0.05)
            checks.probe_service(test_state, Path(record["config"]), record["probe"], installed["version"], process.pid,
                          expected_contract=installed["state_contract"], check_remote=check_remote)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
