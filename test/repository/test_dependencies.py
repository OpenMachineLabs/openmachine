"""Tests for test_dependencies.py behavior at the repository layer."""

from pathlib import Path
import re
import unittest
from test.support.dependencies import DependencyFixture


class RuntimeTests(DependencyFixture, unittest.TestCase):
    def test_runtime_import_boundaries_and_dependency_graph(self):
        sources = {}
        directory = Path(__file__).resolve().parents[2] / "access"
        for path in (directory / "openmachine_access").rglob("*.py"):
            module = ".".join(path.relative_to(directory).with_suffix("").parts).removesuffix(".__init__")
            sources[module] = (path.read_text(), path.name == "__init__.py")
        self.assert_import_contract(sources)

    def test_import_check_rejects_forbidden_edges_and_unowned_modules(self):
        cases = (
            ("openmachine_access.git", "import openmachine_access.rules", "must not depend on openmachine_access.rules"),
            ("openmachine_access.rules", "from . import sessions", "must not depend on openmachine_access.sessions"),
            ("openmachine_access.sessions", "import openmachine_access.service.state", "must not depend on openmachine_access.service.state"),
            ("openmachine_access.__main__", "import openmachine_access.service", "must not depend on openmachine_access.service"),
            ("openmachine_access.rules", "def compile():\n    import openmachine_access.cli", "must not depend on openmachine_access.cli"),
            ("openmachine_access.foundation.state", "import openmachine_access.config", "must not depend on openmachine_access.config"),
            ("openmachine_access.foundation.state", "from .. import git", "must not depend on openmachine_access.git"),
            ("openmachine_access.config", "import openmachine_access.sessions", "must not depend on openmachine_access.sessions"),
            ("openmachine_access.service.state", "from . import transition", "must not depend on openmachine_access.service.transition"),
            ("openmachine_access.service.processes", "from . import install", "must not depend on openmachine_access.service.install"),
            ("openmachine_access.service.install", "import openmachine_access.sessions", "must not depend on openmachine_access.sessions"),
            ("openmachine_access.service.maintenance", "from openmachine_access import restricted", "must not depend on openmachine_access.restricted"),
            ("openmachine_access.new_owner", "import os", "explicit dependency owner"),
            ("openmachine_access.foundation.state", "from .sanitize import *", "wildcard import"),
            ("openmachine_access.foundation.state", "if TYPE_CHECKING:\n    pass\nelse:\n    import openmachine_access.config", "must not depend on openmachine_access.config"),
        )
        for module, text, message in cases:
            with self.subTest(module=module, text=text), self.assertRaisesRegex(AssertionError, re.escape(message) + "$"):
                # Stubs make a named sibling resolve to itself rather than to the package root.
                stubs = {name: ("", False) for name in (
                    "openmachine_access.git", "openmachine_access.restricted", "openmachine_access.sessions",
                    "openmachine_access.service.install", "openmachine_access.service.payload",
                    "openmachine_access.service.transition")}
                self.assert_import_contract(stubs | {module: (text, False)})

    def test_import_check_detects_cycles_through_reexports_and_local_imports(self):
        for final_edge in ("import openmachine_access.foundation", "def f():\n    from .. import foundation"):
            with self.subTest(final_edge=final_edge), self.assertRaisesRegex(AssertionError, "Runtime source import cycle"):
                self.assert_import_contract({
                    "openmachine_access.foundation": ("from .state import read", True),
                    "openmachine_access.foundation.state": ("from . import sanitize", False),
                    "openmachine_access.foundation.sanitize": (final_edge, False),
                })

    def test_import_check_accepts_type_only_dependencies(self):
        self.assert_import_contract({
            "openmachine_access.git": ("import os\nfrom typing import TYPE_CHECKING\n"
                                           "if TYPE_CHECKING:\n    from openmachine_access.config import DEFAULT_STATE\n"
                                           "import typing\nif typing.TYPE_CHECKING:\n    import openmachine_access.service", False),
        })


if __name__ == "__main__":
    unittest.main()
