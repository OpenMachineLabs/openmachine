"""Tests for service behavior at the component layer."""

import io
import json
from pathlib import Path
import runpy
import subprocess
import tarfile
import unittest
from unittest.mock import patch
from openmachine.service import payload
from test.support.payload_cases import PayloadCaseFixture


class PayloadTests(PayloadCaseFixture, unittest.TestCase):
    def test_signing_precedes_inventory_and_preserves_links(self):
        builder = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/build_runtime_payload.py"))
        native = self.source / "python/bin/python3.13"
        native.write_bytes(b"\xcf\xfa\xed\xfepython")
        (self.source / "manifest.json").unlink()

        def sign(arguments, **kwargs):
            if "--sign" in arguments:
                self.assertEqual(Path(arguments[-1]), native)
                native.write_bytes(native.read_bytes() + b"signature")

        with patch.object(subprocess, "run", side_effect=sign) as codesign:
            digest = builder["seal_payload"](self.source, self.manifest, "Developer ID Application: Fixture")
        self.assertEqual(codesign.call_count, 2)
        manifest = payload.verify(self.source, digest)
        self.assertTrue((self.source / "python/bin/python3").is_symlink())
        self.assertEqual(manifest["files"]["python/bin/python3.13"]["bytes"], len(native.read_bytes()))
        self.assertNotEqual(digest, self.digest)

    def test_signing_failure_does_not_publish_a_manifest(self):
        builder = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/build_runtime_payload.py"))
        (self.source / "python/bin/python3.13").write_bytes(b"\xcf\xfa\xed\xfepython")
        manifest = self.source / "manifest.json"
        manifest.unlink()
        with patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, "codesign")):
            with self.assertRaises(subprocess.CalledProcessError):
                builder["seal_payload"](self.source, self.manifest, "Developer ID Application: Fixture")
        self.assertFalse(manifest.exists())

    def test_unsigned_payload_keeps_the_existing_manifest_contract(self):
        builder = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/build_runtime_payload.py"))
        original = json.loads((self.source / "manifest.json").read_bytes())
        with patch.object(subprocess, "run") as command:
            digest = builder["seal_payload"](self.source, self.manifest, None)
        command.assert_not_called()
        self.assertEqual(payload.verify(self.source, digest), original)

    def test_git_entry_resolves_helpers_after_relocation_and_preserves_arguments(self):
        builder = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/build_runtime_payload.py"))
        (self.source / "git/bin/git").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$GIT_EXEC_PATH" "$GIT_TEMPLATE_DIR" "$@"\n')
        builder["git_entry"](self.source)
        moved = self.root / "relocated payload 'quoted'"
        self.source.rename(moved)
        result = subprocess.run([str(moved / "bin/git"), "a b", "$(not-a-command)"],
            cwd="/", env={"PATH": "/bin:/usr/bin", "GIT_EXEC_PATH": "/foreign"},
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(moved / "git/libexec/git-core"),
            str(moved / "git/share/git-core/templates"), "a b", "$(not-a-command)"])

    def test_producer_rejects_hardlinks_and_archive_path_escape(self):
        builder = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/build_runtime_payload.py"))
        for name, kind in (("hard", tarfile.LNKTYPE), ("../escape", tarfile.REGTYPE)):
            archive = self.root / ("hard.tar" if kind == tarfile.LNKTYPE else "escape.tar")
            with tarfile.open(archive, "w") as output:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "target" if kind == tarfile.LNKTYPE else ""
                output.addfile(member, io.BytesIO(b""))
            with self.subTest(name=name), self.assertRaises((ValueError, tarfile.FilterError)):
                builder["extract"](archive, self.root / "unpacked")
        self.assertFalse((self.root / "escape").exists())


if __name__ == "__main__":
    unittest.main()
