"""Curated, immutable worker archives built from the installed distribution."""

import hashlib
from importlib.resources import files
import io
import os
from pathlib import Path
import stat
import tempfile
import zipfile

from openmachine import __version__
from openmachine.foundation.sanitize import Fault

from . import MAX_BUNDLE


def _archive_bytes(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError
            data = stream.read(MAX_BUNDLE + 1)
    except OSError as error:
        raise Fault("worker_identity_mismatch", "Use an available regular worker archive, not a filesystem link.") from error
    if len(data) > MAX_BUNDLE or path.name != hashlib.sha256(data).hexdigest() + ".pyz":
        raise Fault("worker_identity_mismatch", "The worker bytes do not match the originally configured checksum filename.")
    return data


def build_bundle(directory: Path) -> dict:
    """Build from installed resources; no checkout, dependencies or private state."""
    output = io.BytesIO()
    members = {"__main__.py": b"from integrations.ssh.endpoint import main\nmain()\n"}
    for package, names in {
        "openmachine": ("__init__.py",),
        "openmachine.execution": ("__init__.py", "__main__.py", "api.py", "worker.py", "process.py", "records.py", "logs.py", "control.py"),
        "openmachine.workspace": ("__init__.py", "inspect.py", "git.py", "files.py"),
        "openmachine.foundation": ("__init__.py", "state.py", "files.py", "sanitize.py"),
        "integrations": ("__init__.py",),
        "integrations.ssh": ("__init__.py", "endpoint.py", "bundle.py"),
    }.items():
        for name in names:
            members[f"{package.replace('.', '/')}/{name}"] = files(package).joinpath(name).read_bytes()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(members.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, content)
    data = output.getvalue()
    if len(data) > MAX_BUNDLE:
        raise Fault("worker_size_limit", "The standard-library worker exceeds its distribution bound.")
    digest = hashlib.sha256(data).hexdigest()
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}.pyz"
    fd, temporary = tempfile.mkstemp(prefix=".worker-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            try:
                existing = _archive_bytes(destination)
            except Fault as error:
                raise Fault("worker_conflict", "Preserve the existing artifact; it is not the expected regular worker.") from error
            if existing != data:
                raise Fault("worker_conflict", "Preserve the existing artifact; its content does not match its name.")
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.unlink(temporary)
    return {"version": __version__, "path": str(destination), "sha256": digest, "size_bytes": len(data)}
