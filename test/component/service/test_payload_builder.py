"""Tests for service behavior at the component layer."""

import io
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import unittest
from test.support.payload_cases import PayloadCaseFixture
from openmachine.service import payload


class PayloadTests(PayloadCaseFixture, unittest.TestCase):
    def test_download_tags_pin_oldest_supported_linux_glibc(self):
        builder = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/build_runtime_payload.py"))
        common = ["--implementation", "cp", "--python-version", "3.13", "--abi", "cp313"]
        linux_options = builder["wheel_target_options"](payload.LINUX_TARGET)
        linux_tags = [f"manylinux_2_{minor}_x86_64" for minor in range(34, 4, -1)]
        linux_tags += ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"]
        self.assertEqual(linux_options,
                         [part for tag in linux_tags for part in ("--platform", tag)] + common)
        self.assertNotIn("manylinux_2_35_x86_64", linux_options)
        self.assertEqual(builder["wheel_target_options"](payload.TARGET),
                         ["--platform", "macosx_13_0_arm64", *common])

    def test_producer_refuses_product_signing_options(self):
        script = Path(__file__).resolve().parents[3] / "scripts/build_runtime_payload.py"
        result = subprocess.run([
            sys.executable, str(script), "--wheel", "unused.whl",
            "--requirements", "unused.lock", "--commit", "a" * 40,
            "--output", "unused-output", "--cache", "unused-cache",
            "--sign-identity", "Developer ID Application: Fixture",
        ], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments: --sign-identity", result.stderr)

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
