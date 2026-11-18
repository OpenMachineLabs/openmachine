"""Retain, verify and switch the Connector's own immutable bundle. It installs no service: Desktop, or om on
Linux, runs the Connector per call, and om follows config.json."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import uuid

from .errors import ConnectorError
from .store import Store, private_directory


def bundle_source() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    if not (candidate / "manifest.json").is_file():
        raise ConnectorError("bundle_required", "Use the Open Machine Connector application to install its verified runtime bundle.")
    return candidate


def verify_bundle(source: Path, *, retiring: bool = False) -> tuple[dict, str]:
    source = source.absolute()
    if source.is_symlink():
        raise ConnectorError("invalid_bundle", "The bundle root must not be a symlink.")
    manifest_path = source / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise ConnectorError("invalid_bundle", "The connector bundle has no bounded manifest.")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ConnectorError("invalid_bundle", "The connector bundle manifest is unreadable.") from None
    if (manifest.get("format") != 1 or manifest.get("product") != "openmachine-connector"
            or not isinstance(manifest.get("files"), dict) or not isinstance(manifest.get("links"), dict)):
        raise ConnectorError("invalid_bundle", "The connector bundle format is not supported.")
    expected = set(manifest["files"]) | set(manifest["links"])
    actual = {item.relative_to(source).as_posix() for item in source.rglob("*")
              if item.is_file() or item.is_symlink()} - {"manifest.json"}
    extra = actual - expected
    # Historical Python launches generated cache files inside owned bundles.
    # They may be retired, never trusted as executable input for installation.
    cache_only = retiring and all(
        "__pycache__" in Path(name).parts and name.endswith(".pyc")
        and not (source / name).is_symlink()
        and stat.S_ISREG((source / name).lstat().st_mode)
        and (source / name).stat().st_uid == os.getuid()
        and (source / name).resolve().is_relative_to(source.resolve()) for name in extra)
    if expected - actual or (extra and not cache_only):
        raise ConnectorError("bundle_changed", "The bundle's file inventory does not match its manifest.")
    for name in expected:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConnectorError("invalid_bundle", "The connector bundle contains an invalid path.")
        target = source / name
        if not target.resolve().is_relative_to(source.resolve()):
            raise ConnectorError("invalid_bundle", "A connector bundle link escapes its retained runtime.")
        if name in manifest["links"]:
            if not target.is_symlink() or os.readlink(target) != manifest["links"][name]:
                raise ConnectorError("bundle_changed", "A connector bundle link changed.")
        elif target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != manifest["files"][name]:
            raise ConnectorError("bundle_changed", "A connector bundle file changed.")
    if not (source / "python/bin/python3").is_file() or not (source / "entry.py").is_file():
        raise ConnectorError("invalid_bundle", "The bundle does not contain its standalone Python and entry point.")
    return manifest, hashlib.sha256(raw).hexdigest()


def _retain_bundle(source: Path, digest: str) -> Path:
    versions = _versions()
    private_directory(versions.parent)
    private_directory(versions)
    target = versions / digest
    if target.exists() or target.is_symlink():
        if verify_bundle(target)[1] != digest:
            raise ConnectorError("bundle_changed", "The retained runtime differs from the reviewed bundle.")
    else:
        staging = versions / (digest + ".preparing")
        if staging.is_symlink() or staging.exists() and not staging.is_dir():
            raise ConnectorError("installation_pending", "A previous bundle preparation was retained.")
        if staging.exists():
            # An interrupted retention: the operation lock is held, so no process is still writing it.
            shutil.rmtree(staging)
        shutil.copytree(source, staging, symlinks=True)
        os.chmod(staging, 0o700)
        if verify_bundle(staging)[1] != digest:
            raise ConnectorError("bundle_changed", "The source changed during retention.")
        staging.rename(target)
    return target


def _versions() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/OpenMachine/Connector/versions"
    return Path.home() / ".local/share/openmachine/connector/versions"


def _platform(manifest: dict) -> None:
    """A bundle runs only on the platform and architecture it was built for."""
    if manifest.get("platform") != sys.platform or manifest.get("architecture") != os.uname().machine:
        raise ConnectorError("incompatible_bundle", "This Connector bundle does not match this computer.")


def installation(store: Store) -> dict | None:
    """The selected bundle's identity and phase; keys of earlier builds, such as their LaunchAgent, are ignored."""
    selected = store.read("installation.json", missing=True)
    return None if selected is None else {key: selected.get(key) for key in ("bundle", "bundle_sha256", "version", "phase")}


