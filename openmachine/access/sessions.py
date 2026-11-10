"""Code SSH session requests, the session environment, lifetime records and Full Access admission.

Lifetime records hold only supervision metadata, never commands, output or context, and nothing
here restarts a command.
"""

import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import termios
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openmachine.access import rules
from openmachine.access.macos import seatbelt
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json

PROTOCOL = 2
MAX_REQUEST = 256 * 1024
SHARED = Path("/Users/Shared")
HOME_NAME = "openmachine-code-{uid}"
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
SFTP_SERVERS = ("/usr/libexec/sftp-server", "/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server")
LifetimeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class Terminal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    term: Annotated[str, Field(pattern=r"^[A-Za-z0-9._+-]{1,64}$")]


# A session's routing to its host's GitHub broker: these exact variables, the broker on loopback over TLS
# (the routing pins its key, so whatever else holds the port after the broker fails Git closed), Git keys
# scoped to that broker, and values bounded to printable text.
BROKER = r"https://127\.0\.0\.1:[1-9][0-9]{0,4}"
GITHUB_COUNT = 11


class GitHub(BaseModel):
    """The host's Git routing for one session, and its om-code client, which restricted sessions run as
    Core's copy (github_client in capabilities)."""
    model_config = ConfigDict(extra="forbid", strict=True)
    environment: Annotated[dict[str, Annotated[str, Field(pattern=r"^[\x20-\x7e]{0,1024}$")]],
                           Field(min_length=5, max_length=3 + 2 * GITHUB_COUNT)]
    client: Annotated[str, Field(min_length=2, max_length=1024)]

    @model_validator(mode="after")
    def _routing(self):
        environment, broker = self.environment, self.environment.get("OM_CODE_BROKER", "")
        count = environment.get("GIT_CONFIG_COUNT", "")
        if (not re.fullmatch(BROKER, broker) or int(broker.rsplit(":", 1)[1]) > 65535
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", environment.get("OM_CODE_CAPABILITY", ""))
                or not re.fullmatch(r"[1-9][0-9]?", count) or int(count) > GITHUB_COUNT):
            raise ValueError("github routing needs a loopback broker, a capability and a Git configuration count")
        pairs = [(f"GIT_CONFIG_KEY_{index}", f"GIT_CONFIG_VALUE_{index}") for index in range(int(count))]
        if set(environment) != {"OM_CODE_BROKER", "OM_CODE_CAPABILITY", "GIT_CONFIG_COUNT", *(name for pair in pairs for name in pair)}:
            raise ValueError("github routing carries exactly its own variables")
        scoped = re.compile(rf"url\.{re.escape(broker)}/[\x21-\x7e]*\.insteadOf|(credential|http)\.{re.escape(broker)}\.[A-Za-z]{{1,32}}")
        if not all(scoped.fullmatch(environment[key]) for key, _ in pairs):
            raise ValueError("github routing configures only its broker")
        if (not os.path.isabs(self.client) or os.path.normpath(self.client) != self.client
                or any(char in self.client for char in ':\0"\\') or any(ord(char) < 32 or ord(char) == 127 for char in self.client)):
            raise ValueError("github routing names its client by an exact absolute path")
        return self


class SessionRequest(BaseModel):
    """One SSH channel after Go verified Cloud authority; Core verifies paths and applies confinement."""
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[2]
    user: Annotated[str, Field(min_length=1, max_length=255)]
    lifetime: LifetimeId
    policy: Literal["read_only", "workspace", "full_access"]
    folders: rules.Folders | None = None
    kind: Literal["shell", "exec", "sftp"]
    command: Annotated[str, Field(min_length=1, max_length=65536)] | None = None
    terminal: Terminal | None = None
    environment: Annotated[dict[Annotated[str, Field(pattern=r"^(LANG|LC_[A-Z]{1,32})$")],
                                Annotated[str, Field(pattern=r"^[A-Za-z0-9._@-]{0,128}$")]],
                           Field(max_length=32)] = {}
    github: GitHub | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if (self.policy == "workspace") != (self.folders is not None):
            raise ValueError("workspace requires exactly its folders")
        if (self.kind == "exec") != (self.command is not None) or "\0" in (self.command or ""):
            raise ValueError("exec requires exactly one command")
        return self


def parse(raw: bytes) -> SessionRequest:
    if len(raw) > MAX_REQUEST:
        raise Fault("request_invalid", "The access request exceeds its bound.", effect="known_no_effect")
    try:
        return SessionRequest.model_validate_json(raw)
    except ValidationError as error:
        raise Fault("request_invalid", "Use access protocol 2 with a valid session request.",
                    effect="known_no_effect") from error


class Lifetime:
    """Protected supervision metadata under Core state, which every sandbox denies.

    Admission holds the lifetime lock while it checks the record and selects or starts an instance, then
    hands its launch to that instance's spawner.
    """

    def __init__(self, state: Path, lifetime: str):
        self.directory = private_directory(private_directory(state / "code") / lifetime)
        self.path = self.directory / "record.json"

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None

    def save(self, record: dict) -> None:
        save_json(self.path, record)

    def lock(self):
        return lease(self.directory / "lock", wait=True)


