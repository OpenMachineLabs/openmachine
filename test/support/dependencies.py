"""Repository dependency assertions using only the standard library."""

import ast
from graphlib import CycleError, TopologicalSorter
from importlib.util import resolve_name
import sys


class DependencyFixture(object):
    def assert_import_contract(self, sources):
        # Bare names are exact interfaces; .* permits an owned implementation tree.
        boundaries = {
            "openmachine_access.foundation.*": ("openmachine_access.foundation.*",),
            "openmachine_access.config": ("pydantic", "openmachine_access.foundation.*"),
            # The package root holds only the version; the access launcher never imports service or cli.
            "openmachine_access": (),
            "openmachine_access.git": ("openmachine_access.foundation.sanitize",),
            "openmachine_access.linux": (),
            "openmachine_access.linux.environment": (),
            "openmachine_access.linux.sandbox": ("openmachine_access.foundation.sanitize",),
            "openmachine_access.macos": (),
            "openmachine_access.macos.seatbelt": ("openmachine_access.foundation.sanitize",),
            "openmachine_access.rules": ("openmachine_access.git", "openmachine_access.linux.sandbox",
                                         "openmachine_access.macos.seatbelt", "openmachine_access.config",
                                         "openmachine_access.foundation.*", "pydantic"),
            "openmachine_access.sessions": ("openmachine_access.linux.environment", "openmachine_access.linux.sandbox",
                                            "openmachine_access.macos.seatbelt", "openmachine_access.rules",
                                            "openmachine_access.foundation.*", "pydantic"),
            "openmachine_access.restricted": ("openmachine_access.linux.sandbox", "openmachine_access.macos.seatbelt",
                                              "openmachine_access.rules", "openmachine_access.sessions",
                                              "openmachine_access.foundation.*"),
            "openmachine_access.linux.sessions": ("openmachine_access.linux.sandbox", "openmachine_access.restricted",
                                                  "openmachine_access.rules", "openmachine_access.sessions",
                                                  "openmachine_access.foundation.*"),
            "openmachine_access.macos.sessions": ("openmachine_access.macos.seatbelt", "openmachine_access.restricted",
                                                  "openmachine_access.rules", "openmachine_access.sessions",
                                                  "openmachine_access.foundation.*"),
            "openmachine_access.__main__": ("openmachine_access", "openmachine_access.linux.sandbox",
                                            "openmachine_access.linux.sessions", "openmachine_access.macos.seatbelt",
                                            "openmachine_access.macos.sessions", "openmachine_access.restricted",
                                            "openmachine_access.rules", "openmachine_access.sessions", "openmachine_access.config",
                                            "openmachine_access.foundation.*", "pydantic"),
            "openmachine_access.service": (),
            "openmachine_access.service.state": ("openmachine_access.foundation.*",),
            "openmachine_access.service.processes": ("openmachine_access.foundation.*",),
            "openmachine_access.service.payload": ("openmachine_access.foundation.*",),
            "openmachine_access.service.install": ("openmachine_access.service.payload", "openmachine_access.service.processes",
                                            "openmachine_access.service.state", "openmachine_access.foundation.*"),
            "openmachine_access.service.transition": ("openmachine_access.service.state", "openmachine_access.foundation.*"),
            "openmachine_access.service.maintenance": ("openmachine_access.config", "openmachine_access.foundation.*", "openmachine_access.service.install",
                                                "openmachine_access.service.payload", "openmachine_access.service.state",
                                                "openmachine_access.service.transition"),
            "openmachine_access.cli": ("openmachine_access.service.maintenance",),
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
