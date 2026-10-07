"""Tests for service behavior at the component layer."""

import io
from pathlib import Path
import runpy
import subprocess
import tarfile
import unittest
from test.support.payload_cases import PayloadCaseFixture


class PayloadTests(PayloadCaseFixture, unittest.TestCase):
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
