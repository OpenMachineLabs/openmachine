"""Companion actions, receipts and original operations across process reopen and uncertainty."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_connector import ROOT  # noqa: F401  (puts device on the path)
from openmachine_device.cli import command, parser
from openmachine_device.companion import ACTIONS, Companion
from openmachine_device.errors import ConnectorError
from openmachine_device.store import ORIGIN, Store, device_record
from fixture_device import HARDWARE, stored_device

RECEIPT = {"device_id": "device-A", "principal_id": "owner-A", "grant_id": "grant-A", "revoked_at": None,
           "grant_revision": 3}


class CompanionOwner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="companion-")
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "connector")
        self.key, stored = stored_device(self, self.store, {"device_id": "device-A", "grant_id": "grant-A",
                                                            "principal_id": "owner-A"})
        self.config = {"version": 1, "server": ORIGIN, "enabled": True, **stored}
        self.store.write("config.json", self.config)
        self.session = {"session_id": "s" * 43, "challenge_id": "s" * 43, "challenge": "private", "server": ORIGIN,
                        "public_key": self.key.public_key, "listed": [],
                        "registration": {"device": self.config["device"], "connect": True, "switch": False},
                        "registration_applied": True}
        self.store.write("native-session.json", self.session)
        self.companion = Companion(self.store)
        self.account = {"status": "active", "session_id": "s" * 43, "account": {"principal_id": "owner-A", "login": "alice"},
                        "key_fingerprint": self.key.fingerprint, "device": {**RECEIPT, "key_fingerprint": self.key.fingerprint}}

    def request(self, identity="a" * 32, **fields):
        return {"operation_id": identity, "expected_binding": self.companion.binding(), **fields}

    def test_receipt_and_stored_records_validate_the_device_binding(self):
        receipt = Companion.receipt({**RECEIPT, "key_fingerprint": self.key.fingerprint}, "owner-A", self.key.fingerprint,
                                    enrollment=True)
        self.assertEqual(receipt, {"device_id": "device-A", "grant_id": "grant-A", "principal_id": "owner-A"})
        self.assertEqual(set(device_record(self.store, receipt, self.key.fingerprint)),
                         {"device_id", "grant_id", "principal_id", "key_fingerprint", "hardware_id"})
        self.assertEqual(device_record(self.store, receipt, self.key.fingerprint)["hardware_id"], HARDWARE)
        for invalid in ({"grant_revision": None}, {"grant_revision": True}, {"grant_id": ""},
                        {"principal_id": "owner-B"}, {"key_fingerprint": "0" * 64}):
            with self.subTest(invalid=invalid), self.assertRaises(ConnectorError):
                Companion.receipt({**RECEIPT, "key_fingerprint": self.key.fingerprint, **invalid}, "owner-A",
                                  self.key.fingerprint, enrollment=True)
        missing = {key: value for key, value in RECEIPT.items() if key != "revoked_at"}
        with self.assertRaises(ConnectorError):
            Companion.receipt({**missing, "key_fingerprint": self.key.fingerprint}, "owner-A", self.key.fingerprint,
                              enrollment=True)

    async def test_snapshot_is_account_device_errors_and_connection(self):
        reply = {**RECEIPT, "key_fingerprint": self.key.fingerprint, "name": "My Mac", "observed_at_ms": 1,
                 "policy": {"revision": 1, "mode": "read_only", "folders": []}}
        self.companion.cloud.native = AsyncMock(side_effect=lambda path, *_: self.account if path.endswith("/status") else reply)
        snapshot = await self.companion.act("snapshot", {})
        self.assertEqual(set(snapshot), {"account", "device", "errors", *self.companion.connection()})
        self.assertEqual(snapshot["device"], {**reply, "status": "verified"})
        self.assertEqual(snapshot["errors"], {})
        self.assertEqual(snapshot["remote_access"]["state"], "ready")
        self.companion.cloud.native = AsyncMock(side_effect=ConnectorError("cloud_unavailable", "Offline"))
        offline = await self.companion.act("snapshot", {})
        self.assertEqual(offline["errors"], {"account": "cloud_unavailable"})
        self.assertEqual(offline["device"], {"device_id": "device-A", "name": "This device",
                                             "key_fingerprint": self.key.fingerprint, "status": "unverified"})

    async def test_sign_out_takes_code_off_before_cloud_even_offline(self):
        observed = []
        async def native(path, *_):
            observed.append(self.store.read("config.json")["enabled"])
            raise ConnectorError("cloud_unavailable", "Offline")
        self.companion.cloud.native = native
        result = await self.companion.act("sign-out", self.request())
        self.assertEqual((result["status"], result["error"]), ("unknown", "cloud_unavailable"))
        self.assertEqual(observed, [False])
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertTrue(self.store.read("native-session.json")["signed_out_locally"])
        self.assertEqual(self.store.read("config.json")["device"], self.config["device"])
        self.assertEqual(self.companion.connection()["remote_access"]["state"], "pairing_required")

    async def test_removed_device_stays_removed_after_the_session_ends_and_the_connector_reopens(self):
        reply = {**self.account, "status": "signed_out", "account": None,
                 "device": {**self.account["device"], "revoked_at": 1, "grant_revision": 4}}
        self.companion.cloud.native = AsyncMock(return_value=reply)
        snapshot = await self.companion.snapshot()
        self.assertEqual(snapshot["errors"], {})
        self.assertEqual(snapshot["account"]["status"], "signed_out")
        self.assertEqual(snapshot["remote_access"]["code"], "device_not_authorized")
        self.assertTrue(self.store.read("config.json")["revoked"])
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertEqual(self.store.read("config.json")["device"], self.config["device"])
        self.assertEqual(self.store.read("native-session.json")["ended"], reply)
        self.companion.cloud.native.assert_awaited_once()
        reopened = Companion(self.store)
        reopened.cloud.native = AsyncMock(side_effect=AssertionError("An ended session is not queried again."))
        self.assertEqual(reopened.connection()["remote_access"]["code"], "device_not_authorized")
        self.assertEqual((await reopened.snapshot())["remote_access"]["code"], "device_not_authorized")
        reopened.cloud.native.assert_not_awaited()

    async def test_local_logout_and_remote_session_end_are_not_device_removal(self):
        reply = {**self.account, "status": "signed_out", "account": None, "device": None}
        for local in (False, True):
            with self.subTest(local=local):
                self.store.write("config.json", self.config)
                self.store.write("native-session.json", self.session)
                self.companion.cloud.native = AsyncMock(return_value=reply)
                if local:
                    result = await self.companion.act("sign-out", self.request())
                    self.assertEqual(result["status"], "verified")
                else:
                    await self.companion.snapshot()
                self.assertNotIn("revoked", self.store.read("config.json"))
                self.assertEqual(self.companion.connection()["remote_access"]["state"], "pairing_required")
                self.assertIsNone(self.companion.connection()["remote_access"]["code"])

    async def test_a_removed_receipt_for_another_binding_does_not_change_or_end_this_device(self):
        for changed in ({"device_id": "device-B"}, {"principal_id": "owner-B"},
                        {"grant_id": "grant-B"}, {"key_fingerprint": "0" * 64}):
            with self.subTest(changed=changed):
                self.companion.cloud.native = AsyncMock(return_value={
                    **self.account, "status": "signed_out", "account": None,
                    "device": {**self.account["device"], "revoked_at": 1, **changed}})
                with self.assertRaises(ConnectorError):
                    await self.companion.native("status")
                self.assertEqual(self.store.read("config.json"), self.config)
                self.assertEqual(self.store.read("native-session.json"), self.session)

    async def test_a_cancel_that_an_approval_won_takes_code_off_before_signing_out(self):
        observed = []
        async def native(path, *_):
            if path.endswith("/cancel"):
                raise ConnectorError("native_login_active", "Approved")
            observed.append(self.store.read("config.json")["enabled"])
            return {"session_id": "s" * 43, "key_fingerprint": self.key.fingerprint, "status": "signed_out"}
        self.companion.cloud.native = native
        result = await self.companion.act("cancel-login", self.request())
        self.assertEqual((result["status"], result["result"]["status"]), ("verified", "signed_out"))
        self.assertEqual(observed, [False])

    async def test_policy_writes_this_device_directly_without_an_operation(self):
        reply = {"revision": 2, "mode": "workspace", "folders": ["/Users/me/code"]}
        self.companion.cloud.native = AsyncMock(return_value=reply)
        result = await self.companion.act("policy", {"expected_binding": self.companion.binding(), "expected_revision": 1,
                                                     "mode": "workspace", "folders": ["/Users/me/code"]})
        self.assertEqual(result, reply)
        path, _, value, signer = self.companion.cloud.native.await_args.args
        self.assertEqual((path, value, signer.fingerprint), ("/api/native-sessions/" + "s" * 43 + "/policy",
                         {"device_id": "device-A", "expected_revision": 1, "mode": "workspace", "folders": ["/Users/me/code"]},
                         self.key.fingerprint))
        self.assertEqual(list(self.store.root.glob("companion-*.json")), [])
        with self.assertRaises(ConnectorError) as changed:
            await self.companion.act("policy", {"expected_binding": "0" * 64, "expected_revision": 1, "mode": "read_only", "folders": []})
        self.assertEqual(changed.exception.code, "connector_binding_changed")
        # A device_id of the caller's never replaces this device's.
        await self.companion.act("policy", {"expected_binding": self.companion.binding(), "device_id": "device-B",
                                            "expected_revision": 1, "mode": "read_only", "folders": []})
        self.assertEqual(self.companion.cloud.native.await_args.args[2]["device_id"], "device-A")
        self.store.write("native-session.json", {**self.session, "ended": {"status": "signed_out"}})
        with self.assertRaises(ConnectorError) as ended:
            await self.companion.act("policy", {"expected_binding": self.companion.binding(), "expected_revision": 1,
                                                "mode": "read_only", "folders": []})
        self.assertEqual(ended.exception.code, "sign_in_required")

    async def test_a_companion_request_reads_up_to_40_kib(self):
        folders = ["/" + "a" * 1022 + str(n % 10) for n in range(32)]  # the largest policy, about 33 KB
        native = AsyncMock(return_value={"revision": 2, "mode": "workspace", "folders": folders})
        for size, accepted in ((40960, True), (40961, False)):
            value = {"expected_binding": self.companion.binding(), "expected_revision": 1, "mode": "workspace", "folders": folders}
            raw = json.dumps(value).encode()
            raw = raw[:-1] + b" " * (size - len(raw)) + b"}"
            args = parser().parse_args(["--state-dir", str(self.store.root), "companion", "policy"])
            with self.subTest(size=size), patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(raw))), \
                    patch.object(Companion, "native", native):
                if accepted:
                    self.assertEqual((await command(args))["folders"], folders)
                else:
                    with self.assertRaises(ConnectorError) as refused:
                        await command(args)
                    self.assertEqual(refused.exception.code, "invalid_arguments")

    async def test_connection_reads_do_not_wait_for_the_operation_lock(self):
        args = parser().parse_args(["--state-dir", str(self.store.root), "companion", "connection"])
        with self.store.lock("operation.lock"):
            result = await command(args)
        self.assertEqual(result["binding"], self.companion.binding())

    async def test_offline_unpair_disables_and_continues_only_the_original(self):
        self.companion.cloud.challenge = AsyncMock(side_effect=ConnectorError("cloud_unavailable", "Offline"))
        self.companion.account = AsyncMock(side_effect=AssertionError("Unpair must use the owned device proof."))
        original = await self.companion.act("unpair", self.request())
        self.assertEqual(original["status"], "unknown")
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertNotIn("revoked", self.store.read("config.json"))
        reopened = Companion(self.store)
        self.assertEqual((await reopened.recover("a" * 32))["status"], "unknown")
        self.assertTrue((await reopened.recover("a" * 32))["can_continue"])
        reopened.cloud.challenge = AsyncMock(return_value={"challenge_id": "proof-A", "challenge": "proof",
                                                           "expires_at": 2**40})
        reopened.cloud.request = AsyncMock(return_value={"status": "revoked", "device_id": "device-A", "revoked_at": 1})
        continued = await reopened.act("continue-revocation", {"operation_id": "a" * 32, "expected_binding": reopened.binding()})
        self.assertEqual(continued["operation_id"], original["operation_id"])
        self.assertEqual(continued["result"]["status"], "revoked")
        self.assertEqual(reopened.cloud.request.call_count, 1)
        self.assertEqual(reopened.connection()["remote_access"]["state"], "pairing_required")

    async def test_session_key_and_server_binding_and_unknown_operation_identity(self):
        self.store.write("native-session.json", {"session_id": "s" * 43, "challenge_id": "s" * 43,
            "challenge": "private", "server": ORIGIN, "public_key": self.key.public_key})
        self.companion.cloud.native = AsyncMock(return_value={"session_id": "s" * 43, "key_fingerprint": "wrong", "status": "active"})
        with self.assertRaisesRegex(ConnectorError, "identity changed"):
            await self.companion.native("status")
        session = self.store.read("native-session.json")
        session["expires_at"] = 1
        self.store.write("native-session.json", session)
        self.companion.cloud.native = AsyncMock(side_effect=ConnectorError("native_session_unavailable", "Not admitted"))
        self.assertEqual((await self.companion.native("status"))["status"], "expired")
        with self.assertRaises(ConnectorError):
            self.companion.operation("../../outside")
        with self.assertRaises(ConnectorError) as changed:
            await self.companion.act("unpair", {"operation_id": "b" * 32, "expected_binding": "0" * 64})
        self.assertEqual(changed.exception.code, "connector_binding_changed")

    async def test_missing_original_blocks_late_admission_and_retired_records_are_kept(self):
        self.companion.cloud.native = AsyncMock(side_effect=AssertionError("A read cannot admit login."))
        result = await self.companion.recover("a" * 32)
        self.assertEqual(result["status"], "rejected")
        with self.assertRaisesRegex(ConnectorError, "original operation"):
            await self.companion.act("login", self.request())
        record = {"operation_id": "b" * 32, "action": "approve-full", "status": "unknown",
                  "admitted": True, "input": {}}
        self.companion.save(record)
        self.store.write("companion-pending.json", {"operations": ["b" * 32]})
        self.assertEqual((await self.companion.recover("b" * 32))["status"], "unknown")
        self.assertEqual(self.companion.operation("b" * 32), record)
        self.companion.cloud.challenge = AsyncMock(side_effect=ConnectorError("cloud_unavailable", "Offline"))
        self.assertEqual((await self.companion.act("unpair", self.request("c" * 32)))["action"], "unpair")

    async def test_only_supported_actions_are_admitted(self):
        with self.assertRaises(ConnectorError) as refused:
            await self.companion.act("unknown-action", self.request())
        self.assertEqual(refused.exception.code, "invalid_action")
        self.assertEqual(ACTIONS, {"snapshot", "connection", "operation", "login", "cancel-login", "sign-out", "policy",
                                   "remote-access", "unpair", "continue-revocation", "move"})


if __name__ == "__main__":
    unittest.main()
