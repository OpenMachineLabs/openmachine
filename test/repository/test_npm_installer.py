"""Check the npm installer with Node and verify safe package metadata."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


class NpmInstallerTests(unittest.TestCase):
    def test_node_installer_contracts(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable in this development environment")
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [node, "--test", str(root / "test/repository/npm_installer.test.cjs")],
            cwd=root, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_npm_install_scripts_or_platform_expansion(self):
        package = json.loads(
            (Path(__file__).resolve().parents[2] / "npm/package.json").read_text())
        self.assertEqual(package["name"], "openmachine")
        self.assertEqual(package["os"], ["darwin"])
        self.assertEqual(package["cpu"], ["arm64"])
        for hook in ("preinstall", "install", "postinstall", "prepare"):
            self.assertNotIn(hook, package["scripts"])

    def test_registry_verification_waits_boundedly_without_republishing(self):
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github/workflows/npm-release.yml").read_text()
        step = workflow.split("      - name: Verify registry artifact\n", 1)[1]
        header, body = step.split("        run: |\n", 1)
        self.assertIn("        timeout-minutes: 10\n", header)
        lines = []
        for line in body.splitlines():
            if line and not line.startswith("          "):
                break
            lines.append(line[10:])
        script = "\n".join(lines)
        for mode, exit_code, attempts, sleeps in (
            ("ready", 0, 1, 0), ("delayed", 0, 3, 2),
            ("unavailable", 1, 20, 19), ("mismatch", 1, 1, 0), ("fatal", 1, 1, 0),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                (work / "dist").mkdir()
                (work / "dist/openmachine-1.2.3.tgz").write_bytes(b"published tarball")
                binary = work / "bin"
                binary.mkdir()
                npm = binary / "npm"
                npm.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
                    import os
                    from pathlib import Path
                    import sys
                    assert sys.argv[1:4] == ["view", "openmachine@1.2.3", "dist.shasum"]
                    assert {"--prefer-online", "--fetch-retries=0", "--fetch-timeout=10000"} <= set(sys.argv)
                    count = Path(os.environ["REGISTRY_TEST_COUNT"])
                    attempt = int(count.read_text()) + 1 if count.exists() else 1
                    count.write_text(str(attempt))
                    mode = os.environ["REGISTRY_TEST_MODE"]
                    if mode == "unavailable" or mode == "delayed" and attempt < 3:
                        print("npm error code E404", file=sys.stderr)
                        sys.exit(1)
                    if mode == "fatal":
                        print("npm error code E403", file=sys.stderr)
                        sys.exit(1)
                    print("0" * 40 if mode == "mismatch" else os.environ["REGISTRY_TEST_DIGEST"])
                    '''))
                npm.chmod(0o755)
                sleeper = binary / "sleep"
                sleeper.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$REGISTRY_TEST_SLEEPS"\n')
                sleeper.chmod(0o755)
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                        timeout=10, env={**os.environ,
                                            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                                            "RUNNER_TEMP": temporary, "VERSION": "1.2.3",
                                            "REGISTRY_TEST_MODE": mode,
                                            "REGISTRY_TEST_COUNT": str(work / "attempts"),
                                            "REGISTRY_TEST_SLEEPS": str(work / "sleeps"),
                                            "REGISTRY_TEST_DIGEST": hashlib.sha1(b"published tarball").hexdigest(),
                                        })
                self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                self.assertEqual(int((work / "attempts").read_text()), attempts)
                observed = (work / "sleeps").read_text().splitlines() if (work / "sleeps").exists() else []
                self.assertEqual(observed, ["15"] * sleeps)


if __name__ == "__main__":
    unittest.main()