def install(store: Store, source: Path) -> dict:
    """Retains and selects a verified bundle; repeating it with the same bundle changes nothing."""
    active = store.read("upgrade-active.json", missing=True)
    if active and store.read(f"upgrade-{active['operation_id']}.json")["phase"] != "verified":
        raise ConnectorError("upgrade_pending", "Recover the original upgrade before installing.")
    manifest, digest = verify_bundle(source)
    _platform(manifest)
    current = installation(store)
    if current is not None and current["bundle_sha256"] != digest:
        raise ConnectorError("connector_update_not_supported", "Upgrade this installation to change its bundle.")
    target = _retain_bundle(source, digest)
    record = {"bundle": str(target), "bundle_sha256": digest, "version": manifest["version"], "phase": "installed"}
    if current != record:
        store.write("installation.json", record)
    return {"status": "installed", "version": manifest["version"]}


def inspect(store: Store) -> dict:
    selected = installation(store)
    if selected is None:
        return {"status": "not_installed"}
    if verify_bundle(Path(selected["bundle"]))[1] != selected["bundle_sha256"]:
        raise ConnectorError("bundle_changed", "The retained Connector bundle changed.")
    config = store.read("config.json", missing=True) or {}
    return {"status": "installed" if selected["phase"] == "installed" else "prepared",
            "version": selected["version"], "bundle_sha256": selected["bundle_sha256"], "phase": selected["phase"],
            "enabled": config.get("enabled") is True, "paired": bool(config.get("device"))}