def _account(user: str) -> pwd.struct_passwd:
    account = pwd.getpwuid(os.getuid())
    if user != account.pw_name or os.getuid() == 0:
        raise Fault("user_rejected", "Sessions run only as the non-root OS user running Open Machine.",
                    effect="known_no_effect")
    return account


def _argv(request: SessionRequest, account: pwd.struct_passwd) -> tuple[str, list[str]]:
    shell = account.pw_shell or "/bin/sh"
    if request.kind == "shell":
        return shell, ["-" + Path(shell).name]
    if request.kind == "exec":
        return shell, [shell, "-c", request.command]
    server = next((path for path in SFTP_SERVERS if os.access(path, os.X_OK)), None)
    if server is None:
        raise Fault("sftp_unavailable", "No sftp-server is installed on this machine.", effect="known_no_effect")
    return server, [server]


def _system_path() -> str:
    """The PATH macOS login shells get from path_helper: /etc/paths, then /etc/paths.d in name order."""
    sources = [Path("/etc/paths")]
    try:
        sources += sorted(Path("/etc/paths.d").iterdir())
    except OSError:
        pass
    entries = []
    for source in sources:
        try:
            entries += [line.strip() for line in source.read_text().splitlines() if line.strip()]
        except OSError:
            continue
    return ":".join(dict.fromkeys(entries)) or DEFAULT_PATH


def _environment(request: SessionRequest, account: pwd.struct_passwd, client: Path | None = None) -> dict:
    path = _system_path() if sys.platform == "darwin" else DEFAULT_PATH
    if client is not None:
        path += ":" + str(client.parent)  # so the session finds om-code by name
    environment = {"PATH": path, "USER": account.pw_name,
                   "LOGNAME": account.pw_name, "SHELL": account.pw_shell or "/bin/sh", "HOME": account.pw_dir}
    if request.terminal is not None:
        environment["TERM"] = request.terminal.term
    return environment | request.environment


def _client(request: SessionRequest) -> Path | None:
    """The host's om-code client: an executable regular file at its own physical path. The file and its
    directory belong to root or this user, with no group or other write and no ACL, and lie outside the
    persistent home and every restricted runtime tree, where restricted work could replace what Full
    sessions run."""
    if request.github is None:
        return None
    client = Path(request.github.client)
    restricted = [SHARED / HOME_NAME.format(uid=os.getuid())] + ([seatbelt.user_temp() / "omc"] if sys.platform == "darwin" else [])
    try:
        info, parent = client.lstat(), client.parent.lstat()
        valid = (stat.S_ISREG(info.st_mode) and os.path.realpath(client) == str(client) and os.access(client, os.X_OK)
                 and all(item.st_uid in (0, os.getuid()) and not item.st_mode & 0o022 for item in (info, parent))
                 and not seatbelt.has_acl(client) and not seatbelt.has_acl(client.parent) and not _within(client, restricted))
    except OSError as error:
        raise Fault("github_client_invalid", "The om-code client is unavailable.", effect="known_no_effect") from error
    if not valid:
        raise Fault("github_client_invalid", "The om-code client must be an executable file only its owner can change, "
                    "outside the persistent home and Core's runtime trees.", effect="known_no_effect")
    return client


def _within(client: Path, roots: list[Path]) -> bool:
    """Whether the client lies in one of roots, by physical identity, so no case or link spelling escapes.
    A root that is a link holds nothing: another user may plant one in /Users/Shared, and Core writes
    through none. A client that vanished meanwhile is refused with no effect."""
    identities = set()
    for root in roots:
        try:
            info = root.lstat()
        except OSError:
            continue  # An absent root holds nothing.
        if stat.S_ISDIR(info.st_mode):
            identities.add((info.st_dev, info.st_ino))
    try:
        ancestry = [os.stat(item) for item in [client, *client.parents]]
    except OSError as error:
        raise Fault("github_client_invalid", "The om-code client is unavailable.", effect="known_no_effect") from error
    return any((item.st_dev, item.st_ino) in identities for item in ancestry)


def _admit_full(request: SessionRequest, account, state: Path) -> tuple:
    """Make this process a session leader; the caller execs the command in place."""
    program, argv = _argv(request, account)
    environment = _environment(request, account, _client(request)) | (request.github.environment if request.github else {})
    lifetime = Lifetime(state, request.lifetime)
    with lifetime.lock():
        record = lifetime.read()
        if record is not None and record["policy"] != "full_access":
            raise Fault("lifetime_changed", "This lifetime's policy changed; start sessions in a new lifetime.",
                        effect="known_no_effect")
        if os.getsid(0) != os.getpid():
            os.setsid()
        if record is None:
            lifetime.save({"format": 1, "policy": "full_access"})
    if request.terminal is not None:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.chdir(account.pw_dir)
    return os.getpid(), program, argv, environment
