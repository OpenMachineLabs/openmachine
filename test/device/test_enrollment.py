"""Desktop sign-in keeps one device per account on this computer."""

import copy
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "device"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openmachine_device.cli import command, parser
from openmachine_device.cloud import Cloud
from openmachine_device.companion import Companion, computer_name
from openmachine_device.errors import ConnectorError
from openmachine_device.store import Identity, ORIGIN, Store, hardware_id, key_file, key_fingerprint


class Crash(Exception):
    """A process ending at this point."""


class FakeCloud:
    """The native session routes as Cloud serves them, checking each request's keys."""

    def __init__(self, test):
        self.test = test
        self.devices = {}
        self.sessions = {}
        self.calls = []
        self.lose_creation = False
        self.refuse_key = None  # the code Cloud refuses a create's key with, creating nothing
        self.move_to = None  # the account a create with a move's secret is approved as; its device in use is removed
        self.drop_creation = False  # a create never reaches Cloud
        self.offline_sign_out = False
        self.offline_cancel = False
        self.before_cancel = None  # what happens in the browser just before a cancel arrives

    def current(self, keys) -> list[str]:
        return [identity for identity, device in self.devices.items()
                if device["public_key"] in keys and device["revoked_at"] is None]

    def receipt(self, identity: str) -> dict:
        device = self.devices[identity]
        return {"device_id": identity, "principal_id": device["principal_id"], "grant_id": "grant-" + identity,
                "key_fingerprint": key_fingerprint(device["public_key"]), "revoked_at": device["revoked_at"],
                "grant_revision": 1}

    def snapshot(self, identity: str) -> dict:
        session = self.sessions[identity]
        device = self.devices.get(session["device_id"])
        active = session["status"] == "active"
        value = {"session_id": identity, "status": session["status"],
                 "key_fingerprint": key_fingerprint(device["public_key"] if device else session["public_key"]),
                 "account": {"principal_id": session["principal_id"], "login": session["principal_id"]} if active else None,
                 "device": self.receipt(session["device_id"]) if active else None}
        if session["status"] == "pending":
            value["stale_keys"] = [key_fingerprint(key) for key in session["device_keys"] if not self.current([key])]
        return value

    def approve(self, identity: str, principal: str) -> str:
        """The browser approves as principal: its listed device, else a new device with the candidate."""
        session = self.sessions[identity]
        listed = self.current(session["device_keys"])
        own = [device for device in listed if self.devices[device]["principal_id"] == principal]
        if own:
            device = own[0]
            for other in self.sessions.values():
                if other["device_id"] == device and other["status"] == "active":
                    other["status"] = "signed_out"
        else:
            self.test.assertLess(len(listed), 8)
            device = f"device-{len(self.devices) + 1}"
            self.devices[device] = {"principal_id": principal, "public_key": session["public_key"], "revoked_at": None}
        session.update(status="active", device_id=device, principal_id=principal, device_keys=None)
        return device

    async def native(self, path, session, value, signer, stored=()):
        body = value(int(time.time())) if callable(value) else value
        self.calls.append((path, copy.deepcopy(body)))
        identity = session["session_id"]
        if path == "/api/native-sessions":
            self.test.assertEqual(body["public_key"], signer.public_key)
            self.test.assertEqual(body["device_keys"], [key.public_key for key in stored])
            self.test.assertNotIn(signer.public_key, body["device_keys"])
            self.test.assertTrue(body["device_name"])
            if self.refuse_key:
                raise ConnectorError(self.refuse_key, "Cloud refused the key")
            if self.drop_creation:
                raise ConnectorError("cloud_unavailable", "Offline")
            self.sessions[identity] = {"status": "pending", "public_key": body["public_key"],
                                       "device_keys": body["device_keys"], "device_id": None, "principal_id": None}
            if body.get("key", "").startswith("ommove_"):
                # The approval that registers the machine in the move's account removes the device it came from.
                left = [device for device in self.current(body["device_keys"]) if any(
                    other["device_id"] == device and other["status"] == "active" for other in self.sessions.values())]
                self.approve(identity, self.move_to)
                for device in left:
                    self.devices[device]["revoked_at"] = 1
                    for other in self.sessions.values():
                        if other["device_id"] == device:
                            other["status"] = "signed_out"
            if self.lose_creation:
                raise ConnectorError("cloud_unavailable", "Original response lost")
            return self.snapshot(identity)
        if identity not in self.sessions:
            raise ConnectorError("native_session_unavailable", "Cloud holds no such sign-in")
        selected = self.sessions[identity]
        device_key = self.devices[selected["device_id"]]["public_key"] if selected["device_id"] else None
        action = path.rsplit("/", 1)[1]
        if action == "device":
            self.test.assertEqual(signer.public_key, device_key)
            return {**self.receipt(selected["device_id"]), "name": "My Mac", "observed_at_ms": 1,
                    "policy": {"revision": 1, "mode": "read_only", "folders": []}}
        self.test.assertIn(signer.public_key, {selected["public_key"], device_key})
        if action == "sign-out":
            if self.offline_sign_out:
                raise ConnectorError("cloud_unavailable", "Offline")
            if selected["status"] in {"pending", "active"}:
                selected.update(status="signed_out", device_keys=None)
        elif action == "cancel":
            if self.offline_cancel:
                raise ConnectorError("cloud_unavailable", "Offline")
            if self.before_cancel:
                self.before_cancel()
            if selected["status"] == "active":
                raise ConnectorError("native_login_active", "Sign-in already completed")
            if selected["status"] == "pending":
                selected.update(status="cancelled", device_keys=None)
        return self.snapshot(identity)

    def creates(self) -> int:
        return sum(path == "/api/native-sessions" for path, _ in self.calls)


