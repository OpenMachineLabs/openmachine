"""First install: only the bundle and an operation ID; recovery by that ID without replay."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_connector import write_bundle
from openmachine_device import setup
from openmachine_device.errors import ConnectorError
from openmachine_device.service import verify_bundle
from openmachine_device.store import ORIGIN, Store


class DesktopSetup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / "state")
        self.bundle = write_bundle(self.root / "bundle", "0.1.0")
        self.digest = verify_bundle(self.bundle)[1]
        for entry in (patch("openmachine_device.service.sys", SimpleNamespace(platform="darwin")),
                      patch.object(Path, "home", return_value=self.root / "home")):
            entry.start()
            self.addCleanup(entry.stop)
        self.request = {"operation_id": "a" * 32}

    async def act(self, kind, request=None):
        with self.store.lock("operation.lock"):
            return await setup.act(self.store, kind, request or self.request, self.bundle)

    async def test_preview_reads_only_the_bundle_and_existing_state(self):
        self.assertEqual(await self.act("preview", {}), {"protocol": 1, "server": ORIGIN, "existing": False,
                                                          "bundle_sha256": self.digest, "installation": "not_installed"})
        self.assertIsNone(self.store.read("config.json", missing=True))
        self.assertIsNone(self.store.read("installation.json", missing=True))

    async def test_start_creates_state_and_config_and_retains_the_bundle_without_a_key(self):
        self.assertFalse(self.store.root.exists())
        result = await self.act("start")
        self.assertEqual(result, {"operation_id": "a" * 32, "status": "verified", "can_continue": False})
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.read("config.json"), {"version": 1, "server": ORIGIN, "enabled": False})
        self.assertEqual(self.store.read("installation.json")["bundle_sha256"], self.digest)
        self.assertEqual(self.store.names("key-"), [])
        self.assertIsNone(self.store.read("native-session.json", missing=True))
        self.assertEqual(await self.act("start"), result)

    async def test_lost_reply_and_reopen_read_the_same_installation(self):
        def lost(store, bundle):
            original(store, bundle)
            raise OSError("reply lost")
        original = setup.service.install
        with patch.object(setup.service, "install", side_effect=lost), self.assertRaises(OSError):
            await self.act("start")
        self.store = Store(self.store.root)
        with patch.object(setup.service, "install", side_effect=AssertionError("No replay")):
            self.assertEqual((await self.act("status"))["status"], "verified")
            self.assertEqual((await self.act("start"))["status"], "verified")

    async def test_read_before_admission_fences_a_delayed_submission(self):
        self.assertEqual((await self.act("status"))["status"], "rejected")
        self.assertEqual((await self.act("start"))["status"], "rejected")
        self.assertIsNone(self.store.read("config.json", missing=True))
        self.assertIsNone(self.store.read("installation.json", missing=True))

    async def test_an_interrupted_install_continues_with_the_original_bundle_only(self):
        with patch.object(setup.service, "install", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            await self.act("start")
        self.assertTrue((await self.act("status"))["can_continue"])
        other = write_bundle(self.root / "other", "0.2.0")
        with self.assertRaises(ConnectorError) as changed:
            await setup.act(self.store, "continue", self.request, other)
        self.assertEqual(changed.exception.code, "setup_changed")
        self.assertIsNone(self.store.read("installation.json", missing=True))
        self.assertEqual((await self.act("continue"))["status"], "verified")

    async def test_existing_config_is_preserved(self):
        config = {"version": 1, "server": "https://staging.example", "enabled": True, "device": {"device_id": "existing"}}
        self.store.write("config.json", config)
        self.assertTrue((await self.act("preview", {}))["existing"])
        self.assertEqual((await self.act("preview", {}))["server"], "https://staging.example")
        self.assertEqual((await self.act("start"))["status"], "verified")
        self.assertEqual(self.store.read("config.json"), config)

    async def test_only_one_original_setup_can_be_pending(self):
        with patch.object(setup.service, "install", side_effect=OSError("interrupted")), self.assertRaises(OSError):
            await self.act("start")
        with self.assertRaises(ConnectorError) as pending:
            await self.act("start", {"operation_id": "b" * 32})
        self.assertEqual(pending.exception.code, "setup_pending")

    async def test_core_inputs_have_no_effect(self):
        result = await self.act("start", {"operation_id": "c" * 32, "core_socket": "/example/core.sock",
                                          "workspaces": ["project"], "selection": "s" * 64})
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("socket", self.store.read("config.json"))
        self.assertNotIn("workspaces", self.store.read("config.json"))
        with self.assertRaises(ConnectorError):
            await self.act("start", {"operation_id": "../x"})


if __name__ == "__main__":
    unittest.main()
