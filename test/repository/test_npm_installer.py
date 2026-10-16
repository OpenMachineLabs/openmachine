"""Check the npm installer with Node and verify safe package metadata."""

import json
from pathlib import Path
import shutil
import subprocess
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


if __name__ == "__main__":
    unittest.main()
