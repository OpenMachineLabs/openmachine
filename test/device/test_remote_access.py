"""Desktop's remote-access intent, connection() states and the enabled writes om follows."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_connector import ROOT  # noqa: F401  (puts device on the path)
from openmachine_device import remote_access
from openmachine_device.companion import Companion
from openmachine_device.errors import ConnectorError
from openmachine_device.store import Identity, ORIGIN, Store
from fixture_device import stored_device


class IntentFixture:
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="om-remote-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.desktop = Store(self.root / "desktop")
        self.store = Store(self.root / "connector")
        self.path = self.desktop.root / "remote-access.json"
        self.key, stored = stored_device(self, self.store, {"device_id": "device-A", "grant_id": "grant-A",
                                                            "principal_id": "owner-A"})
        self.config = {"version": 1, "server": ORIGIN, "enabled": False, **stored,
                       "remote_access": {"intent_path": str(self.path)}}
        self.intent(True, 1)
        self.store.write("config.json", self.config)
        self.signed_in()

    def signed_in(self, **fields):
        """An adopted sign-in of the device in use."""
        self.store.write("native-session.json", {
            "session_id": "s" * 43, "challenge_id": "s" * 43, "challenge": "c", "server": ORIGIN,
            "public_key": self.key.public_key, "listed": [],
            "registration": {"device": self.config["device"], "connect": True, "switch": False},
            "registration_applied": True, **fields})

    def intent(self, enabled, revision):
        self.desktop.write("remote-access.json", {"format": 1, "enabled": enabled, "revision": revision})

    def update(self, **fields):
        self.store.write("config.json", {**self.store.read("config.json"), **fields})

    def request(self, owner, revision=1, identity="a" * 32):
        return {"operation_id": identity, "expected_binding": owner.binding(),
                "intent_path": str(self.path), "revision": revision}

    def state(self) -> tuple:
        current = Companion(self.store).connection()["remote_access"]
        return current["state"], current["code"]


class IntentPolicy(IntentFixture, unittest.TestCase):
    def test_private_single_source_and_revision_guard(self):
        self.assertEqual(remote_access.read_intent(str(self.path), 1), {"format": 1, "enabled": True, "revision": 1})
        self.intent(False, 2)
        self.assertEqual(remote_access.policy(self.config), {"managed": True, "enabled": False, "revision": 2})
        with self.assertRaises(ConnectorError) as stale:
            remote_access.read_intent(str(self.path), 1)
        self.assertEqual(stale.exception.code, "remote_access_changed")
        for path in ("relative.json", str(self.path) + "/../remote-access.json", ""):
            with self.assertRaises(ConnectorError):
                remote_access.read_intent(path)

    def test_missing_invalid_and_unsafe_intent_fail_closed_without_repair(self):
        self.path.chmod(0o644)
        self.assertFalse(remote_access.policy(self.config)["enabled"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.path.chmod(0o600)
        for value in ({"format": 1, "enabled": 1, "revision": 1},
                      {"format": 1, "enabled": True, "revision": True},
                      {"format": 1, "enabled": True, "revision": -1},
                      {"format": 1, "enabled": True, "revision": 2**64},
                      {"format": 1, "enabled": True, "revision": 1, "extra": "ignored?"}):
            self.desktop.write("remote-access.json", value)
            self.assertFalse(remote_access.policy(self.config)["enabled"])
        self.path.unlink()
        self.assertFalse(remote_access.policy(self.config)["enabled"])
        self.assertFalse(self.path.exists())
        self.intent(True, 1)
        original = self.path.with_name("original.json")
        self.path.rename(original)
        self.path.symlink_to(original)
        self.assertFalse(remote_access.policy(self.config)["enabled"])
        self.path.unlink()
        os.link(original, self.path)
        self.assertFalse(remote_access.policy(self.config)["enabled"])


class ConnectionStates(IntentFixture, unittest.TestCase):
    def test_connection_shape(self):
        self.update(enabled=True)
        owner = Companion(self.store)
        connection = owner.connection()
        self.assertEqual(set(connection), {"protocol", "capabilities", "binding", "server", "key_fingerprint",
                                           "observed_at_ms", "paired", "remote_access"})
        self.assertEqual(connection["capabilities"], ["device-registration", "remote-access"])
        self.assertEqual((connection["protocol"], connection["server"], connection["paired"]), (1, ORIGIN, True))
        self.assertEqual(connection["key_fingerprint"], self.key.fingerprint)
        self.assertEqual(connection["remote_access"], {"managed": True, "enabled": True, "revision": 1, "state": "ready",
            "code": None, "admission_blocked": False, "connector_stopped": False})

    def test_binding_covers_server_and_the_device_in_use(self):
        owner = Companion(self.store)
        binding = owner.binding()
        self.update(enabled=True, remote_access=None)
        self.assertEqual(owner.binding(), binding)
        self.update(device={**self.config["device"], "device_id": "device-B"})
        self.assertNotEqual(owner.binding(), binding)
        self.update(device={**self.config["device"], "key_fingerprint": "0" * 64})
        self.assertNotEqual(owner.binding(), binding)
        self.update(device=self.config["device"], server="https://staging.example")
        self.assertNotEqual(owner.binding(), binding)

    def test_each_state_in_order(self):
        self.update(enabled=True)
        self.assertEqual(self.state(), ("ready", None))
        self.update(enabled=False)
        self.assertEqual(self.state(), ("starting", None))
        # pairing_required: revoked, a revocation begun, no device, no active sign-in.
        self.update(revoked=True)
        self.assertEqual(self.state(), ("pairing_required", "device_not_authorized"))
        self.update(revoked=False)
        self.store.write("revocation-device-A.json", {"device_id": "device-A"})
        self.assertEqual(self.state(), ("pairing_required", "device_not_authorized"))
        self.store.delete("revocation-device-A.json")
        for session in ({"signed_out_locally": True}, {"ended": {"status": "signed_out"}}, {"registration_applied": False}):
            with self.subTest(session=session):
                self.signed_in(**session)
                self.assertEqual(self.state(), ("pairing_required", None))
        self.store.delete("native-session.json")
        self.assertEqual(self.state(), ("pairing_required", None))
        self.signed_in()
        self.update(device=None)
        self.assertEqual(self.state(), ("pairing_required", None))
        self.update(device=self.config["device"])
        # starting with connector_identity_changing: an approved sign-in's switch is pending.
        self.signed_in(registration={"device": self.config["device"], "connect": True, "switch": True},
                       registration_applied=False)
        self.assertEqual(self.state(), ("starting", "connector_identity_changing"))
        self.update(revoked=True)
        self.assertEqual(self.state(), ("starting", "connector_identity_changing"))
        # off and error come first.
        self.intent(False, 2)
        self.assertEqual(self.state(), ("off", None))
        self.path.chmod(0o644)
        self.assertEqual(self.state(), ("error", "remote_access_invalid"))
        current = Companion(self.store).connection()["remote_access"]
        self.assertEqual((current["enabled"], current["admission_blocked"]), (False, True))

    def test_connector_stopped_follows_enabled_and_admission_follows_ready(self):
        for enabled, intent, stopped, blocked in ((True, True, False, False), (False, True, True, True),
                                                  (True, False, False, True), (False, False, True, True)):
            with self.subTest(enabled=enabled, intent=intent):
                self.update(enabled=enabled)
                self.intent(intent, 1)
                current = Companion(self.store).connection()["remote_access"]
                self.assertEqual((current["connector_stopped"], current["admission_blocked"]), (stopped, blocked))

    def test_an_unbound_connector_is_unmanaged(self):
        config = {key: value for key, value in self.store.read("config.json").items() if key != "remote_access"}
        self.store.write("config.json", {**config, "enabled": True})
        current = Companion(self.store).connection()["remote_access"]
        self.assertEqual(current, {"managed": False, "enabled": True, "revision": None, "state": "ready", "code": None,
                                   "admission_blocked": False, "connector_stopped": False})


class RemoteAccessWrites(IntentFixture, unittest.IsolatedAsyncioTestCase):
    async def test_on_sets_enabled_only_for_a_registered_unrevoked_signed_in_device(self):
        owner = Companion(self.store)
        owner.native = AsyncMock(side_effect=AssertionError("No Cloud request"))
        result = await owner.act("remote-access", self.request(owner))
        self.assertEqual((result["status"], result["result"]["status"]), ("verified", "ready"))
        self.assertTrue(self.store.read("config.json")["enabled"])
        self.assertEqual(self.desktop.read("remote-access.json"), {"format": 1, "enabled": True, "revision": 1})
        for index, (change, code) in enumerate(((lambda: self.update(revoked=True), "device_not_authorized"),
                                                (lambda: self.signed_in(signed_out_locally=True), None),
                                                (lambda: self.update(device=None), None)), 2):
            with self.subTest(code=code, index=index):
                self.update(enabled=True, revoked=False, device=self.config["device"])
                self.signed_in()
                change()
                self.intent(True, index)
                result = await owner.act("remote-access", self.request(owner, index, f"{index:032x}"))
                self.assertEqual(result["result"]["status"], "pairing_required")
                self.assertEqual(result["result"]["remote_access"]["code"], code)
                self.assertFalse(self.store.read("config.json")["enabled"])

    async def test_off_sets_enabled_false(self):
        self.update(enabled=True)
        owner = Companion(self.store)
        self.intent(False, 2)
        result = await owner.act("remote-access", self.request(owner, 2))
        self.assertEqual(result["result"]["status"], "off")
        self.assertTrue(result["result"]["remote_access"]["connector_stopped"])
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertEqual(self.store.read("config.json")["device"], self.config["device"])
        self.assertEqual(Identity.load(self.store, self.key.fingerprint).public_key, self.key.public_key)

    async def test_a_lost_write_is_completed_by_reading_the_same_operation(self):
        owner = Companion(self.store)
        request = self.request(owner)
        with patch.object(Companion, "apply_intent", side_effect=ConnectorError("unsafe_local_state", "Interrupted")):
            self.assertEqual((await owner.act("remote-access", request))["status"], "unknown")
        self.assertFalse(self.store.read("config.json")["enabled"])
        recovered = await Companion(self.store).act("remote-access", request)
        self.assertEqual((recovered["status"], recovered["result"]["status"]), ("verified", "ready"))
        self.assertTrue(self.store.read("config.json")["enabled"])

    async def test_a_newer_off_supersedes_an_unknown_on_and_a_stale_revision_is_refused(self):
        owner = Companion(self.store)
        with patch.object(Companion, "apply_intent", side_effect=ConnectorError("unsafe_local_state", "Interrupted")):
            self.assertEqual((await owner.act("remote-access", self.request(owner)))["status"], "unknown")
        self.intent(False, 2)
        off = await owner.act("remote-access", self.request(owner, 2, "b" * 32))
        self.assertEqual(off["result"]["status"], "off")
        self.assertEqual((await owner.recover("a" * 32))["result"]["status"], "superseded")
        with self.assertRaises(ConnectorError) as stale:
            await owner.act("remote-access", self.request(owner, 1, "c" * 32))
        self.assertEqual(stale.exception.code, "remote_access_changed")
        self.assertFalse(self.store.read("config.json")["enabled"])

    async def test_another_intent_path_is_refused(self):
        owner = Companion(self.store)
        other = Store(self.root / "other")
        other.write("remote-access.json", {"format": 1, "enabled": True, "revision": 1})
        result = await owner.act("remote-access", {**self.request(owner), "intent_path": str(other.root / "remote-access.json")})
        self.assertEqual((result["status"], result["error"]), ("unknown", "remote_access_changed"))
        self.assertEqual(self.store.read("config.json")["remote_access"], self.config["remote_access"])
        self.assertFalse(self.store.read("config.json")["enabled"])

    async def test_completed_history_does_not_exhaust_admission_or_replay_originals(self):
        owner = Companion(self.store)
        for index in range(520):
            identity = f"{index:032x}"
            self.store.write(f"companion-{identity}.json", {
                "operation_id": identity,
                "action": "remote-access" if index % 2 else "login",
                "input": {"operation_id": identity, "expected_binding": owner.binding()}, "status": "verified", "result": {"status": "retained"},
            })
        original = owner.operation("0" * 32)
        owner.cloud.native = AsyncMock(return_value={"status": "pending"})
        self.store.delete("native-session.json")
        request = {"operation_id": "f" * 32, "expected_binding": owner.binding()}
        result = await owner.act("login", request)
        self.assertEqual(result["status"], "verified")
        owner.cloud.native.assert_awaited_once()
        reopened = Companion(self.store)
        reopened.cloud.native = AsyncMock(side_effect=AssertionError("No replay"))
        self.assertEqual(await reopened.act("login", request), result)
        self.assertEqual(await reopened.act("login", original["input"]), reopened.public_operation(original))
        self.assertEqual(reopened.operation("0" * 32), original)
        self.assertEqual(self.store.read("companion-pending.json")["operations"], ["f" * 32])

    async def test_rapid_repeated_switches_keep_all_original_records_and_follow_the_last(self):
        owner = Companion(self.store)
        for revision in range(1, 21):
            self.intent(revision % 2 == 1, revision)
            request = self.request(owner, revision, f"{revision:032x}")
            result = await owner.act("remote-access", request)
            self.assertEqual(await owner.act("remote-access", request), result)
            self.assertEqual(self.store.read("config.json")["enabled"], revision % 2 == 1)
        self.assertEqual(len([p for p in self.store.root.glob("companion-*.json") if p.name != "companion-pending.json"]), 20)
        self.assertEqual(self.store.read("config.json")["device"], self.config["device"])


if __name__ == "__main__":
    unittest.main()
