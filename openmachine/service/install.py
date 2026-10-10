"""Prepare and verify immutable runtime installations; never select a version."""

from email.parser import BytesParser
import hashlib
import os
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
from openmachine.foundation.state import lease, private_directory, save_json

from . import checks, payload, processes
from . import state as service_state


def _prepare_commit(state: Path, commit: str, config_path: Path) -> str:
    """Build the exact official source, then retain it through the existing installer."""
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise Fault("invalid_commit", "Select a full 40-character lowercase Git commit SHA.")
    with tempfile.TemporaryDirectory(prefix="om-commit-") as temporary:
        root = Path(temporary).resolve()
        source, wheels = root / "source", root / "wheels"
        source.mkdir()
        wheels.mkdir()
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(root), "LANG": "en_US.UTF-8",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
               "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        # Only fetching may use the user's existing noninteractive Git credentials.
        fetch_env = {key: value for key, value in env.items() if not key.startswith("GIT_CONFIG_")}
        fetch_env["HOME"] = str(Path.home())
        fetch_env.update({key: os.environ[key] for key in ("SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "GH_CONFIG_DIR") if key in os.environ})
        git = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-C", str(source)]
        for command in (["init", "--quiet", "--template="],
                        ["fetch", "--quiet", "--depth=1", "--no-tags", "--no-recurse-submodules",
                         "https://github.com/OpenMachineLabs/openmachine.git", commit],
                        ["checkout", "--quiet", "--detach", "FETCH_HEAD"]):
            if processes.run([*git, *command], timeout=180, env=fetch_env if command[0] == "fetch" else env).returncode:
                raise Fault("commit_fetch_failed", "The selected source could not be checked out; the active installation is untouched.")
        observed = processes.run([*git, "rev-parse", "--verify", "HEAD^{commit}"], env=env)
        if observed.returncode or observed.stdout.strip() != commit:
            raise Fault("commit_unverified", "The checked-out source differs from the requested commit; no candidate was built.")
        built = processes.run([sys.executable, "-I", "-m", "pip", "wheel", "--quiet", "--no-input",
                               "--no-deps", "--no-cache-dir", "--index-url", "https://pypi.org/simple",
                               "--wheel-dir", str(wheels), str(source)], timeout=300, env=env)
        candidates = list(wheels.glob("*.whl"))
        if built.returncode or len(candidates) != 1:
            raise Fault("commit_build_failed", "The selected commit did not build one wheel; the active installation is untouched.")
        return _prepare(state, candidates[0], source / "requirements.lock", config_path, commit=commit)


def _prepare(state: Path, wheel: Path, requirements: Path, config_path: Path, *,
             offline: tuple[Path, str, dict] | None = None, commit: str | None = None) -> str:
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
    if offline is not None:
        if version != offline[2]["core"]["version"] or commit is not None and commit != offline[2]["core"]["commit"]:
            raise Fault("installation_identity_mismatch", "The payload declaration and Core wheel disagree.")
        commit = offline[2]["core"]["commit"]
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise Fault("invalid_commit", "Select a full 40-character lowercase Git commit SHA.")
    configuration = service_state._snapshot_configuration(state, config_path)
    config_path = service_state._saved_configuration(state, configuration)
    runtime_identity = offline[1] if offline else sys.version + ("\ncommit:" + commit if commit is not None else "")
    digest = hashlib.sha256((wheel_hash + hashlib.sha256(locked).hexdigest() + runtime_identity + configuration).encode()).hexdigest()
    versions = private_directory(state / "service/versions")
    destination = versions / digest
    if destination.exists():
        original = service_state._installed(state, digest)
        if (original.get("wheel_sha256") != wheel_hash or original.get("version") != version
                or offline and original.get("payload_sha256") != offline[1]
                or commit is not None and original.get("commit") != commit and (offline is None or "commit" in original)):
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
    # Both paths share the installer. Offline input changes its interpreter and
    # dependency source, never service selection or the retained-state readers.
    python = str(offline[0] / "python/bin/python3") if offline else sys.executable
    options = ["--no-index", "--find-links", str(offline[0] / "wheels"), "--only-binary=:all:"] if offline else []
    isolated = {"env": payload.environment(offline[0])} if offline else {}
    pip = [str(destination / "bin/python"), "-I", "-m", "pip"]
    install = ["install", "--disable-pip-version-check", "--no-input", "--no-deps", *options]
    # Keep failed candidates for diagnosis. Never overwrite or garbage-collect a runtime.
    for argv in ([python, "-I", "-m", "venv", str(destination)],
                 [*pip, *install, "-r", str(requirements)],
                 [*pip, *install, str(wheel)], [*pip, "check"]):
        if processes.run(argv, timeout=180, **isolated).returncode:
            raise Fault("installation_failed", "Candidate installation failed; the active runtime is untouched and the candidate is retained.")
    observed = checks._candidate_state(destination / "bin/python", config_path, state, **isolated)
    if observed["version"] != version or observed["prefix"] != str(destination):
        raise Fault("installation_identity_mismatch", "Wheel metadata, installed runtime and environment identity disagree.")
    save_json(destination / "installation.json", {
        "identity": digest, "version": version, "protocol": observed["protocol"], "state_contract": observed["state_contract"],
        "management_contract": observed.get("management_contract", 0),
        "wheel_sha256": wheel_hash, "requirements_sha256": hashlib.sha256(locked).hexdigest(),
        **({"payload_sha256": offline[1]} if offline else {}),
        **({"commit": commit} if commit is not None else {}),
    })
    return digest


def prepare_payload(config_path: Path, state: Path, source: Path, expected_sha256: str) -> dict:
    """Prepare only: no LaunchAgent, active selection, background policy or worker changes."""
    if not all(path.is_absolute() for path in (config_path, state, source)):
        raise Fault("invalid_service_path", "Payload preparation requires absolute configuration, state and payload paths.")
    load(config_path)
    # Validate before creating a state directory or executing any payload entry.
    payload.verify(source, expected_sha256)
    state = private_directory(state)
    root = private_directory(state / "service")
    with lease(root / "operation.lock"):
        return _prepare_payload(config_path, state, source, expected_sha256)


def _prepare_payload(config_path: Path, state: Path, source: Path, expected_sha256: str) -> dict:
    """Caller owns operation.lock, including an inherited maintenance lease."""
    retained, manifest = payload.retain(state, source, expected_sha256)
    env = payload.environment(retained)
    python = retained / "python/bin/python3"
    observed_python = processes.run([str(python), "-I", "-B", "-c",
        "import platform; print(platform.python_version())"], env=env)
    observed_git = processes.run([str(retained / "bin/git"), "--version"], env=env)
    if (observed_python.returncode or observed_python.stdout.strip() != manifest["python"]["version"]
            or observed_git.returncode or observed_git.stdout.strip() != "git version " + manifest["git"]["version"]):
        raise Fault("installation_identity_mismatch", "The retained Python or Git does not match the payload declaration.")
    identity = _prepare(state, retained / manifest["core"]["wheel"], retained / "requirements.lock",
                        config_path, offline=(retained, expected_sha256, manifest))
    installed = service_state._installed(state, identity)
    return {"status": "prepared", "identity": identity, "version": installed["version"],
            "payload_sha256": expected_sha256, "runtime": str(service_state._runtime(state, identity)),
            "launch_python": str(python.resolve()), "path_environment": env["PATH"],
            "selection": "unchanged", "service": "not_started"}



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
                    raise Fault("candidate_unready", "The candidate did not create its private MCP listener; readiness remains unverified.")
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
