"""Read Desktop's single durable remote-access intent and report what om follows.

The Connector owns only the binding to this private file. It never writes a
second user switch, changes its revision, repairs permissions or replaces it.
Standalone Connectors remain unbound; a bound but unreadable intent fails closed.
"""

import json
import os
from pathlib import Path
import stat

from .errors import ConnectorError
from .store import revoking

MAX_INTENT_BYTES = 4096


def read_intent(value: str, revision: int | None = None) -> dict:
    if (not isinstance(value, str) or not value or "\x00" in value
            or not Path(value).is_absolute() or str(Path(value)) != value
            or ".." in Path(value).parts):
        raise ConnectorError("remote_access_invalid", "The intent must use its exact absolute private path.")
    if revision is not None and (type(revision) is not int or not 0 <= revision < 2**64):
        raise ConnectorError("remote_access_invalid", "Read the original intent revision.")
    descriptors = []
    try:
        # Walk with directory descriptors so no ancestor symlink is followed,
        # including when a directory is swapped while the file is being opened.
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(parent)
        parts = Path(value).parts[1:]
        for part in parts[:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ConnectorError("remote_access_invalid", "The intent needs an owner-only parent directory.")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > MAX_INTENT_BYTES):
            raise ConnectorError("remote_access_invalid", "The intent must be a bounded private regular file.")
        raw = os.read(fd, MAX_INTENT_BYTES + 1)
        if len(raw) > MAX_INTENT_BYTES:
            raise ConnectorError("remote_access_invalid", "The intent exceeded its bound.")
        intent = json.loads(raw)
        if (not isinstance(intent, dict) or set(intent) != {"format", "enabled", "revision"}
                or type(intent["format"]) is not int or intent["format"] != 1
                or type(intent["enabled"]) is not bool or type(intent["revision"]) is not int
                or not 0 <= intent["revision"] < 2**64):
            raise ConnectorError("remote_access_invalid", "The intent has an unsupported format.")
        if revision is not None and intent["revision"] != revision:
            raise ConnectorError("remote_access_changed", "A newer choice superseded this operation.")
        return intent
    except (OSError, ValueError, UnicodeError, IndexError):
        raise ConnectorError("remote_access_invalid", "The intent could not be read safely; remote admission is blocked.") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def policy(config: dict) -> dict:
    binding = config.get("remote_access")
    if binding is None:
        return {"managed": False, "enabled": True, "revision": None}
    try:
        if not isinstance(binding, dict) or set(binding) != {"intent_path"}:
            raise ConnectorError("remote_access_invalid", "The intent binding is invalid.")
        intent = read_intent(binding["intent_path"])
        return {"managed": True, "enabled": intent["enabled"], "revision": intent["revision"]}
    except ConnectorError as error:
        return {"managed": True, "enabled": False, "revision": None, "code": error.code}


def switching(session: dict | None) -> bool:
    """An approved sign-in has not yet moved this computer to its device. A cancel or sign-out,
    seen by Cloud or not, abandons the move."""
    registration = (session or {}).get("registration")
    return bool(registration and registration["switch"] and not session.get("registration_applied")
                and not session.get("ended") and not session.get("signed_out_locally"))


def signed_in(session: dict | None) -> bool:
    """A sign-in this computer adopted and has neither ended nor signed out."""
    return bool(session and session.get("registration_applied")
                and not session.get("ended") and not session.get("signed_out_locally"))


def admits(store, config: dict) -> bool:
    """Whether om may go online: the intent is on, the device in use is registered and unrevoked, and its
    sign-in is active."""
    session = store.read("native-session.json", missing=True)
    return bool(policy(config)["enabled"] and config.get("device") and not revoking(store, config)
                and not switching(session) and signed_in(session))


def status(store) -> dict:
    """Remote access state: the first of these checks that applies."""
    config = store.read("config.json")
    current = policy(config)
    session = store.read("native-session.json", missing=True)
    state, code = "ready", None
    if current.get("code"):
        state, code = "error", current["code"]
    elif not current["enabled"]:
        state = "off"
    elif switching(session):
        state, code = "starting", "connector_identity_changing"
    elif config.get("device") and revoking(store, config):
        state, code = "pairing_required", "device_not_authorized"
    elif not config.get("device") or not signed_in(session):
        state = "pairing_required"
    elif config.get("enabled") is not True:
        state = "starting"
    return {"managed": current["managed"], "enabled": current["enabled"], "revision": current["revision"],
            "state": state, "code": code, "admission_blocked": state != "ready",
            "connector_stopped": config.get("enabled") is not True}
