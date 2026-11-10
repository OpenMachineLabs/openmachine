"""Repository dependency assertions using only the standard library."""

import ast
from graphlib import CycleError, TopologicalSorter
from importlib.util import resolve_name
import sys


class DependencyFixture(object):
    def assert_import_contract(self, sources):
        # Bare names are exact interfaces; .* permits an owned implementation tree.
        boundaries = {
            "openmachine": (),
            "openmachine.foundation.*": ("openmachine.foundation.*",),
            "openmachine.config": ("pydantic", "openmachine.foundation.*"),
            # Spec: access imports only openmachine, openmachine.access, openmachine.foundation and openmachine.config.
            "openmachine.access": (),
            "openmachine.access.git": ("openmachine.foundation.sanitize",),
            "openmachine.access.macos": (),
            "openmachine.access.macos.seatbelt": ("openmachine.foundation.sanitize",),
            "openmachine.access.catalog": ("openmachine.access.git", "openmachine.foundation.*"),
            "openmachine.access.rules": ("openmachine.access.catalog", "openmachine.access.git", "openmachine.access.macos.seatbelt",
                                         "openmachine.config", "openmachine.foundation.*", "pydantic"),
            "openmachine.access.sessions": ("openmachine.access.macos.seatbelt", "openmachine.foundation.*", "pydantic"),
            "openmachine.access.macos.sessions": ("openmachine.access.macos.seatbelt", "openmachine.access.rules",
                                                  "openmachine.access.sessions", "openmachine.config", "openmachine.foundation.*"),
            "openmachine.access.__main__": ("openmachine", "openmachine.access.catalog", "openmachine.access.macos.seatbelt",
                                            "openmachine.access.macos.sessions", "openmachine.access.rules",
                                            "openmachine.access.sessions", "openmachine.config", "openmachine.foundation.*",
                                            "pydantic"),
            "openmachine.service": (),
            "openmachine.service.state": ("openmachine.foundation.*",),
            "openmachine.service.processes": ("openmachine.foundation.*",),
            "openmachine.service.payload": ("openmachine.foundation.*",),
            "openmachine.service.install": ("openmachine.service.payload", "openmachine.service.processes",
                                            "openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.service.transition": ("openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.service.maintenance": ("openmachine.config", "openmachine.foundation.*", "openmachine.service.install",
                                                "openmachine.service.payload", "openmachine.service.state",
                                                "openmachine.service.transition"),
            "openmachine.cli": ("openmachine.service.maintenance",),
        }

        def matches(module, pattern):
            return module == pattern or (pattern.endswith(".*") and
                                         (module == pattern[:-2] or module.startswith(pattern[:-1])))

        graph = {module: set() for module in sources}
        for module, (text, is_package) in sources.items():
            owners = [owner for owner in boundaries if matches(module, owner)]
            self.assertEqual(len(owners), 1, f"{module} needs one explicit dependency owner")
            package = module if is_package else module.rpartition(".")[0]
            tree = ast.parse(text)
            # Type-only annotations do not pull runtime dependencies into unrelated owners.
            for node in ast.walk(tree):
                if isinstance(node, ast.If) and ast.unparse(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
                    node.body = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn("*", [item.name for item in node.names], f"{module}:{node.lineno}: wildcard import")
                    base = resolve_name("." * node.level + (node.module or ""), package)
                    imports = [base + "." + item.name if base + "." + item.name in sources else base for item in node.names]
                else:
                    continue
                for dependency in imports:
                    allowed = dependency.split(".")[0] in sys.stdlib_module_names or any(
                        matches(dependency, pattern) for pattern in boundaries[owners[0]])
                    self.assertTrue(allowed, f"{module}:{node.lineno} must not depend on {dependency}")
                    if dependency in sources:
                        graph[module].add(dependency)
        try:
            TopologicalSorter(graph).prepare()
        except CycleError as error:
            self.fail("Runtime source import cycle: " + " -> ".join(error.args[1]))
