"""Tests for test_dependencies.py behavior at the repository layer."""

from pathlib import Path
import unittest
from test.support.dependencies import DependencyFixture


class RuntimeTests(DependencyFixture, unittest.TestCase):
    def test_runtime_import_boundaries_and_dependency_graph(self):
        sources = {}
        directory = Path(__file__).resolve().parents[2] / "openmachine"
        for path in directory.rglob("*.py"):
            relative = path.relative_to(directory).with_suffix("")
            module = ("openmachine." + ".".join(relative.parts)).removesuffix(".__init__")
            sources[module] = (path.read_text(), path.name == "__init__.py")
        self.assert_import_contract(sources)

    def test_import_check_rejects_forbidden_edges_and_unowned_modules(self):
        cases = (
            ("openmachine.access.git", "import openmachine.access.rules", "must not depend"),
            ("openmachine.access.rules", "from . import sessions", "must not depend"),
            ("openmachine.access.sessions", "import openmachine.service.state", "must not depend"),
            ("openmachine.access.__main__", "import openmachine.service", "must not depend"),
            ("openmachine.access.rules", "def compile():\n    import openmachine.cli", "must not depend"),
            ("openmachine.foundation.state", "import openmachine.config", "must not depend"),
            ("openmachine.foundation.state", "from ..access import git", "must not depend"),
            ("openmachine.config", "import openmachine.access", "must not depend"),
            ("openmachine.service.state", "from . import transition", "must not depend"),
            ("openmachine.service.processes", "from . import install", "must not depend"),
            ("openmachine.service.install", "import openmachine.access.sessions", "must not depend"),
            ("openmachine.service.maintenance", "import openmachine.access", "must not depend"),
            ("openmachine.new_owner", "import os", "explicit dependency owner"),
            ("openmachine.foundation.state", "from .sanitize import *", "wildcard import"),
            ("openmachine.foundation.state", "if TYPE_CHECKING:\n    pass\nelse:\n    import openmachine.config", "must not depend"),
        )
        for module, text, message in cases:
            with self.subTest(module=module, text=text), self.assertRaisesRegex(AssertionError, message):
                self.assert_import_contract({module: (text, False), "openmachine.service.payload": ("", False)})

    def test_import_check_detects_cycles_through_reexports_and_local_imports(self):
        for final_edge in ("import openmachine.foundation", "def f():\n    from .. import foundation"):
            with self.subTest(final_edge=final_edge), self.assertRaisesRegex(AssertionError, "Runtime source import cycle"):
                self.assert_import_contract({
                    "openmachine.foundation": ("from .state import read", True),
                    "openmachine.foundation.state": ("from . import sanitize", False),
                    "openmachine.foundation.sanitize": (final_edge, False),
                })

    def test_import_check_accepts_type_only_dependencies(self):
        self.assert_import_contract({
            "openmachine.access.git": ("import os\nfrom typing import TYPE_CHECKING\n"
                                           "if TYPE_CHECKING:\n    from openmachine.config import Settings\n"
                                           "import typing\nif typing.TYPE_CHECKING:\n    import openmachine.service", False),
        })


if __name__ == "__main__":
    unittest.main()
