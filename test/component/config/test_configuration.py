"""The machines-only configuration and its one first-run writer."""

import os
from pathlib import Path
import tempfile
import unittest

from openmachine.config import TEXT, initialize, load
from openmachine.foundation.sanitize import Fault


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-config-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "openmachine/config.toml"

    def fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_only_the_local_machine_with_local_transport_loads(self):
        self.path.parent.mkdir()
        self.path.write_text(TEXT)
        self.assertEqual(load(self.path).machines["local"].transport, "local")
        for text in ('schema_version = 1\n' + TEXT, TEXT + 'local_access = "full_access"\n',
                     TEXT + '[workspaces.project]\nmachine = "local"\nroot = "/tmp"\n', TEXT + "[skills]\nroots = []\n",
                     '[machines.remote]\ntransport = "local"\n', '[machines.local]\ntransport = "ssh"\n', "", "[machines"):
            with self.subTest(text=text):
                self.path.write_text(text)
                self.fault("invalid_config", lambda: load(self.path))
        self.path.unlink()
        self.fault("config_unavailable", lambda: load(self.path))

    def test_initialize_writes_once_and_verifies_its_own_bytes(self):
        self.assertEqual(initialize(self.path), {"created": str(self.path)})
        self.assertEqual((self.path.read_text(), self.path.stat().st_mode & 0o777), (TEXT, 0o600))
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            initialize(self.path)
        self.assertEqual(initialize(self.path, verify_existing=True), {"verified": str(self.path)})

    def test_verification_refuses_changed_or_unsafe_files_without_writing(self):
        initialize(self.path)
        for change in ("content", "bytes", "mode", "hardlink", "symlink"):
            with self.subTest(change=change):
                if change == "content":
                    self.path.write_text(TEXT + "\n")
                elif change == "bytes":
                    self.path.write_bytes(b"\xff" + TEXT.encode())
                elif change == "mode":
                    self.path.chmod(0o644)
                elif change == "hardlink":
                    os.link(self.path, self.path.with_name("alias"))
                else:
                    self.path.rename(self.path.with_name("real"))
                    self.path.symlink_to(self.path.with_name("real"))
                before = self.path.read_bytes()
                self.fault("configuration_mismatch", lambda: initialize(self.path, verify_existing=True))
                self.assertEqual(self.path.read_bytes(), before)
                for name in ("config.toml", "alias", "real"):
                    self.path.with_name(name).unlink(missing_ok=True)
                initialize(self.path)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            initialize(self.path, verify_existing=True)


if __name__ == "__main__":
    unittest.main()