def upgrade_status(store: Store, operation_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ConnectorError("invalid_operation", "Use the original upgrade operation ID.")
    record = store.read(f"upgrade-{operation_id}.json", missing=True)
    if record is None:
        active = store.read("upgrade-active.json", missing=True)
        if not active or active.get("operation_id") != operation_id:
            return {"status": "not_admitted", "operation_id": operation_id}
        if "old" not in active:
            return {"status": "unknown", "operation_id": operation_id}
        record = active
    if record["phase"] == "verified":
        return record["result"]
    selected = installation(store)
    return {"status": "prepared" if selected == record["old"] else "unknown", "operation_id": operation_id,
            "phase": record["phase"],
            "installation": "old" if selected == record["old"] else "new" if selected == record["new"] else "unknown",
            "recovery": "explicit_same_operation_only"}


def maintain_bundle(store: Store, source: Path, expected_current: str, expected_target: str) -> dict:
    """Finish the enclosing App's component update through the original owner journal."""
    active = store.read("upgrade-active.json", missing=True)
    if active:
        original = store.read(f"upgrade-{active['operation_id']}.json", missing=True) or active
        if original.get("phase") != "verified":
            if (original.get("old", {}).get("bundle_sha256") != expected_current
                    or original.get("new", {}).get("bundle_sha256") != expected_target):
                raise ConnectorError("upgrade_pending", "A different component update is still being recovered.")
            return upgrade(store, source, expected_current, expected_target, original["operation_id"], resume=True)
    installed = inspect(store)
    if installed.get("status") == "installed" and installed.get("bundle_sha256") == expected_target:
        return {**installed, "status": "verified"}
    return upgrade(store, source, expected_current, expected_target, uuid.uuid4().hex)


def upgrade(store: Store, source: Path | None, expected_current: str, expected_target: str,
            operation_id: str, *, resume: bool = False) -> dict:
    """Retains, verifies and switches to a new bundle under one original recovery record. Once it is verified,
    only the selected bundle and the one before it stay."""
    if not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (expected_current, expected_target)):
        raise ConnectorError("invalid_arguments", "Both exact manifest SHA256 values are required.")
    observed = upgrade_status(store, operation_id)
    name = f"upgrade-{operation_id}.json"
    record = store.read(name, missing=True)
    if record is None:
        active = store.read("upgrade-active.json", missing=True)
        if active and active.get("operation_id") == operation_id and "old" in active:
            record = active
            if resume:
                store.write(name, record, create=True)
    if record:
        if (record["old"]["bundle_sha256"], record["new"]["bundle_sha256"]) != (expected_current, expected_target):
            raise ConnectorError("operation_changed", "The original upgrade selection changed.")
        if not resume or record["phase"] == "verified":
            return observed
    else:
        if resume or source is None:
            raise ConnectorError("missing_original", "Continue only a previously prepared upgrade.")
        active = store.read("upgrade-active.json", missing=True)
        if active and store.read(f"upgrade-{active['operation_id']}.json")["phase"] != "verified":
            raise ConnectorError("upgrade_pending", "Recover the existing upgrade before another.")
        old = installation(store)
        if (old is None or old["phase"] != "installed" or old["bundle_sha256"] != expected_current or
                verify_bundle(Path(old["bundle"]), retiring=True)[1] != expected_current):
            raise ConnectorError("installation_changed", "The confirmed installed version changed.")
        manifest, digest = verify_bundle(source)
        if digest != expected_target or digest == expected_current:
            raise ConnectorError("incompatible_bundle", "The target bundle identity changed.")
        _platform(manifest)
        target = _retain_bundle(source, digest)
        new = {"bundle": str(target), "bundle_sha256": digest, "version": manifest["version"], "phase": "installed"}
        record = {"operation_id": operation_id, "phase": "prepared", "old": old, "new": new}
        store.write("upgrade-active.json", record)
        store.write(name, record, create=True)

    old, new = record["old"], record["new"]
    if (Path(new["bundle"]).parent != Path(old["bundle"]).parent or Path(new["bundle"]).name != expected_target
            or verify_bundle(Path(new["bundle"]))[1] != expected_target):
        raise ConnectorError("bundle_changed", "The retained upgrade target changed.")
    selected = installation(store)
    if selected not in (old, new):
        raise ConnectorError("installation_changed", "The selected installation changed.")
    if selected == old:
        store.write("installation.json", new)
    record["result"] = {"status": "verified", "operation_id": operation_id, "version": new["version"],
                        "bundle_sha256": expected_target}
    record["phase"] = "verified"
    store.write(name, record)
    for retained in Path(new["bundle"]).parent.iterdir():
        if retained.name not in {old["bundle_sha256"], new["bundle_sha256"]} and retained.is_dir() and not retained.is_symlink():
            shutil.rmtree(retained)
    return upgrade_status(store, operation_id)


def remove(store: Store) -> dict:
    """Deletes this installation's bundle and the Connector's sign-in; other retained versions stay."""
    active = store.read("upgrade-active.json", missing=True)
    if active and store.read(f"upgrade-{active['operation_id']}.json")["phase"] != "verified":
        raise ConnectorError("upgrade_pending", "Recover the original upgrade before removal.")
    selected = installation(store)
    if selected:
        bundle = Path(selected["bundle"])
        if bundle.parent != _versions() or bundle.name != selected["bundle_sha256"]:
            raise ConnectorError("installation_changed", "The retained connector bundle location changed.")
        verify_bundle(bundle, retiring=True)
        shutil.rmtree(bundle)
    setup = store.read("setup-active.json", missing=True)
    if setup:
        name = f"setup-{setup['operation_id']}.json"
        record = store.read(name)
        if record["phase"] != "verified":
            store.write(name, {**record, "phase": "cancelled"})
        store.delete("setup-active.json")
    for name in ("native-session.json", *store.names("key-"), *store.names("revocation"), "config.json", "installation.json"):
        store.delete(name)
    return {"status": "removed"}
