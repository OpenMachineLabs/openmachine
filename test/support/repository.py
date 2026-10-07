"""Small disposable Git repositories for test-selection and premerge behavior."""

import copy
import json
from pathlib import Path
import subprocess
import tempfile


class RepositoryFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-test-selection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q")
        self.files = {
            "README.md": "# Fixture\n",
            "openmachine/github.py": "VALUE = 1\n",
            "openmachine/execution.py": "VALUE = 1\n",
            "openmachine/server.py": "VALUE = 1\n",
            "test/unit/github/test_values.py": "import unittest\n\nclass Values(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 1)\n",
            "test/component/github/test_calls.py": "import unittest\n\nclass Calls(unittest.TestCase):\n    def test_call(self):\n        self.assertEqual(1, 1)\n",
            "test/resilience/execution/test_process.py": "import unittest\n\nclass Process(unittest.TestCase):\n    def test_process(self):\n        self.assertEqual(1, 1)\n",
            "test/contract/mcp/github/test_dispatch.py": "import unittest\n\nclass Dispatch(unittest.TestCase):\n    def test_dispatch(self):\n        self.assertEqual(1, 1)\n",
            "test/repository/test_rules.py": "import unittest\n\nclass Rules(unittest.TestCase):\n    def test_rules(self):\n        self.assertEqual(1, 1)\n",
            "test/support/wire.py": '"""Fixture helper."""\n',
        }
        for path, content in self.files.items():
            self.write(path, content)
        self.manifest = {
            "format": 1,
            "modules": {
                "github": {
                    "sources": ["openmachine/github.py"],
                    "tests": ["test/unit/github/test_values.py", "test/component/github/test_calls.py"],
                    "boundary_tests": ["test/contract/mcp/github/test_dispatch.py"],
                },
                "execution": {
                    "sources": ["openmachine/execution.py"],
                    "tests": ["test/resilience/execution/test_process.py"], "boundary_tests": [],
                },
                "server": {
                    "sources": ["openmachine/server.py"],
                    "tests": ["test/contract/mcp/github/test_dispatch.py"], "boundary_tests": [],
                },
                "repository": {
                    "sources": [], "tests": ["test/repository/test_rules.py"], "boundary_tests": [],
                },
            },
            "support": {"test/support/wire.py": ["test/contract/mcp/github/test_dispatch.py"]},
        }
        self.save_manifest()
        self.commit("test(fixture): establish a complete ownership baseline")
        self.base = self.git("rev-parse", "HEAD")
        self.baseline = copy.deepcopy(self.manifest)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def save_manifest(self):
        self.write("test/ownership.json", json.dumps(self.manifest, indent=2) + "\n")

    def commit(self, message="test(fixture): change the candidate"):
        self.git("add", "-A")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-qm", message)

    def product_tests(self, plan):
        return set(path for path in plan["tests"] if not path.startswith("test/repository/"))
