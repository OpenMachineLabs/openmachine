"""Desktop first install and its recovery: the state directory, config.json and the retained bundle.

Called under the existing operation lock. No key, account or Cloud effect occurs here; sign-in creates keys.
"""
from pathlib import Path
import re

from . import service
from .errors import ConnectorError
from .store import ORIGIN, Store


def preview(store: Store, bundle: Path) -> dict:
    config = store.read("config.json", missing=True)
    installed = service.inspect(store)
    _, manifest = service.verify_bundle(bundle)
    if installed["status"] != "not_installed" and installed["bundle_sha256"] != manifest:
        raise ConnectorError("connector_update_not_supported", "Reuse the retained version; setup cannot upgrade it.")
    return {"protocol": 1, "server": config["server"] if config else ORIGIN, "existing": config is not None,
            "bundle_sha256": manifest, "installation": installed["status"]}


def status(store: Store, operation_id: str) -> dict:
    name = f"setup-{operation_id}.json"
    record = store.read(name, missing=True)
    if record is None:
        # Fence delayed admission under the same lock as start.
        record = {"phase": "not_admitted"}
        store.write(name, record, create=True)
    result = {"operation_id": operation_id, "status": "unknown", "can_continue": False}
    if record["phase"] == "not_admitted":
        return {**result, "status": "rejected"}
    if record["phase"] == "cancelled":
        return {**result, "status": "cancelled"}
    if record["phase"] == "verified":
        return {**result, "status": "verified"}
    installed = service.inspect(store)
    if installed["status"] != "not_installed" and installed["bundle_sha256"] != record["bundle_sha256"]:
        raise ConnectorError("bundle_changed", "The original setup bundle changed.")
    if installed["status"] == "installed" and store.read("config.json", missing=True) is not None:
        record["phase"] = "verified"
        store.write(name, record)
        return {**result, "status": "verified"}
    # Creating config.json and retaining a verified bundle are idempotent, so an unfinished setup may continue.
    return {**result, "can_continue": True}


async def act(store: Store, action: str, value: dict, bundle: Path) -> dict:
    if action == "preview":
        return preview(store, bundle)
    identity = value.get("operation_id")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise ConnectorError("invalid_arguments", "Use the original setup ID.")
    if action == "status":
        return status(store, identity)
    name = f"setup-{identity}.json"
    record = store.read(name, missing=True)
    if action == "start":
        if record is not None:
            return status(store, identity)
        current = preview(store, bundle)
        active = store.read("setup-active.json", missing=True)
        if active and status(store, active["operation_id"])["status"] not in {"verified", "cancelled", "rejected"}:
            raise ConnectorError("setup_pending", "Read the original setup before starting another.")
        record = {"phase": "preparing", "bundle_sha256": current["bundle_sha256"]}
        store.write(name, record, create=True)
        store.write("setup-active.json", {"operation_id": identity})
    elif action == "continue":
        observed = status(store, identity)
        if not observed["can_continue"]:
            return observed
        record = store.read(name)
        if preview(store, bundle)["bundle_sha256"] != record["bundle_sha256"]:
            raise ConnectorError("setup_changed", "Continue with the original bundle.")
    else:
        raise ConnectorError("invalid_action", "Select a supported setup action.")
    if store.read("config.json", missing=True) is None:
        store.write("config.json", {"version": 1, "server": ORIGIN, "enabled": False}, create=True)
    record["phase"] = "installing"
    store.write(name, record)
    service.install(store, bundle)
    return status(store, identity)
