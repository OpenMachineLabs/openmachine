"""Fresh CLI processes: local-only effects, original receipts and argument rejection."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "device"))

from openmachine_device.store import ORIGIN, Store

CLI_PROBE = r"""
import runpy
import sys

sys.path.insert(0, sys.argv.pop(1))
sys.argv[0] = "openmachine-connector"

def local_only(event, args):
    if event in {"socket.connect", "subprocess.Popen"}:
        raise AssertionError("The local fixture must not connect or start a process")

sys.addaudithook(local_only)
code = 0
try:
    runpy.run_module("openmachine_device", run_name="__main__")
except SystemExit as error:
    code = error.code
raise SystemExit(code)
"""


class LocalCLI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root / "connector")
        self.desktop = Store(self.root / "desktop")

    def cli(self, *arguments, value=None, returncode=0):
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", CLI_PROBE, str(ROOT / "device"),
             "--state-dir", str(self.store.root), *arguments],
            input="" if value is None else json.dumps(value), text=True,
            capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, returncode, result.stderr)
        return result

    def test_configure_selects_the_origin_once(self):
        staging = "https://api.staging.openmachine.app"
        self.assertEqual(json.loads(self.cli("configure", "--server", staging).stdout),
                         {"status": "configured", "server": staging})
        self.assertEqual(self.store.read("config.json"), {"version": 1, "server": staging, "enabled": False})
        self.assertEqual(json.loads(self.cli("configure").stdout)["server"], staging)
        self.assertEqual(json.loads(self.cli("inspect").stdout), {"status": "not_installed"})

    def test_connection_intent_and_original_receipt_are_local(self):
        self.store.write("config.json", {"version": 1, "server": ORIGIN, "enabled": False})
        connection = json.loads(self.cli("companion", "connection").stdout)
        self.assertEqual((connection["paired"], connection["remote_access"]["managed"]), (False, False))
        for enabled, revision, identity, expected in ((True, 1, "a" * 32, "pairing_required"),
                                                      (False, 2, "b" * 32, "off")):
            with self.subTest(enabled=enabled):
                self.desktop.write("remote-access.json", {"format": 1, "enabled": enabled, "revision": revision})
                request = {"operation_id": identity, "expected_binding": connection["binding"],
                           "intent_path": str(self.desktop.root / "remote-access.json"), "revision": revision}
                result = json.loads(self.cli("companion", "remote-access", value=request).stdout)
                self.assertEqual((result["status"], result["operation_id"]), ("verified", identity))
                self.assertEqual(result["result"]["status"], expected)
                self.assertEqual(result["result"]["remote_access"]["revision"], revision)
                recovered = json.loads(self.cli("companion", "operation", value={"operation_id": identity}).stdout)
                self.assertEqual(recovered, result)
        self.assertFalse(self.store.read("config.json")["enabled"])

    def test_local_rejection_preserves_its_code(self):
        self.store.write("config.json", {"version": 1, "server": ORIGIN, "enabled": False})
        failed = self.cli("companion", "remote-access", value={"expected_binding": "changed"}, returncode=1)
        self.assertEqual(json.loads(failed.stderr)["error"]["code"], "connector_binding_changed")
        self.assertIsNone(self.store.read("companion-pending.json", missing=True))

    def test_unknown_commands_actions_and_options_have_no_effect(self):
        for arguments in (("unknown-command",), ("companion", "unknown-action"), ("configure", "--unknown-option")):
            with self.subTest(arguments=arguments):
                self.assertRegex(self.cli(*arguments, returncode=2).stderr, "invalid choice|unrecognized arguments")
        self.assertIsNone(self.store.read("config.json", missing=True))


if __name__ == "__main__":
    unittest.main()
