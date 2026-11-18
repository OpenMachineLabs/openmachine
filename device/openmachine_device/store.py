"""Private, owner-checked Connector state."""

import base64
from contextlib import contextmanager
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from .errors import ConnectorError

ORIGIN = "https://api.openmachine.app"
MAX_STATE = 65536
MACHINE_ID = Path("/etc/machine-id")


def default_state() -> Path:
    return Path.home() / ".local/state/openmachine-connector"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or not parsed.hostname):
        raise ConnectorError("invalid_origin", "Select an HTTPS Cloud origin without a path or credentials.")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise ConnectorError("invalid_origin", "Cloud requires HTTPS; HTTP is supported only for loopback development.")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def private_directory(path: Path) -> None:
    # Existing paths are checked, never repaired by chmod or followed as links.
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConnectorError("unsafe_local_state", "Connector state must be an owner-only directory; existing permissions were not changed.")


class Store:
    def __init__(self, root: Path):
        self.root = root.expanduser().absolute()

    def _path(self, name: str) -> Path:
        if not name or "/" in name or name.startswith(".") or "\\" in name:
            raise ValueError("Invalid connector state name")
        return self.root / name

    def read_bytes(self, name: str, *, missing: bool = False) -> bytes | None:
        if not self.root.exists() and not self.root.is_symlink() and missing:
            return None
        private_directory(self.root)
        try:
            descriptor = os.open(self._path(name), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if missing:
                return None
            raise ConnectorError("missing_local_state", "No connector installation is selected.") from None
        except OSError:
            raise ConnectorError("unsafe_local_state", "Connector state could not be opened safely.") from None
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_STATE:
                raise ConnectorError("unsafe_local_state", "Connector state must be a bounded owner-only regular file.")
            return handle.read(MAX_STATE + 1)

    def read(self, name: str, *, missing: bool = False) -> dict | None:
        value = self.read_bytes(name, missing=missing)
        if value is None:
            return None
        try:
            record = json.loads(value)
        except (UnicodeError, ValueError):
            raise ConnectorError("invalid_local_state", "Connector state is unreadable; it was not replaced.") from None
        if not isinstance(record, dict):
            raise ConnectorError("invalid_local_state", "Connector state has an unsupported shape.")
        return record

    def write_bytes(self, name: str, value: bytes, *, create: bool = False) -> None:
        if len(value) > MAX_STATE:
            raise ConnectorError("local_state_limit", "Connector state exceeds its bounded storage limit.")
        private_directory(self.root)
        target = self._path(name)
        if create:
            try:
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                raise ConnectorError("local_state_exists", "The existing connector identity was preserved.") from None
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            self.read_bytes(name, missing=True)
            descriptor, temporary = tempfile.mkstemp(prefix="pending-", dir=self.root)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(value)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def names(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(name for name in os.listdir(self.root) if name.startswith(prefix))

    def write(self, name: str, value: dict, *, create: bool = False) -> None:
        self.write_bytes(name, (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(), create=create)

    def delete(self, name: str) -> None:
        if self.read_bytes(name, missing=True) is not None:
            self._path(name).unlink()

    @contextmanager
    def lock(self, name: str):
        private_directory(self.root)
        descriptor = os.open(self._path(name), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ConnectorError("unsafe_local_state", "The connector lock is not private.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConnectorError("connector_busy", "Another connector owner is active; no second owner was started.") from None
            yield
        finally:
            os.close(descriptor)


def key_file(fingerprint: str) -> str:
    """A device key's file, named by its fingerprint."""
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ConnectorError("invalid_identity", "The device key fingerprint is invalid.")
    return "key-" + fingerprint


class Identity:
    def __init__(self, key: Ed25519PrivateKey):
        self._key = key

    @classmethod
    def create(cls, store: Store):
        """A new key, written once under its fingerprint and never rewritten."""
        identity = cls(Ed25519PrivateKey.generate())
        store.write_bytes(key_file(identity.fingerprint), identity._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()), create=True)
        return identity

    @classmethod
    def load(cls, store: Store, fingerprint: str):
        value = store.read_bytes(key_file(fingerprint))
        identity = cls(Ed25519PrivateKey.from_private_bytes(value)) if len(value) == 32 else None
        if identity is None or identity.fingerprint != fingerprint:
            raise ConnectorError("invalid_identity", "The device key is invalid; it was not replaced.")
        return identity

    @classmethod
    def current(cls, store: Store):
        """The key of the device in use."""
        device = store.read("config.json").get("device")
        if not device:
            raise ConnectorError("sign_in_required", "Sign in to register this computer.")
        return cls.load(store, device.get("key_fingerprint"))

    @property
    def public_key(self) -> str:
        return b64url(self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    @property
    def fingerprint(self) -> str:
        public = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return hashlib.sha256(public).hexdigest()

    def proof(self, server: str, method: str, path: str, challenge_id: str, challenge: str, body: bytes = b"",
              time: int | None = None) -> dict:
        """Signs om-device-v1, or with time om-device-v2, the native session proof."""
        if any("\n" in part or "\r" in part for part in (server, method, path, challenge_id, challenge)):
            raise ConnectorError("invalid_challenge", "The Cloud challenge is invalid.")
        if not challenge_id or not challenge or len(challenge_id) > 128 or len(challenge) > 512:
            raise ConnectorError("invalid_challenge", "The Cloud challenge is invalid.")
        lines = (origin(server), method.upper(), path, challenge_id, challenge, hashlib.sha256(body).hexdigest())
        message = "\n".join(("om-device-v1", *lines) if time is None else ("om-device-v2", *lines, str(time))).encode()
        return {"X-OM-Challenge-Id": challenge_id, "X-OM-Challenge": challenge,
                "X-OM-Signature": b64url(self._key.sign(message)), **({} if time is None else {"X-OM-Time": str(time)})}


@functools.cache
def hardware_id(store: Store, *, create: bool = False) -> str:
    """This computer's hardware ID, read once per process; it never leaves this computer. On
    Linux it is the state file hardware-id if it exists, else /etc/machine-id; a sign-in creates that file, once,
    on a machine whose /etc/machine-id is unreadable or malformed. om reads the same."""
    try:
        if sys.platform == "darwin":
            output = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                    capture_output=True, text=True, timeout=10, check=True).stdout
            found = re.search(r'"IOPlatformUUID" = "([0-9A-F-]{36})"', output)
            value = found[1] if found else None
        else:
            stored = store.read_bytes("hardware-id", missing=True)
            if stored is None:
                try:
                    stored = MACHINE_ID.read_bytes()
                except OSError:
                    stored = b""
                if create and not re.fullmatch(rb"[0-9a-f]{32}", stored.strip()):
                    stored = secrets.token_hex(16).encode()
                    store.write_bytes("hardware-id", stored)
            value = stored.decode().strip()
            value = value if re.fullmatch(r"[0-9a-f]{32}", value) else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        value = None
    if value is None:
        raise ConnectorError("hardware_unavailable", "This computer's hardware ID could not be read; nothing was changed.")
    return value


def key_fingerprint(public_key: str) -> str:
    return hashlib.sha256(base64.urlsafe_b64decode(public_key + "=")).hexdigest()


def device_record(store: Store, receipt: dict, fingerprint: str) -> dict:
    """A stored device: its receipt bound to its key and to this computer."""
    return {**receipt, "key_fingerprint": fingerprint, "hardware_id": hardware_id(store)}


def disable(store: Store) -> dict:
    """Takes this computer off Code: om follows enabled within a second. Returns config.json, {} if absent."""
    config = store.read("config.json", missing=True) or {}
    if config and config.get("enabled") is not False:
        config["enabled"] = False
        store.write("config.json", config)
    return config


def revocation_record(device_id: str) -> str:
    """The retained revocation proof and receipt of one stored device."""
    return f"revocation-{device_id}.json"


def revoking(store: Store, config: dict) -> bool:
    """The device in use is revoked, or its revocation began."""
    device = config.get("device")
    return bool(config.get("revoked") or device and store.read(revocation_record(device["device_id"]), missing=True))


def copied(store: Store) -> bool:
    """Whether the stored devices came from another computer: one records another hardware ID, or its key is
    missing. State of an earlier build, whose key is identity.key, counts as such. An unreadable
    ID is never a mismatch; sign-in, going online and revocation refuse it."""
    if store.read_bytes("identity.key", missing=True) is not None:
        return True
    config = store.read("config.json", missing=True) or {}
    devices = [*config.get("devices", []), *([config["device"]] if config.get("device") else [])]
    for device in devices:
        fingerprint = device.get("key_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint) \
                or store.read_bytes(key_file(fingerprint), missing=True) is None:
            return True
    try:
        return any(device.get("hardware_id") != hardware_id(store) for device in devices)
    except ConnectorError:
        return False


def forget(store: Store) -> None:
    """Forgets every stored device of a copied registration; they stay the other computer's, so nothing is
    revoked, and their unresolved operations no longer hold this one. config.json is written last, so an
    interrupted forget repeats."""
    for name in ("native-session.json", "companion-pending.json", "identity.key", *store.names("revocation"),
                 *store.names("key-")):
        store.delete(name)
    config = store.read("config.json")
    for key in ("device", "devices", "revoked"):
        config.pop(key, None)
    config["enabled"] = False
    store.write("config.json", config)
