"""Tests for service behavior at the component layer."""

import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from openmachine.foundation.state import private_directory
from openmachine.service import checks, install, payload, processes
from test.support.payload_cases import PayloadCaseFixture


class PayloadTests(PayloadCaseFixture, unittest.TestCase):
    def test_manifest_binds_all_bytes_paths_modes_and_links(self):
        self.assertEqual(payload.verify(self.source, self.digest), self.manifest)
        file = self.source / "requirements.lock"
        original = file.read_bytes()
        file.write_bytes(original + b"changed")
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        file.write_bytes(original)
        extra = self.source / "unexpected.py"
        extra.touch()
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        extra.unlink()
        file.chmod(0o666)
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        file.chmod(0o644)
        file.unlink()
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))

    def test_manifest_and_target_rejection_before_copy_or_execution(self):
        path = self.source / "manifest.json"
        original = path.read_bytes()
        for raw in (b"null", b"[]", b'{"format":1,"format":1}', b"\xff", b"{}"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, digest))
        path.write_bytes(original)
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, "0" * 64))
        with patch.object(payload, "MANIFEST_LIMIT", 8):
            self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        for target in ({"arch": "x86_64"}, {"minimum_os": "14.0"}):
            self.manifest["target"] = payload.TARGET | target
            digest = self.seal()
            self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, digest))
        self.manifest["target"] = dict(payload.TARGET)
        digest = self.seal()
        with patch.object(payload.platform, "machine", return_value="x86_64"):
            self.fault("unsupported_payload_host", lambda: payload.verify(self.source, digest))
        with patch.object(payload.platform, "mac_ver", return_value=("12.7", (), "")):
            self.fault("unsupported_payload_host", lambda: payload.verify(self.source, digest))
        with patch.object(processes, "run") as run:
            self.fault("invalid_runtime_payload", lambda: install.prepare_payload(self.config, self.state, self.source, "0" * 64))
            run.assert_not_called()
        self.assertFalse((self.state / "service").exists())

    def test_escaping_links_special_files_and_limits_are_rejected(self):
        link = self.source / "python/bin/python3"
        link.unlink()
        link.symlink_to(self.config)
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        link.unlink()
        link.symlink_to("python3.13")
        fifo = self.source / "pipe"
        os.mkfifo(fifo)
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        fifo.unlink()
        for name in ("FILE_LIMIT", "BYTE_LIMIT"):
            with self.subTest(limit=name), patch.object(payload, name, 1):
                self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        self.manifest["core"]["wheel"] = "../outside.whl"
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.seal()))

    def test_retained_payload_survives_source_replacement_and_is_not_overwritten(self):
        retained, manifest = payload.retain(self.state, self.source, self.digest)
        inode = (retained / "python/bin/python3.13").stat().st_ino
        again, _ = payload.retain(self.state, self.source, self.digest)
        self.assertEqual(again, retained)
        self.assertEqual((retained / "python/bin/python3.13").stat().st_ino, inode)
        self.assertEqual(retained.stat().st_mode & 0o777, 0o500)
        self.source.rename(self.root / "replaced-app")
        self.assertEqual(payload.verify(retained, self.digest), manifest)
        self.assertEqual(os.readlink(retained / "python/bin/python3"), "python3.13")

    def test_reuse_completes_interrupted_read_only_permissions(self):
        with patch.object(Path, "chmod", side_effect=OSError("fixture hardening interruption")), self.assertRaises(OSError):
            payload.retain(self.state, self.source, self.digest)
        retained = self.state / "service/payloads" / self.digest
        self.assertEqual(payload.verify(retained, self.digest), self.manifest)
        inode = (retained / "python/bin/python3.13").stat().st_ino
        self.assertTrue(retained.stat().st_mode & 0o200)
        with patch.object(payload.shutil, "copytree") as copy:
            again, _ = payload.retain(self.state, self.source, self.digest)
            copy.assert_not_called()
        self.assertEqual(again, retained)
        self.assertEqual((retained / "python/bin/python3.13").stat().st_ino, inode)
        for path in [retained, *retained.rglob("*")]:
            if not path.is_symlink():
                self.assertEqual(path.stat().st_mode & 0o222, 0, path)
        self.assertEqual(payload.verify(retained, self.digest), self.manifest)

    def test_interrupted_copy_and_disk_exhaustion_never_replace_prior_content(self):
        parent = private_directory(self.state / "service/payloads")
        with patch.object(payload.shutil, "disk_usage", return_value=Mock(free=0)):
            self.fault("insufficient_disk_space", lambda: payload.retain(self.state, self.source, self.digest))
        self.assertFalse((parent / self.digest).exists())
        def partial(source, destination, **kwargs):
            destination.mkdir()
            (destination / "partial").write_text("retain this failed copy")
            raise OSError("fixture interruption")
        with patch.object(payload.shutil, "copytree", side_effect=partial), self.assertRaises(OSError):
            payload.retain(self.state, self.source, self.digest)
        with patch.object(payload.shutil, "copytree") as copy:
            self.fault("invalid_runtime_payload", lambda: payload.retain(self.state, self.source, self.digest))
            copy.assert_not_called()
        self.assertEqual((parent / self.digest / "partial").read_text(), "retain this failed copy")


if __name__ == "__main__":
    unittest.main()
