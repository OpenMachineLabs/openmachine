"""Fixed stdlib-only setup program sent by the owner over authenticated SSH."""

import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile


def fail(code):
    raise ValueError(code)


def worker_state(path, digest):
    if not os.path.lexists(path):
        return "absent"
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
            or info.st_size > 512 * 1024 or path.resolve() != path):
        fail("worker_conflict")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        fail("worker_conflict")
    return "available"


def inspect(request):
    if sys.platform != "linux" or sys.version_info < (3, 12):
        fail("unsupported_target")
    if shutil.which("git") is None:
        fail("git_unavailable")
    try:
        manager = subprocess.run(["systemctl", "--user", "--no-ask-password", "show", "--property=Version", "--value"],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        match = re.match(r"([0-9]+)", manager.stdout.strip())
        if manager.returncode or match is None or int(match[1]) < 255:
            fail("scope_unavailable")
    except (OSError, subprocess.SubprocessError):
        fail("scope_unavailable")
    digest = request.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        fail("invalid_request")
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    machine_id = Path("/etc/machine-id").read_bytes().strip()
    if not re.fullmatch(rb"[0-9a-f]{32}", machine_id):
        fail("identity_unavailable")
    identity = hashlib.sha256(machine_id + b"\0" + str(os.getuid()).encode() + b"\0" + os.fsencode(home)).hexdigest()
    path = home / ".local/share/openmachine/workers" / (digest + ".pyz")
    value = {"home": str(home), "uid": os.getuid(), "identity": identity, "os": platform.system(), "architecture": platform.machine(),
             "python": platform.python_version(), "systemd": manager.stdout.strip()[:80],
             "worker": str(path), "worker_status": worker_state(path, digest)}
    if "home" in request and (request["home"] != value["home"] or request.get("uid") != value["uid"]
                              or request.get("identity") != identity):
        fail("identity_mismatch")
    return value


def act(request):
    value = inspect(request)
    action = request.get("action")
    if action == "inspect":
        return value
    if action == "project":
        root = request.get("root")
        if not isinstance(root, str) or not root.startswith("/") or len(root) > 4096 or any(ord(c) < 32 for c in root):
            fail("invalid_remote_project")
        try:
            project = Path(root).resolve(strict=True)
        except OSError:
            fail("remote_project_unavailable")
        if project == Path("/") or not project.is_dir() or not os.access(project, os.R_OK | os.X_OK):
            fail("remote_project_unavailable")
        return value | {"root": str(project)}
    if action != "prepare":
        fail("invalid_request")
    raw = base64.b64decode(request.get("archive", ""), validate=True)
    if len(raw) > 512 * 1024 or hashlib.sha256(raw).hexdigest() != request["sha256"]:
        fail("worker_conflict")
    destination = Path(value["worker"])
    if value["worker_status"] == "absent":
        parent = Path(value["home"])
        for part in (".local", "share", "openmachine", "workers"):
            parent /= part
            parent.mkdir(mode=0o700, exist_ok=True)
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                fail("unsafe_worker_directory")
        fd, temporary = tempfile.mkstemp(prefix=".preparing-", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                os.fchmod(stream.fileno(), 0o500)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                worker_state(destination, request["sha256"])
            fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.unlink(temporary)
    value["worker_status"] = worker_state(destination, request["sha256"])
    return value


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            fail("request_limit")
        request = json.loads(raw)
        if not isinstance(request, dict):
            fail("invalid_request")
        result = act(request)
    except ValueError as error:
        code = str(error)
        result = {"error": code if re.fullmatch(r"[a-z_]{1,64}", code) else "invalid_request"}
    except (OSError, KeyError, TypeError):
        result = {"error": "ssh_preparation_unavailable"}
    print(json.dumps(result))
