"""The payload producer refuses signing options and unsafe input archives."""

import io
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest


class PayloadBuilderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-builder-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

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
