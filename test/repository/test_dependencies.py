"""Tests for test_dependencies.py behavior at the repository layer."""

from pathlib import Path
import unittest
from test.support.dependencies import DependencyFixture


class RuntimeTests(DependencyFixture, unittest.TestCase):
    def test_runtime_import_boundaries_and_dependency_graph(self):
        sources = {}
        for root in ("openmachine", "integrations"):
            directory = Path(__file__).resolve().parents[2] / root
            self.assertTrue(directory.is_dir(), str(directory))
            for path in directory.rglob("*.py"):
                relative = path.relative_to(directory).with_suffix("")
                module = (root + "." + ".".join(relative.parts)).removesuffix(".__init__")
                sources[module] = (path.read_text(), path.name == "__init__.py")
        self.assert_import_contract(sources)

    def test_import_check_rejects_forbidden_edges_and_unowned_modules(self):
        cases = (
            ("openmachine.foundation.files", "import openmachine.tasks", "must not depend"),
            ("openmachine.foundation.files", "from ..tasks import read", "must not depend"),
            ("openmachine.foundation.files", "def read():\n    import openmachine.tasks", "must not depend"),
            ("openmachine.service", "from openmachine.execution.worker import worker", "must not depend"),
            ("openmachine.service.state", "from . import transition", "must not depend"),
            ("openmachine.service.runtime", "from . import install", "must not depend"),
            ("openmachine.service.processes", "from . import checks", "must not depend"),
            ("openmachine.service.processes", "import openmachine.service.runtime", "must not depend"),
            ("openmachine.service.transition", "import openmachine.tasks.database", "must not depend"),
            ("openmachine.service.maintenance", "from openmachine.bootstrap import create_application", "must not depend"),
            ("openmachine.tasks.index", "from . import store", "must not depend"),
            ("openmachine.tasks.rows", "import openmachine.tasks.store", "must not depend"),
            ("openmachine.tasks.rows", "import openmachine.tasks.migration", "must not depend"),
            ("openmachine.tasks.migration", "import openmachine.tasks.store", "must not depend"),
            ("openmachine.app.task_receipts", "import openmachine.app.task_admission", "must not depend"),
            ("openmachine.app.task_receipts", "import openmachine.app.task_observer", "must not depend"),
            ("integrations.github.connection", "from . import client", "must not depend"),
            ("openmachine.execution.records", "from openmachine.config import Settings", "must not depend"),
            ("openmachine.skills.catalog", "from openmachine.app import Application", "must not depend"),
            ("openmachine.skills.guidance", "import openmachine.server", "must not depend"),
            ("openmachine.mcp_protocol", "import openmachine.server", "must not depend"),
            ("openmachine.mcp_tools_connections", "import integrations.connections", "must not depend"),
            ("openmachine.mcp_tools_workspace", "import openmachine.app.task_admission", "must not depend"),
            ("integrations.connections", "import openmachine.mcp_protocol", "must not depend"),
            ("openmachine.new_owner", "import os", "explicit dependency owner"),
            ("openmachine.foundation.files", "from .sanitize import *", "wildcard import"),
            ("openmachine.foundation.files", "if TYPE_CHECKING:\n    pass\nelse:\n    import openmachine.tasks", "must not depend"),
        )
        for module, text, message in cases:
            with self.subTest(module=module, text=text), self.assertRaisesRegex(AssertionError, message):
                self.assert_import_contract({module: (text, False), "openmachine.tasks.store": ("", False),
                                             "integrations.github.client": ("", False)})

    def test_import_check_detects_cycles_through_reexports_and_local_imports(self):
        for final_edge in ("import openmachine.foundation", "def f():\n    from .. import foundation"):
            with self.subTest(final_edge=final_edge), self.assertRaisesRegex(AssertionError, "Runtime source import cycle"):
                self.assert_import_contract({
                    "openmachine.foundation": ("from .files import read", True),
                    "openmachine.foundation.files": ("from . import sanitize", False),
                    "openmachine.foundation.sanitize": (final_edge, False),
                })

    def test_import_check_accepts_type_only_dependencies_and_process_entries(self):
        self.assert_import_contract({
            "openmachine.foundation.files": ("import os\nfrom typing import TYPE_CHECKING\n"
                                            "if TYPE_CHECKING:\n    import openmachine.config\n"
                                            "import typing\nif typing.TYPE_CHECKING:\n    import mcp", False),
            "openmachine.cli": ("def main():\n    from integrations.ssh.bundle import build_bundle\n"
                                "    from openmachine.server import serve", False),
            "integrations.ssh.endpoint": ("from openmachine.execution.worker import worker", False),
        })


if __name__ == "__main__":
    unittest.main()
