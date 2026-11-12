"""The payload producer builds its host's target and refuses signing options and unsafe input archives."""

from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from openmachine.service import payload

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/build_runtime_payload.py"


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

    def build(self, *target):
        """Run the producer up to its first download; returns the input URL it fetched, or its exit status."""
        wheel = self.root / "openmachine-0.2.8-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("openmachine-0.2.8.dist-info/METADATA", "Name: openmachine\nVersion: 0.2.8\n")
            archive.write(payload.__file__, "openmachine/service/payload.py")
        (self.root / "requirements.lock").write_text("pydantic==2.13.5\n")
        argv = [str(SCRIPT), "--wheel", str(wheel), "--requirements", str(self.root / "requirements.lock"), "--commit", "a" * 40,
                "--output", str(self.root / "output"), "--cache", str(self.root / "cache"), *target]
        with patch.object(sys, "argv", argv), patch("urllib.request.urlopen", side_effect=ConnectionError) as fetch, \
                redirect_stderr(io.StringIO()):
            try:
                runpy.run_path(str(SCRIPT), run_name="__main__")
            except SystemExit as exit:
                return exit.code
            except ConnectionError:
                return fetch.call_args.args[0]

    def test_target_selects_its_inputs_and_only_the_host_builds_it(self):
        inputs = json.loads(SCRIPT.with_name("runtime-inputs.json").read_text())
        self.assertEqual(set(inputs), set(payload.TARGETS))
        host = payload.host_target()
        self.assertEqual(self.build(), inputs[host]["python"]["url"])
        (self.root / "output").rmdir()
        self.assertEqual(self.build("--target", host), inputs[host]["python"]["url"])
        (self.root / "output").rmdir()
        for other in set(payload.TARGETS) - {host}:
            with self.subTest(target=other):
                self.assertEqual(self.build("--target", other), 2)
                self.assertFalse((self.root / "output").exists())

    def test_linux_payloads_carry_a_static_bubblewrap_built_from_a_pinned_release(self):
        inputs = json.loads(SCRIPT.with_name("runtime-inputs.json").read_text())
        self.assertNotIn("bubblewrap", inputs["macos-arm64"])
        self.assertEqual(inputs["linux-x86_64"]["bubblewrap"], inputs["linux-aarch64"]["bubblewrap"])
        pinned = inputs["linux-x86_64"]["bubblewrap"]
        self.assertEqual(pinned["url"], "https://github.com/containers/bubblewrap/releases/download/"
                         f"v{pinned['version']}/bubblewrap-{pinned['version']}.tar.xz")
        self.assertRegex(pinned["sha256"], r"^[0-9a-f]{64}$")
        # The build, with the compiler's part played here: the release's source in, bwrap and its licence out.
        source = self.root / "bubblewrap-9.9"
        source.mkdir()
        (source / "COPYING").write_text("LGPL\n")
        release = self.root / "release.tar.xz"
        with tarfile.open(release, "w:xz") as archive:
            archive.add(source, "bubblewrap-9.9")
        item = {"version": "9.9", "url": "https://example.invalid/unused", "sha256": hashlib.sha256(release.read_bytes()).hexdigest()}
        cache, output, ran, kinds = self.root / "cache", self.root / "output", [], [1, 6]  # PT_LOAD, PT_PHDR
        for path in (cache, output):
            path.mkdir()
        release.rename(cache / (item["sha256"] + ".tar.xz"))

        def run(argv, env):
            ran.append(argv)
            if argv[0] == "ninja":
                self.assertEqual((Path(argv[2]).parent / "bubblewrap-9.9/COPYING").read_text(), "LGPL\n")
                Path(argv[2]).mkdir()
                (Path(argv[2]) / "bwrap").write_bytes(  # An ELF header's program header table, as far as it is read.
                    bytes(32) + struct.pack("<Q", 64) + bytes(14) + struct.pack("<HH", 56, len(kinds)) + bytes(6)
                    + b"".join(struct.pack("<I", kind) + bytes(52) for kind in kinds))

        builder = runpy.run_path(str(SCRIPT))
        builder["bubblewrap"].__globals__["run"] = run
        builder["bubblewrap"](item, cache, output)
        self.assertEqual([argv[0] for argv in ran], ["meson", "ninja", str(output / "bwrap")])
        self.assertIn("-Dc_link_args=-static", ran[0])
        self.assertEqual((output / "bwrap").stat().st_mode & 0o777, 0o755)
        self.assertEqual((output / "bwrap.COPYING").read_text(), "LGPL\n")
        self.assertEqual(payload.inventory(output)["bwrap"]["executable"], True)
        kinds.append(3)  # PT_INTERP: dynamically linked, which an old machine's loader may not run.
        with self.assertRaisesRegex(ValueError, "dynamic loader"):
            builder["bubblewrap"](item, cache, self.root)
        self.assertFalse((self.root / "bwrap").exists())
        # A release that differs from its pin builds nothing.
        (cache / (item["sha256"] + ".tar.xz")).write_bytes(b"other")
        with self.assertRaises(ValueError):
            builder["bubblewrap"](item, cache, self.root)
        self.assertFalse((self.root / "bwrap").exists())

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