class NativeEnrollment(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-enrollment-")
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "connector")
        self.store.write("config.json", {"version": 1, "server": ORIGIN, "enabled": False})
        self.hardware = self.patch("openmachine_device.store.hardware_id", return_value="hardware-A")
        self.patch("openmachine_device.companion.hardware_id", new=self.hardware)
        self.patch("openmachine_device.cloud.hardware_id", new=self.hardware)
        self.patch("openmachine_device.cli.hardware_id", new=self.hardware)
        self.cloud = FakeCloud(self)
        self.companion = self.open()

    def patch(self, target, **options):
        patcher = patch(target, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def open(self) -> Companion:
        """The Connector as a new process opens it."""
        companion = Companion(self.store)
        companion.cloud.native = self.cloud.native
        return companion

    def request(self, identity="a" * 32, **fields):
        return {"operation_id": identity, "expected_binding": self.companion.binding(), **fields}

    async def test_names_a_new_device_by_this_computers_name(self):
        with patch("openmachine_device.companion.computer_name", return_value=" Ada’s MacBook Pro "):
            await self.companion.act("login", self.request())
        self.assertEqual(self.cloud.calls[0][1]["device_name"], "Ada’s MacBook Pro")

    async def start_login(self, identity="a" * 32):
        return await self.companion.act("login", self.request(identity, device_name="My Mac"))

    def approve(self, principal="owner-A") -> str:
        return self.cloud.approve(self.store.read("native-session.json")["session_id"], principal)

    async def sign_in(self, principal="owner-A", identity="a" * 32) -> str:
        """A complete sign-in: Desktop starts it, the browser approves, the Connector reads it back."""
        await self.start_login(identity)
        device = self.approve(principal)
        self.assertEqual((await self.companion.native("status"))["status"], "active")
        return device

    async def sign_out(self, identity="9" * 32):
        self.assertEqual((await self.companion.act("sign-out", self.request(identity)))["status"], "verified")

    def keys(self) -> set[str]:
        return set(self.store.names("key-"))

    def stored(self, device: str) -> dict:
        receipt = self.cloud.receipt(device)
        return {key: receipt[key] for key in ("device_id", "grant_id", "principal_id", "key_fingerprint")} | {
            "hardware_id": "hardware-A"}

    async def test_signing_in_again_uses_the_device_in_use_and_deletes_the_candidate(self):
        """Signing in to the same account again keeps its device and deletes the new candidate key."""
        await self.start_login()
        session = self.store.read("native-session.json")
        candidate = key_file(key_fingerprint(session["public_key"]))
        self.assertEqual(self.keys(), {candidate})
        self.assertEqual((self.store.root / candidate).stat().st_mode & 0o777, 0o600)
        self.assertEqual(session["listed"], [])
        device = self.approve()
        snapshot = await self.companion.snapshot()
        self.assertEqual(snapshot["device"]["status"], "verified")
        self.assertEqual(snapshot["binding"], self.companion.binding())
        config = self.store.read("config.json")
        self.assertEqual(config["device"], self.stored(device))
        self.assertEqual(config["devices"], [config["device"]])
        self.assertTrue(config["enabled"])
        await self.sign_out()
        self.assertFalse(self.store.read("config.json")["enabled"])
        await self.start_login("b" * 32)
        self.assertEqual(len(self.keys()), 2)
        self.assertEqual(self.store.read("native-session.json")["listed"], [config["device"]["key_fingerprint"]])
        self.assertEqual(self.approve(), device)
        await self.companion.native("status")
        self.assertEqual(self.keys(), {candidate})
        self.assertEqual(self.store.read("config.json")["device"], config["device"])
        self.assertTrue(self.store.read("config.json")["enabled"])
        self.assertEqual(len(self.cloud.devices), 1)

    async def test_another_account_gets_its_own_device_and_the_first_stays_stored(self):
        """A switch to a new device, then back to the stored one; nothing is revoked."""
        first = await self.sign_in("owner-A")
        await self.sign_out()
        second = await self.sign_in("owner-B", "b" * 32)
        self.assertNotEqual(second, first)
        config = self.store.read("config.json")
        self.assertEqual(config["device"], self.stored(second))
        self.assertEqual(config["devices"], [self.stored(first), self.stored(second)])
        self.assertIs(config["revoked"], False)
        self.assertTrue(config["enabled"])
        self.assertEqual(self.keys(), {key_file(device["key_fingerprint"]) for device in config["devices"]})
        self.assertIsNone(self.cloud.devices[first]["revoked_at"])
        await self.sign_out("8" * 32)
        self.assertEqual(await self.sign_in("owner-A", "c" * 32), first)
        config = self.store.read("config.json")
        self.assertEqual(config["device"], self.stored(first))
        self.assertEqual(config["devices"], [self.stored(first), self.stored(second)])
        self.assertTrue(config["enabled"])
        self.assertEqual(len(self.keys()), 2)
        self.assertEqual(len(self.cloud.devices), 2)
        snapshot = await self.companion.snapshot()
        self.assertEqual((snapshot["account"]["status"], snapshot["device"]["device_id"]), ("active", first))

    def managed(self) -> Store:
        """Desktop's remote-access intent, On, as Desktop writes it."""
        desktop = Store(self.store.root.parent.resolve() / "desktop")
        desktop.write("remote-access.json", {"format": 1, "enabled": True, "revision": 1})
        config = self.store.read("config.json")
        config["remote_access"] = {"intent_path": str(desktop.root / "remote-access.json")}
        self.store.write("config.json", config)
        return desktop

    async def switch_off(self, desktop: Store, identity: str, revision: int) -> dict:
        desktop.write("remote-access.json", {"format": 1, "enabled": False, "revision": revision})
        return await self.companion.act("remote-access", {**self.request(identity),
            "intent_path": str(desktop.root / "remote-access.json"), "revision": revision})

    async def test_an_off_after_the_sign_in_began_keeps_code_off(self):
        """The recorded decision never overrides a later Off."""
        desktop = self.managed()
        await self.start_login()
        self.assertEqual((await self.switch_off(desktop, "d" * 32, 2))["result"]["status"], "off")
        self.approve()
        await self.companion.native("status")
        self.assertFalse(self.store.read("config.json")["enabled"])
        await self.companion.snapshot()
        self.assertFalse(self.store.read("config.json")["enabled"])

    async def test_only_an_unpair_that_may_have_reached_cloud_holds_the_switch(self):
        """The switch gate: a proofless unpair never reached Cloud, so it is not admitted
        and its record goes; one with a stored proof holds the switch, during which remote access reports progress."""
        desktop = self.managed()
        first = await self.sign_in("owner-A")
        await self.sign_out()
        second = await self.sign_in("owner-B", "b" * 32)
        await self.sign_out("8" * 32)
        await self.start_login("c" * 32)
        for identity, device in (("e" * 32, second), ("d" * 32, first)):
            self.companion.save({"operation_id": identity, "action": "unpair", "status": "unknown", "device_id": device,
                                 "admitted": True, "input": {}})
        self.store.write("companion-pending.json", {"operations": ["e" * 32, "d" * 32]})
        self.store.write(f"revocation-{second}.json", {"device_id": second, "stage": "awaiting_challenge", "expired_challenges": []})
        self.store.write(f"revocation-{first}.json", {"device_id": first, "stage": "prepared", "expired_challenges": [],
                                                       "path": f"/api/devices/{first}/revoke", "headers": {"X-OM-Signature": "sent"}})
        self.assertEqual(self.approve("owner-A"), first)
        await self.companion.native("status")
        self.assertEqual(self.companion.operation("e" * 32)["result"], {"status": "not_admitted"})
        self.assertIsNone(self.store.read(f"revocation-{second}.json", missing=True))
        self.assertEqual(self.store.read("config.json")["device"]["device_id"], second)
        snapshot = await self.companion.snapshot()
        self.assertEqual((snapshot["account"]["status"], snapshot["account"]["account"]), ("pending", None))
        self.assertEqual((snapshot["remote_access"]["state"], snapshot["remote_access"]["code"]),
                         ("starting", "connector_identity_changing"))
        with self.assertRaises(ConnectorError) as held:
            await self.companion.act("remote-access", {**self.request("6" * 32),
                                                       "intent_path": str(desktop.root / "remote-access.json"), "revision": 1})
        self.assertEqual(held.exception.code, "operation_pending")
        self.assertFalse(self.store.read("config.json")["enabled"])
        # An Off after the sign-in began keeps Code off once the revocation reads back as not admitted.
        desktop.write("remote-access.json", {"format": 1, "enabled": False, "revision": 2})
        self.companion.save({**self.companion.operation("d" * 32), "status": "rejected"})
        self.store.delete(f"revocation-{first}.json")
        self.companion.settle()
        config = self.store.read("config.json")
        self.assertEqual((config["device"]["device_id"], config["enabled"], config["revoked"]), (first, False, False))

    async def test_an_unknown_remote_access_start_holds_no_switch(self):
        desktop = self.managed()
        await self.sign_in("owner-A")
        await self.sign_out()
        await self.start_login("b" * 32)
        with patch("openmachine_device.companion.Companion.apply_intent",
                   side_effect=ConnectorError("unsafe_local_state", "Interrupted")):
            failed = await self.companion.act("remote-access", {**self.request("6" * 32),
                "intent_path": str(desktop.root / "remote-access.json"), "revision": 1})
        self.assertEqual(failed["status"], "unknown")
        self.approve("owner-B")
        await self.companion.native("status")
        self.assertEqual(self.store.read("config.json")["device"]["principal_id"], "owner-B")
        self.assertTrue(self.store.read("native-session.json")["registration_applied"])

    async def test_a_crash_at_each_step_resumes_to_the_same_end_state_without_cloud(self):
        """Interrupt each step of a switch, then restart the Connector."""
        def failing(method, matches):
            original = getattr(Store, method)
            def fail(store, name, *args, **options):
                if matches(name, *args):
                    raise Crash()
                return original(store, name, *args, **options)
            return patch.object(Store, method, fail)
        steps = {
            "committing": ("owner-B", lambda: failing("write", lambda name, value: name == "config.json" and "owner-B" in str(value))),
            "deleting the candidate": ("owner-A", lambda: failing("delete", lambda name: name.startswith("key-"))),
            "recording completion": ("owner-B", lambda: failing("write", lambda name, value: name == "native-session.json"
                                                                and value.get("registration_applied"))),
        }
        for step, (principal, crash) in steps.items():
            with self.subTest(step=step):
                self.setUp()
                first = await self.sign_in("owner-A")
                await self.sign_out()
                if principal == "owner-A":
                    second = await self.sign_in("owner-B", "b" * 32)
                    await self.sign_out("8" * 32)
                await self.start_login("c" * 32)
                approved = self.approve(principal)
                with crash(), self.assertRaises(Crash):
                    await self.companion.native("status")
                calls = len(self.cloud.calls)
                restarted = Companion(self.store)
                restarted.cloud.native = AsyncMock(side_effect=AssertionError("No Cloud read on resume"))
                restarted.settle()
                self.assertEqual(len(self.cloud.calls), calls)
                self.assertEqual(self.cloud.creates(), 3 if principal == "owner-A" else 2)
                self.assertEqual(len(self.cloud.devices), 2)
                session = self.store.read("native-session.json")
                self.assertTrue(session["registration_applied"])
                config = self.store.read("config.json")
                self.assertEqual(config["device"], self.stored(approved))
                self.assertEqual(config["devices"], [self.stored(first), self.stored(second if principal == "owner-A" else approved)])
                self.assertTrue(config["enabled"])
                self.assertEqual(self.keys(), {key_file(device["key_fingerprint"]) for device in config["devices"]})

    async def test_an_approval_after_restart_is_adopted_and_an_unnamed_key_file_is_deleted(self):
        """The login operation stays readable by ID after the switch."""
        stray = key_file(Identity.create(self.store).fingerprint)
        self.cloud.lose_creation = True
        self.assertEqual((await self.start_login())["status"], "unknown")
        self.assertNotIn(stray, self.keys())
        session = self.store.read("native-session.json")
        self.assertEqual(self.keys(), {key_file(key_fingerprint(session["public_key"]))})
        device = self.approve()
        await self.open().act("snapshot", {})
        self.assertEqual(self.store.read("config.json")["device"], self.stored(device))
        recovered = await self.open().act("operation", {"operation_id": "a" * 32})
        self.assertEqual(recovered["status"], "verified")
        self.assertEqual(recovered["result"]["status"], "active")
        self.assertEqual(self.cloud.creates(), 1)

    async def test_denial_cancellation_or_sign_out_before_adoption_deletes_the_candidate(self):
        """A denied, cancelled or signed-out sign-in deletes its candidate key and stores no device."""
        for ending in ("deny", "cancel-login", "sign-out"):
            with self.subTest(ending=ending):
                self.setUp()
                await self.start_login()
                session = self.store.read("native-session.json")
                if ending == "deny":
                    self.cloud.sessions[session["session_id"]]["status"] = "denied"
                    self.assertEqual((await self.companion.native("status"))["status"], "denied")
                else:
                    self.assertEqual((await self.companion.act(ending, self.request("b" * 32)))["status"], "verified")
                self.assertEqual(self.keys(), set())
                self.assertNotIn("device", self.store.read("config.json"))
                # The ended sign-in reads back without its key, and a new one starts.
                calls = len(self.cloud.calls)
                self.assertNotEqual((await self.companion.native("status"))["status"], "pending")
                self.assertEqual(len(self.cloud.calls), calls)
                self.assertIsNone((await self.companion.snapshot())["account"])
                await self.sign_in("owner-A", "c" * 32)

    async def test_stored_devices_from_another_computer_are_forgotten_only_under_the_operation_lock(self):
        """A different hardware ID or a missing key forgets every stored device and revokes nothing."""
        async def invoke(*arguments):
            with patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))):
                return await command(parser().parse_args(["--state-dir", str(self.store.root), *arguments]))
        for case in ("another computer", "a missing key of the device in use", "a missing key of another device",
                     "an earlier build", "an interrupted forget"):
            with self.subTest(case=case):
                self.setUp()
                self.patch("openmachine_device.cloud.Cloud.request", side_effect=AssertionError("Nothing is revoked"))
                first = await self.sign_in("owner-A")
                await self.sign_out()
                second = await self.sign_in("owner-B", "b" * 32)
                self.store.write("revocation-" + first + ".json", {"fixture": True})
                # An operation of the other computer's that this one can never finish.
                self.companion.save({"operation_id": "e" * 32, "action": "unpair", "status": "unknown", "device_id": first,
                                     "admitted": True, "input": {}})
                self.store.write("companion-pending.json", {"operations": ["e" * 32]})
                if case == "another computer":
                    self.hardware.return_value = "hardware-B"
                elif case == "an earlier build":
                    # Its config.device names no key, and its key is identity.key.
                    config = self.store.read("config.json")
                    config["device"] = {key: config["device"][key] for key in ("device_id", "grant_id", "principal_id")}
                    config.pop("devices")
                    self.store.write("config.json", config)
                    self.store.write_bytes("identity.key", b"\1" * 32, create=True)
                else:
                    missing = first if case == "a missing key of another device" else second
                    self.store.delete(key_file(self.stored(missing)["key_fingerprint"]))
                before = self.store.read("config.json")
                with self.assertRaises(ConnectorError) as reported:
                    await invoke("companion", "connection")
                self.assertEqual(reported.exception.code, "hardware_changed")
                self.assertEqual(self.store.read("config.json"), before)
                if case == "an interrupted forget":
                    with patch.object(Store, "write", side_effect=Crash), self.assertRaises(Crash):
                        await invoke("companion", "snapshot")
                    self.assertEqual(self.keys(), set())
                    self.assertEqual(self.store.read("config.json"), before)
                await invoke("companion", "snapshot")
                config = self.store.read("config.json")
                self.assertFalse({"device", "devices", "revoked"} & set(config))
                self.assertFalse(config["enabled"])
                self.assertEqual(self.keys(), set())
                for name in ("native-session.json", "revocation-" + first + ".json", "companion-pending.json", "identity.key"):
                    self.assertIsNone(self.store.read_bytes(name, missing=True))
                self.assertTrue(all(device["revoked_at"] is None for device in self.cloud.devices.values()))
                await self.sign_in("owner-A", "c" * 32)

    async def test_a_key_goes_only_to_the_create_and_a_refused_one_leaves_no_sign_in(self):
        """The create names the platform; Cloud takes a key from Linux only."""
        key = "omkey_" + "k" * 43
        for identity, platform, sent, refusal in (("a" * 32, "darwin", "macos", "key_platform"),
                                                  ("b" * 32, "linux", "linux", "key_invalid"),
                                                  # This computer already holds eight accounts' devices.
                                                  ("d" * 32, "linux", "linux", "native_device_limit"),
                                                  ("c" * 32, "linux", "linux", None)):
            self.cloud.refuse_key = refusal
            request = self.request(identity, device_name="My server", key=key)
            with self.subTest(refusal=refusal), patch("sys.platform", platform):
                result = await self.companion.act("login", request)
                path, create = self.cloud.calls[-1]
                self.assertEqual((path, create["platform"], create["key"]), ("/api/native-sessions", sent, key))
                # The key is no part of the operation: re-sending both reads the original back and sends nothing.
                calls = len(self.cloud.calls)
                self.assertEqual(await self.companion.act("login", request), result)
                self.assertEqual(len(self.cloud.calls), calls)
                self.assertNotIn(key, repr(result))
                for file in self.store.root.iterdir():
                    self.assertNotIn(key.encode(), file.read_bytes(), file.name)
                session = self.store.read("native-session.json", missing=True)
                if refusal:
                    self.assertEqual((result["status"], result["error"]), ("rejected", refusal))
                    self.assertEqual((session, self.keys()), (None, set()))
                else:  # the sign-in after the refusals starts clean
                    self.assertEqual((result["status"], result["result"]["status"]), ("verified", "pending"))
                    self.assertEqual(self.keys(), {key_file(key_fingerprint(session["public_key"]))})

    async def move(self, identity="b" * 32) -> dict:
        return await self.companion.act("move", self.request(identity, key="ommove_" + "m" * 43))

    async def test_a_move_registers_this_computer_in_another_account_and_keeps_it_connected(self):
        """Console, Move: the secret goes only to the create, and the approved device replaces the one in use."""
        left = await self.sign_in("owner-A")
        self.cloud.move_to = "team-T"
        with patch("sys.platform", "linux"):
            result = await self.move()
        self.assertEqual((result["status"], result["result"]["status"]), ("verified", "active"))
        path, create = self.cloud.calls[-1]
        self.assertEqual((path, create["key"], create["platform"]), ("/api/native-sessions", "ommove_" + "m" * 43, "linux"))
        for file in self.store.root.iterdir():
            self.assertNotIn(b"ommove_", file.read_bytes(), file.name)
        self.assertEqual((await self.open().native("status"))["status"], "active")
        config = self.store.read("config.json")
        joined = config["device"]["device_id"]
        self.assertNotEqual(joined, left)
        self.assertEqual((config["device"], config["enabled"], config.get("revoked")), (self.stored(joined), True, False))
        self.assertEqual(self.cloud.devices[joined]["principal_id"], "team-T")
        self.assertIsNotNone(self.cloud.devices[left]["revoked_at"])
        self.assertEqual(self.keys(), {key_file(device["key_fingerprint"]) for device in config["devices"]})

    async def test_a_move_that_fails_leaves_this_computer_as_it_was(self):
        device = await self.sign_in("owner-A")
        before = (self.store.read("config.json"), self.store.read("native-session.json"), self.keys())
        unchanged = lambda: (self.store.read("config.json"), self.store.read("native-session.json"), self.keys())
        # Cloud refuses it: no sign-in is left there or here.
        self.cloud.refuse_key = "move_refused"
        result = await self.move()
        self.assertEqual((result["status"], result["error"]), ("rejected", "move_refused"))
        self.assertEqual(unchanged(), before)
        # The request never reaches Cloud: unknown until the sign-in it would have made can no longer be admitted.
        self.cloud.refuse_key, self.cloud.drop_creation = None, True
        self.assertEqual((await self.move("c" * 32))["status"], "unknown")
        self.assertEqual((await self.open().recover("c" * 32))["status"], "unknown")
        self.assertEqual(unchanged()[:2], before[:2])
        with self.assertRaises(ConnectorError) as held:  # nothing else changes this computer meanwhile
            await self.open().act("login", self.request("d" * 32))
        self.assertEqual(held.exception.code, "operation_pending")
        with patch("openmachine_device.companion.time.time", return_value=time.time() + 61):
            result = await self.open().recover("c" * 32)
        self.assertEqual((result["status"], result["result"]["status"]), ("verified", "expired"))
        self.assertEqual(unchanged(), before)
        self.assertEqual(((await self.open().native("status"))["status"], self.cloud.devices[device]["revoked_at"]), ("active", None))
        # A computer that is signed out or stopped is not moved, and nothing is sent.
        await self.sign_out()
        creates = self.cloud.creates()
        result = await self.move("e" * 32)
        self.assertEqual((result["status"], result["error"], self.cloud.creates()), ("rejected", "move_unavailable", creates))

    async def test_a_move_whose_reply_was_lost_is_read_back_and_adopted(self):
        left = await self.sign_in("owner-A")
        self.cloud.move_to, self.cloud.lose_creation = "team-T", True
        self.assertEqual((await self.move())["status"], "unknown")
        # Until it is read back this computer still names the device it had, which Cloud removed.
        self.assertEqual(self.store.read("config.json")["device"]["device_id"], left)
        result = await self.open().recover("b" * 32)
        self.assertEqual((result["status"], result["result"]["status"]), ("verified", "active"))
        config = self.store.read("config.json")
        self.assertEqual((self.cloud.devices[config["device"]["device_id"]]["principal_id"], config["enabled"]), ("team-T", True))
        self.assertEqual(self.cloud.creates(), 2)

    async def test_a_sign_in_creates_the_hardware_id_of_a_linux_machine_that_has_none(self):
        """The device is bound to the created ID."""
        self.hardware.side_effect = hardware_id  # the real rule
        with patch("sys.platform", "linux"), patch("openmachine_device.store.MACHINE_ID", self.store.root / "absent"):
            await self.sign_in()
        self.assertEqual(self.store.read("config.json")["device"]["hardware_id"], self.store.read_bytes("hardware-id").decode())

    async def test_a_sign_in_that_creates_the_hardware_id_uses_no_device_stored_under_another(self):
        """Devices copied from a computer whose ID was readable are not signed in with."""
        await self.sign_in()
        await self.sign_out()
        config, keys = self.store.read("config.json"), self.keys()
        self.hardware.side_effect = hardware_id
        with patch("sys.platform", "linux"), patch("openmachine_device.store.MACHINE_ID", self.store.root / "absent"):
            refused = await self.companion.act("login", self.request("b" * 32))
        self.assertEqual((refused["status"], refused["error"]), ("rejected", "hardware_changed"))
        self.assertEqual((self.store.read("config.json"), self.keys()), (config, keys))

    async def test_an_unreadable_hardware_id_resets_nothing_and_refuses_sign_in(self):
        """om's own refusal to go online is tested in test/daemon."""
        await self.sign_in()
        config, keys = self.store.read("config.json"), self.keys()
        self.hardware.side_effect = ConnectorError("hardware_unavailable", "Unreadable")
        with patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))), \
                patch("openmachine_device.cloud.Cloud.native", self.cloud.native):
            snapshot = await command(parser().parse_args(["--state-dir", str(self.store.root), "companion", "snapshot"]))
        self.assertEqual(snapshot["account"]["status"], "active")
        self.assertEqual((self.store.read("config.json"), self.keys()), (config, keys))
        await self.sign_out()
        refused = await self.companion.act("login", self.request("b" * 32, device_name="My Mac"))
        self.assertEqual((refused["status"], refused["error"]), ("rejected", "hardware_unavailable"))
        self.assertEqual(self.keys(), keys)

    async def test_an_unreadable_hardware_id_revokes_nothing_until_it_reads_again(self):
        """The keys may be another Mac's, so unpair and remove neither record nor send a revocation,
        a retained proof included, nor cancel a sign-in in flight."""
        first = await self.sign_in("owner-A")
        await self.sign_out()
        await self.start_login("b" * 32)
        config, keys = self.store.read("config.json"), self.keys()
        self.hardware.side_effect = ConnectorError("hardware_unavailable", "Unreadable")
        refused = await self.companion.act("unpair", self.request("c" * 32))
        self.assertEqual((refused["status"], refused["error"]), ("rejected", "hardware_unavailable"))
        self.assertEqual(self.store.read("config.json"), config)
        path = f"/api/devices/{first}/revoke"
        retained = {"device_id": first, "stage": "prepared", "expired_challenges": [], "path": path,
                    "headers": Identity.load(self.store, config["device"]["key_fingerprint"]).proof(
                        ORIGIN, "POST", path, "nonce-" + first, "challenge-value", b"{}")}
        for record in (None, retained):
            if record:
                self.store.write(f"revocation-{first}.json", record)
            result = await self.remove()
            self.assertEqual(result["errors"]["cloud_revocation"], "hardware_unavailable")
            self.assertEqual(self.store.read(f"revocation-{first}.json", missing=True), record)
        self.cloud.devices[first]["revoked_at"] = 2
        self.store.write(f"revocation-{first}.json", {**retained, "receipt": {"status": "revoked", "device_id": first, "revoked_at": 2}})
        result = await self.remove()
        self.assertEqual((result["status"], result["local_removal"]), ("incomplete", "not_attempted"))
        self.assertEqual(result["errors"]["cloud_revocation"], "hardware_unavailable")
        self.assertEqual(self.store.names("revocation"), [f"revocation-{first}.json"])
        self.assertEqual(self.keys(), keys)
        self.assertEqual(self.cloud.sessions[self.store.read("native-session.json")["session_id"]]["status"], "pending")
        second = self.approve("owner-B")
        self.hardware.side_effect = None
        self.assertEqual((await self.remove())["status"], "removed")
        self.assertEqual([self.cloud.devices[device]["revoked_at"] for device in (first, second)], [2, 2])

    async def test_stale_stored_devices_are_forgotten_and_remove_revokes_every_stored_device(self):
        """Devices removed in the Console are forgotten at the next sign-in; remove revokes every one still stored."""
        first = await self.sign_in("owner-A")
        await self.sign_out()
        second = await self.sign_in("owner-B", "b" * 32)
        await self.sign_out("8" * 32)
        third = await self.sign_in("owner-C", "c" * 32)
        await self.sign_out("7" * 32)
        self.store.write(f"revocation-{second}.json", {"device_id": second, "stage": "awaiting_challenge"})
        # Console removes two of them, including the device in use.
        self.cloud.devices[second]["revoked_at"] = self.cloud.devices[third]["revoked_at"] = 1
        await self.start_login("d" * 32)
        config = self.store.read("config.json")
        self.assertEqual(config["devices"], [self.stored(first)])
        self.assertFalse({"device", "revoked"} & set(config))
        self.assertIsNone(self.store.read(f"revocation-{second}.json", missing=True))
        session = self.store.read("native-session.json")
        self.assertEqual(self.keys(), {key_file(self.stored(first)["key_fingerprint"]),
                                       key_file(key_fingerprint(session["public_key"]))})
        fourth = self.approve("owner-B")
        await self.companion.native("status")
        self.assertEqual([device["device_id"] for device in self.store.read("config.json")["devices"]], [first, fourth])

        result = await self.remove()
        self.assertEqual((result["status"], result["cloud_revocation"]), ("removed", "verified"))
        self.assertEqual(self.cloud.devices[first]["revoked_at"], 2)
        self.assertEqual(self.cloud.devices[fourth]["revoked_at"], 2)

    async def test_remove_also_ends_a_sign_in_in_flight(self):
        """A pending sign-in is cancelled; the device of an approval, also one that wins against that cancel,
        is revoked by its own key without a switch."""
        for case in ("pending", "approved", "approved during removal"):
            with self.subTest(case=case):
                self.setUp()
                first = await self.sign_in("owner-A")
                await self.sign_out()
                await self.start_login("b" * 32)
                session = self.store.read("native-session.json")["session_id"]
                approve = lambda: self.cloud.approve(session, "owner-B")
                if case == "approved during removal":
                    self.cloud.before_cancel = approve
                elif case != "pending":
                    approve()
                result = await self.remove()
                self.assertEqual((result["status"], result["cloud_revocation"]), ("removed", "verified"))
                self.assertEqual(self.cloud.devices[first]["revoked_at"], 2)
                if case == "pending":
                    self.assertEqual(self.cloud.sessions[session]["status"], "cancelled")
                else:
                    self.assertEqual(self.cloud.devices[self.cloud.sessions[session]["device_id"]]["revoked_at"], 2)
                self.assertEqual(self.store.read("config.json")["device"]["device_id"], first)

    async def test_remove_after_an_interrupted_removal_completes(self):
        await self.sign_in()
        self.store.delete("config.json")
        result = await self.remove()
        self.assertEqual((result["status"], result["cloud_revocation"]), ("removed", "not_paired"))

    async def test_remove_first_completes_an_interrupted_switch(self):
        """Any later command resumes a switch, so removal revokes the device now in use."""
        first = await self.sign_in("owner-A")
        await self.sign_out()
        await self.start_login("b" * 32)
        second = self.approve("owner-B")
        original = Store.write
        def crash(store, name, value, **options):
            if name == "config.json" and value.get("device", {}).get("device_id") == second:
                raise Crash()
            return original(store, name, value, **options)
        with patch.object(Store, "write", crash), self.assertRaises(Crash):
            await self.companion.native("status")
        result = await self.remove()
        self.assertEqual(result["cloud_revocation"], "verified")
        config = self.store.read("config.json")
        self.assertEqual((config["device"]["device_id"], config["revoked"], config["enabled"]), (second, True, False))
        self.assertEqual([self.cloud.devices[device]["revoked_at"] for device in (first, second)], [2, 2])

    async def test_an_interrupted_switch_is_resumed_by_another_command(self):
        await self.sign_in("owner-A")
        await self.sign_out()
        await self.start_login("b" * 32)
        self.approve("owner-B")
        original = Store.write
        def crash(store, name, value, **options):
            if name == "native-session.json" and value.get("registration_applied"):
                raise Crash()
            return original(store, name, value, **options)
        with patch.object(Store, "write", crash), self.assertRaises(Crash):
            await self.companion.native("status")
        signed_out = await self.companion.act("sign-out", self.request("7" * 32))
        self.assertEqual(signed_out["status"], "verified")
        self.assertTrue(self.store.read("native-session.json")["registration_applied"])
        self.assertEqual(self.store.read("config.json")["device"]["principal_id"], "owner-B")

    async def test_an_unfinished_sign_in_of_an_earlier_build_is_forgotten(self):
        """An earlier build's key, identity.key, counts as a missing key."""
        seed = bytes(range(32))
        self.store.write_bytes("identity.key", seed, create=True)
        self.store.write("native-session.json", {"session_id": "s" * 43, "challenge_id": "s" * 43, "challenge": "c",
            "server": ORIGIN, "public_key": Identity(Ed25519PrivateKey.from_private_bytes(seed)).public_key, "expires_at": 1})
        with patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))):
            with self.assertRaises(ConnectorError) as reported:
                await command(parser().parse_args(["--state-dir", str(self.store.root), "companion", "connection"]))
            self.assertEqual(reported.exception.code, "hardware_changed")
            await command(parser().parse_args(["--state-dir", str(self.store.root), "companion", "snapshot"]))
        for name in ("identity.key", "native-session.json"):
            self.assertIsNone(self.store.read_bytes(name, missing=True))
        await self.sign_in()

    async def remove(self) -> dict:
        return await self.command("remove")

    async def command(self, name: str, *, remove: bool = True) -> dict:
        """A CLI command, with Cloud revoking each device only by a proof from that device's own key; remove=False
        runs the real service.remove."""
        test, cloud = self, self.cloud
        class Revocations(Cloud):
            async def native(inner, *arguments):
                return await cloud.native(*arguments)

            async def challenge(inner, device_id):
                return {"challenge_id": "nonce-" + device_id, "challenge": "challenge-value", "expires_at": int(time.time()) + 60}

            async def request(inner, method, path, *, body=b"", headers=None):
                device_id = path.split("/")[3]
                key = Identity.load(test.store, key_fingerprint(cloud.devices[device_id]["public_key"]))
                test.assertEqual(headers, key.proof(ORIGIN, method, path, "nonce-" + device_id, "challenge-value", body))
                cloud.devices[device_id]["revoked_at"] = 2
                return {"status": "revoked", "device_id": device_id, "revoked_at": 2}
        with patch("openmachine_device.cli.Cloud", side_effect=lambda server: Revocations(server)):
            if not remove:
                return await command(parser().parse_args(["--state-dir", str(self.store.root), name]))
            with patch("openmachine_device.service.remove", return_value={"status": "removed"}) as removed:
                result = await command(parser().parse_args(["--state-dir", str(self.store.root), name]))
        if result["status"] == "removed":
            removed.assert_called_once()
        return result

    async def test_lost_login_response_recovers_the_original_registration(self):
        self.cloud.lose_creation = True
        self.assertEqual((await self.start_login())["status"], "unknown")
        device = self.approve()
        result = await self.open().recover("a" * 32)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["result"]["device"]["device_id"], device)
        self.assertEqual(self.store.read("config.json")["device"]["device_id"], device)
        self.assertEqual(self.cloud.creates(), 1)

    async def test_reopening_pending_sign_in_returns_its_existing_browser_link(self):
        await self.start_login()
        session = self.store.read("native-session.json")
        reopened = self.open()
        result = await reopened.act("login", {"operation_id": "b" * 32,
            "expected_binding": reopened.binding(), "device_name": "My Mac"})
        self.assertEqual(result["result"]["verification_uri"], ORIGIN + "/native-login?id=" + session["session_id"])
        self.assertEqual(result["result"]["status"], "pending")
        self.assertEqual(self.store.read("native-session.json"), session)
        self.assertEqual(self.cloud.creates(), 1)

    async def test_cancellation_after_browser_approval_ends_that_session_and_takes_code_off(self):
        device = await self.sign_in()
        result = await self.companion.act("cancel-login", self.request("b" * 32))
        self.assertEqual((result["status"], result["result"]["status"]), ("verified", "signed_out"))
        self.assertTrue(self.store.read("native-session.json")["ended"])
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertEqual(self.store.read("config.json")["device"], self.stored(device))
        self.assertTrue(self.companion.operation("b" * 32)["cancel_active"])
        before = len(self.cloud.calls)
        self.assertEqual((await self.companion.recover("b" * 32))["status"], "verified")
        self.assertEqual(len(self.cloud.calls), before)

    async def test_a_foreign_or_changed_receipt_never_replaces_the_stored_device(self):
        await self.start_login()
        device = self.approve()
        receipt = self.cloud.receipt
        for field, invalid in (("principal_id", "owner-B"), ("key_fingerprint", "0" * 64), ("grant_id", None),
                               ("grant_revision", "1"), ("grant_revision", True)):
            with self.subTest(field=field):
                self.cloud.receipt = lambda identity: {**receipt(identity), field: invalid}
                with self.assertRaises(ConnectorError):
                    await self.companion.native("status")
                self.assertNotIn("device", self.store.read("config.json"))
        self.cloud.receipt = receipt
        await self.companion.native("status")
        retained = self.store.read("config.json")
        for field, invalid in (("device_id", "device-9"), ("grant_id", "grant-9")):
            with self.subTest(field=field), self.assertRaises(ConnectorError):
                self.cloud.receipt = lambda identity: {**receipt(identity), field: invalid}
                await self.companion.native("status")
            self.assertEqual(self.store.read("config.json"), retained)
        self.cloud.receipt = receipt
        self.assertEqual(retained["device"], self.stored(device))

    async def test_cloud_revocation_never_enables_the_registered_device(self):
        device = await self.sign_in()
        self.cloud.devices[device]["revoked_at"] = 2
        await self.companion.native("status")
        self.assertTrue(self.store.read("config.json")["revoked"])
        self.assertFalse(self.store.read("config.json")["enabled"])

    async def test_an_offline_sign_out_stops_locally_and_is_completed_by_reading_it_back(self):
        """A sign-in lasts, so the sign-out Cloud never saw is sent again when its operation is read."""
        device = await self.sign_in()
        self.cloud.offline_sign_out = True
        result = await self.companion.act("sign-out", self.request("b" * 32))
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertEqual(self.store.read("config.json")["device"], self.stored(device))
        self.assertEqual((await self.companion.native("status"))["status"], "sign_out_pending")
        with self.assertRaises(ConnectorError) as held:
            await self.start_login("c" * 32)
        self.assertEqual(held.exception.code, "operation_pending")
        self.assertEqual((await self.companion.recover("b" * 32))["status"], "unknown")  # still offline
        self.cloud.offline_sign_out = False
        self.assertEqual((await self.companion.recover("b" * 32))["status"], "verified")
        session = self.store.read("native-session.json")
        self.assertTrue(session["ended"])
        self.assertEqual(self.cloud.sessions[session["session_id"]]["status"], "signed_out")
        await self.sign_in("owner-A", "c" * 32)

    async def test_a_cancel_or_sign_out_cloud_has_not_seen_abandons_a_held_switch(self):
        """A cancel or sign-out made while an unpair held the switch, and lost on the way to Cloud, abandons the
        switch as one Cloud saw does; reading it back then ends that sign-in and deletes its candidate."""
        for action, offline in (("cancel-login", "offline_cancel"), ("sign-out", "offline_sign_out")):
            with self.subTest(action=action):
                self.setUp()
                first = await self.sign_in("owner-A")
                await self.sign_out()
                await self.start_login("b" * 32)
                self.companion.save({"operation_id": "d" * 32, "action": "unpair", "status": "unknown", "device_id": first,
                                     "admitted": True, "input": {}})
                self.store.write("companion-pending.json", {"operations": ["d" * 32]})
                self.store.write(f"revocation-{first}.json", {"device_id": first, "stage": "prepared", "expired_challenges": [],
                    "path": f"/api/devices/{first}/revoke", "headers": {"X-OM-Signature": "sent"}})
                second = self.approve("owner-B")
                await self.companion.native("status")
                setattr(self.cloud, offline, True)
                self.assertEqual((await self.companion.act(action, self.request("c" * 32)))["status"], "unknown")
                self.companion.save({**self.companion.operation("d" * 32), "status": "rejected"})
                self.store.delete(f"revocation-{first}.json")
                snapshot = await self.companion.act("snapshot", {})
                self.assertEqual(snapshot["account"]["status"], "sign_out_pending")
                self.assertNotEqual(snapshot["remote_access"]["code"], "connector_identity_changing")
                config = self.store.read("config.json")
                self.assertEqual(([device["device_id"] for device in config["devices"]], config["device"]["device_id"]),
                                 ([first], first))
                setattr(self.cloud, offline, False)
                self.assertEqual((await self.companion.recover("c" * 32))["status"], "verified")
                self.assertEqual(self.cloud.sessions[self.store.read("native-session.json")["session_id"]]["status"], "signed_out")
                self.assertEqual(self.keys(), {key_file(config["device"]["key_fingerprint"])})
                self.assertIn(second, self.cloud.devices)

    async def test_signing_in_after_an_unpair_that_never_reached_cloud(self):
        """An unpair without a stored proof never reached Cloud, so it holds no later sign-in."""
        device = await self.sign_in()
        await self.sign_out()
        self.companion.cloud.challenge = AsyncMock(side_effect=ConnectorError("cloud_unavailable", "Offline"))
        unpair = await self.companion.act("unpair", self.request("e" * 32))
        self.assertEqual((unpair["status"], unpair["error"]), ("unknown", "cloud_unavailable"))
        self.assertEqual((await self.start_login("b" * 32))["status"], "verified")
        self.assertEqual(self.companion.operation("e" * 32)["result"], {"status": "not_admitted"})
        self.assertEqual(self.store.names("revocation"), [])
        self.assertEqual(self.approve(), device)
        await self.companion.native("status")
        self.assertTrue(self.store.read("config.json")["enabled"])

    async def test_signing_in_again_drops_an_unpair_that_never_reached_cloud(self):
        """The device in use, like a switch, is not held by an unpair without a stored proof."""
        device = await self.sign_in()
        await self.sign_out()
        await self.start_login("b" * 32)
        self.companion.save({"operation_id": "e" * 32, "action": "unpair", "status": "unknown", "device_id": device,
                             "admitted": True, "input": {}})
        self.store.write("companion-pending.json", {"operations": ["e" * 32]})
        self.store.write(f"revocation-{device}.json", {"device_id": device, "stage": "awaiting_challenge", "expired_challenges": []})
        self.assertEqual(self.approve(), device)
        original = Store.delete
        def crash(store, name):
            if name == f"revocation-{device}.json":
                raise Crash()
            return original(store, name)
        # A crash while deleting the record: the next read repeats the handling, so the record never outlives it.
        with patch.object(Store, "delete", crash), self.assertRaises(Crash):
            await self.companion.native("status")
        await self.open().native("status")
        self.assertEqual(self.companion.operation("e" * 32)["result"], {"status": "not_admitted"})
        self.assertIsNone(self.store.read(f"revocation-{device}.json", missing=True))
        self.assertTrue(self.store.read("config.json")["enabled"])

    async def test_an_offline_cancel_that_an_approval_won_takes_code_off_and_signs_out_on_read_back(self):
        await self.start_login()
        config = self.store.read("config.json")
        config["enabled"] = True  # remote access was On
        self.store.write("config.json", config)
        self.cloud.offline_cancel = True
        self.assertEqual((await self.companion.act("cancel-login", self.request("b" * 32)))["status"], "unknown")
        self.cloud.offline_cancel = False
        self.approve()
        recovered = await self.companion.recover("b" * 32)
        self.assertEqual((recovered["status"], recovered["result"]["status"]), ("verified", "signed_out"))
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertTrue(self.companion.operation("b" * 32)["cancel_active"])

    async def test_an_interrupted_removal_of_a_pending_first_sign_in_completes(self):
        """service.remove deletes native-session.json before the keys, so a retry never needs a deleted key."""
        await self.start_login()
        original = Store.delete
        def crash(store, name):
            if name == "config.json":
                raise Crash()
            return original(store, name)
        with patch.object(Store, "delete", crash), self.assertRaises(Crash):
            await self.command("remove", remove=False)
        self.assertEqual(self.keys(), set())
        result = await self.command("remove", remove=False)
        self.assertEqual(result["status"], "removed")
        self.assertIsNone(self.store.read("config.json", missing=True))

    async def test_retired_unknown_operation_is_preserved_and_does_not_block_sign_in(self):
        old = {"operation_id": "f" * 32, "action": "pair-confirm", "status": "unknown",
               "admitted": True, "input": {}}
        self.companion.save(old)
        self.store.write("companion-pending.json", {"operations": [old["operation_id"]]})
        device = await self.sign_in()
        self.assertEqual(self.store.read("config.json")["device"]["device_id"], device)
        self.assertEqual((await self.companion.recover(old["operation_id"]))["status"], "unknown")
        self.assertEqual(self.companion.operation(old["operation_id"]), old)


class ComputerName(unittest.TestCase):
    def test_a_mac_reads_its_computer_name_and_falls_back_to_the_host_name(self):
        with (patch("openmachine_device.companion.sys.platform", "darwin"),
              patch("openmachine_device.companion.socket.gethostname", return_value="Ada-1.local"),
              patch("openmachine_device.companion.subprocess.run",
                    return_value=SimpleNamespace(stdout="Ada’s MacBook Pro\n")) as run):
            self.assertEqual(computer_name(), "Ada’s MacBook Pro")
            self.assertEqual(run.call_args.args[0], ["/usr/sbin/scutil", "--get", "ComputerName"])
            run.return_value = SimpleNamespace(stdout="\n")
            self.assertEqual(computer_name(), "Ada-1.local")
            run.side_effect = subprocess.CalledProcessError(1, "scutil")
            self.assertEqual(computer_name(), "Ada-1.local")
        with (patch("openmachine_device.companion.sys.platform", "linux"),
              patch("openmachine_device.companion.socket.gethostname", return_value="build-box"),
              patch("openmachine_device.companion.subprocess.run") as run):
            self.assertEqual(computer_name(), "build-box")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
