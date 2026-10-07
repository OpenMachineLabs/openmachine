"""Tests for bootstrap behavior at the component layer."""

import subprocess
import sys
import unittest
from test.support.runtime_cases import RuntimeCaseFixture


class RuntimeTests(RuntimeCaseFixture, unittest.TestCase):
    def test_disabled_integrations_are_not_imported_or_probed_by_composition(self):
        code = r'''import builtins
from pathlib import Path
import sys
from openmachine.config import Settings
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "integrations" or name.startswith(("integrations.github", "integrations.ssh")):
        raise AssertionError("Disabled integration imported: " + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
from openmachine.bootstrap import create_application
config = Settings.model_validate({"schema_version": 1, "machines": {"local": {"transport": "local"}}})
state = Path(sys.argv[1])
app = create_application(config, state)
assert app.get_capabilities()["integrations"] == []
assert app.find_tasks()["status"] == "uninitialized"
assert not state.exists()
assert not any(name in sys.modules for name in ("integrations.github", "integrations.github.connection", "integrations.ssh.client", "mcp"))
'''
        result = subprocess.run([sys.executable, "-I", "-c", code, str(self.state)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
