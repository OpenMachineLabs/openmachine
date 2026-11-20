"""Entry points of the device identity component, which the Mac app and om on Linux run."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

from . import remote_access, service
from .errors import ConnectorError
from .cloud import Cloud, identifier, revoke, unpair
from .store import ORIGIN, Store, copied, default_state, disable, forget, hardware_id, key_fingerprint, origin


def diagnostic(error: BaseException) -> str:
    return error.code if isinstance(error, ConnectorError) else "connector_fault"


def configure(store: Store, server: str) -> dict:
    """Creates config.json for a maintainer-selected Cloud origin, such as Staging; an existing one is kept."""
    config = store.read("config.json", missing=True)
    if config is None:
        config = {"version": 1, "server": origin(server), "enabled": False}
        store.write("config.json", config, create=True)
    return {"status": "configured", "server": config["server"]}


async def in_flight(store: Store, cloud: Cloud, session: dict) -> dict | None:
    """The device a sign-in in flight registered, read back without switching to it; a pending sign-in is
    cancelled, unless an approval wins."""
    from .companion import Companion
    signer = Companion(store).signer(session)
    path = f"/api/native-sessions/{session['session_id']}/"
    try:
        result = await cloud.native(path + "status", session, {}, signer)
        if result.get("status") == "pending":
            hardware_id(store)  # a removal that cannot revoke cancels no sign-in either
            try:
                result = await cloud.native(path + "cancel", session, {}, signer)
            except ConnectorError as error:
                if error.code != "native_login_active":
                    raise
                result = await cloud.native(path + "status", session, {}, signer)
    except ConnectorError as error:
        if error.code != "native_session_unavailable":
            raise
        return None  # Cloud no longer holds it
    if result.get("status") != "active":
        return None
    fingerprint = result.get("key_fingerprint")
    if (result.get("session_id") != session["session_id"] or not isinstance(result.get("device"), dict)
            or fingerprint not in {key_fingerprint(session["public_key"]), *session.get("listed", [])}):
        raise ConnectorError("account_binding_changed", "The original native sign-in identity changed.")
    return {"device_id": identifier(result["device"].get("device_id")), "key_fingerprint": fingerprint}


async def remove(store: Store, *, local_only: bool = False) -> dict:
    """Takes this computer off Code, revokes every stored device, each with its own key, ends a sign-in in flight,
    then removes the local installation. Local-only removal leaves Cloud revocation explicitly unverified."""
    result = {"status": "incomplete", "operation": "remove", "cloud_revocation": "pending",
              "local_removal": "not_attempted", "errors": {}}
    config = disable(store)
    try:
        session = store.read("native-session.json", missing=True) if config else None
        if remote_access.switching(session):
            # A switch held or interrupted earlier completes first, so the device in use is the current one.
            from .companion import Companion
            Companion(store).settle()
            config = disable(store)
        device = config.get("device")
        others = [stored for stored in config.get("devices", []) if stored != device]
        live = not local_only and session and not session.get("ended")
        flight = None
        if live:
            async with Cloud(config["server"]) as cloud:
                flight = await in_flight(store, cloud, session)
        if flight and flight["device_id"] not in {stored["device_id"] for stored in config.get("devices", [])}:
            others.append(flight)
        if not device and not others:
            result["cloud_revocation"] = "not_paired"
        elif local_only:
            result["cloud_revocation"] = "unverified"
        else:
            async with Cloud(config["server"]) as cloud:
                if device and not config.get("revoked"):
                    await unpair(store, cloud)
                for stored in others:
                    await revoke(store, cloud, stored)
            result["cloud_revocation"] = "verified"
    except (ConnectorError, OSError) as error:
        result["errors"]["cloud_revocation"] = diagnostic(error)
    if result["cloud_revocation"] in {"verified", "not_paired"} or local_only:
        result["local_removal"] = "unverified"
        try:
            service.remove(store)
        except (ConnectorError, OSError) as error:
            result["errors"]["local_removal"] = diagnostic(error)
        else:
            result.update(status="removed", local_removal="verified")
    return result


async def command(args) -> dict:
    store = Store(args.state_dir)
    if args.command == "inspect":
        return service.inspect(store)
    if args.command == "companion" and args.action == "connection":
        from .companion import Companion
        if copied(store):
            raise ConnectorError("hardware_changed", "This registration belongs to another computer; sign in on this one.")
        return Companion(store).connection()
    with store.lock("operation.lock"):
        # Only these commands, under the operation lock, forget devices copied from another computer.
        if args.command in {"companion", "remove"} and copied(store):
            forget(store)
        if args.command in {"setup", "companion"}:
            raw = sys.stdin.buffer.read(40961)
            if len(raw) > 40960:
                raise ConnectorError("invalid_arguments", "The request exceeded its bound.")
            value = json.loads(raw or b"{}")
            if not isinstance(value, dict):
                raise ConnectorError("invalid_arguments", "A request must be an object.")
            if args.command == "setup":
                from .setup import act
                return await act(store, args.action, value, service.bundle_source())
            from .companion import Companion
            companion = Companion(store)
            async with companion.cloud:
                return await companion.act(args.action, value)
        if args.command == "upgrade":
            if args.automatic:
                if args.operation_id or not args.bundle or not args.expected_current or not args.expected_target:
                    raise ConnectorError("invalid_arguments", "App maintenance requires the exact component pair.")
                return service.maintain_bundle(store, args.bundle, args.expected_current, args.expected_target)
            if not args.operation_id:
                raise ConnectorError("invalid_arguments", "Use the original upgrade operation ID.")
            if args.status:
                return service.upgrade_status(store, args.operation_id)
            if not args.expected_current or not args.expected_target or (not args.resume and not args.bundle):
                raise ConnectorError("invalid_arguments", "Select exact old/new digests and a bundle for a new upgrade.")
            return service.upgrade(store, args.bundle, args.expected_current, args.expected_target,
                                   args.operation_id, resume=args.resume)
        if args.command == "configure":
            return configure(store, args.server)
        if args.command == "remove":
            return await remove(store, local_only=args.local_only)
    raise ConnectorError("unknown_command", "Select a supported connector command.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Open Machine device identity: sign-in, the device key and the bundle.")
    result.add_argument("--state-dir", type=Path, default=default_state())
    commands = result.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="Select the Cloud origin before setup")
    configure.add_argument("--server", default=ORIGIN, help="Maintainer origin override; the application uses openmachine.app.")
    commands.add_parser("inspect", help="Read verified retained installation identity without effects")
    upgrade = commands.add_parser("upgrade", help="Switch a retained immutable bundle with original recovery")
    upgrade.add_argument("--operation-id")
    mode = upgrade.add_mutually_exclusive_group()
    mode.add_argument("--automatic", action="store_true", help="Maintain the enclosing App component using its existing recovery journal")
    mode.add_argument("--status", action="store_true", help="Read the recorded outcome without continuing")
    mode.add_argument("--continue", action="store_true", dest="resume", help="Explicitly continue this same upgrade")
    upgrade.add_argument("--bundle", type=Path)
    upgrade.add_argument("--expected-current")
    upgrade.add_argument("--expected-target")
    removal = commands.add_parser("remove")
    removal.add_argument("--local-only", action="store_true", help="Remove locally while leaving Cloud revocation explicitly unverified.")
    setup = commands.add_parser("setup", help="First install and its recovery by operation ID.")
    setup.add_argument("action", choices=("preview", "start", "status", "continue"))
    companion = commands.add_parser("companion", help="Typed native account actions, read back by operation ID; no general command dispatch.")
    from .companion import ACTIONS
    companion.add_argument("action", choices=sorted(ACTIONS))
    return result


def main() -> None:
    args = parser().parse_args()
    for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
    try:
        result = asyncio.run(command(args))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if result.get("status") == "incomplete":
            raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        code = diagnostic(error)
        message = str(error) if isinstance(error, ConnectorError) else "The connector could not complete this operation; no unverified success was assumed."
        print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
        raise SystemExit(1) from None
