"""The system part of a Linux session's environment, as `ssh host command` gives it: the system variable
files, the default PATH, and the user's runtime directory and bus. Not a PAM login: no limits or logind."""

import os
from pathlib import Path
import re
import stat

SYSTEM = Path("/etc/environment")
LOCALES = (Path("/etc/default/locale"), Path("/etc/locale.conf"))  # The first that exists.
RUNTIME = Path("/run/user")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LIMIT = 64 * 1024
LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")


def _variables(path: Path) -> dict:
    """Plain NAME=value lines, an export prefix and matching quotes stripped; a missing or oversized file adds nothing."""
    try:
        with path.open("rb") as source:
            raw = source.read(LIMIT + 1)
    except OSError:
        return {}
    if len(raw) > LIMIT:
        return {}
    variables = {}
    for line in raw.decode(errors="surrogateescape").splitlines():
        match = LINE.fullmatch(line.strip())
        if match is None or "\0" in line:
            continue
        name, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        variables[name] = value
    return variables


def system(uid: int) -> dict:
    variables = _variables(SYSTEM) | _variables(next((path for path in LOCALES if path.exists()), LOCALES[0]))
    variables.setdefault("PATH", DEFAULT_PATH)
    runtime = RUNTIME / str(uid)
    try:
        info = runtime.lstat()
    except OSError:
        return variables
    if stat.S_ISDIR(info.st_mode) and info.st_uid == uid:
        variables["XDG_RUNTIME_DIR"] = str(runtime)
        try:
            if stat.S_ISSOCK((runtime / "bus").lstat().st_mode):
                variables["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
        except OSError:
            pass
    return variables
