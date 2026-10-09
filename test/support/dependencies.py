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
            "integrations": (),
            "openmachine.foundation.*": ("openmachine.foundation.*",),
            "openmachine.execution.*": ("openmachine", "openmachine.execution.*",
                                        "openmachine.foundation.*", "openmachine.workspace.git", "openmachine.workspace.worktrees"),
            "openmachine.workspace.*": ("openmachine", "openmachine.workspace.*", "openmachine.foundation.*"),
            "openmachine.tasks": ("openmachine.tasks.store", "openmachine.tasks.index", "openmachine.tasks.records",
                                  "openmachine.tasks.database", "openmachine.tasks.migration", "openmachine.tasks.rows"),
            "openmachine.tasks.store": ("openmachine.tasks.index", "openmachine.tasks.records", "openmachine.tasks.effects",
                                        "openmachine.tasks.database", "openmachine.tasks.rows", "openmachine.foundation.*"),
            "openmachine.tasks.database": ("openmachine.tasks.index", "openmachine.tasks.records", "openmachine.foundation.*"),
            "openmachine.tasks.migration": ("openmachine.tasks.database", "openmachine.tasks.index",
                                            "openmachine.tasks.records", "openmachine.tasks.rows", "openmachine.foundation.*"),
            "openmachine.tasks.rows": ("openmachine.tasks.database", "openmachine.tasks.effects",
                                       "openmachine.tasks.records", "openmachine.foundation.*"),
            "openmachine.tasks.index": ("openmachine.tasks.records", "openmachine.foundation.*"),
            "openmachine.tasks.records": ("pydantic", "openmachine.foundation.*"),
            "openmachine.tasks.effects": ("openmachine.tasks.records",),
            "openmachine.tasks.transitions": ("openmachine.tasks.records", "openmachine.tasks.effects", "openmachine.foundation.*"),
            "openmachine.config": ("pydantic", "openmachine.foundation.*"),
            "openmachine.skills.*": ("openmachine.skills.*", "openmachine.foundation.*", "yaml"),
            "openmachine.service": ("openmachine.service.state", "openmachine.service.checks",
                                    "openmachine.service.install", "openmachine.service.runtime", "openmachine.service.transition"),
            "openmachine.service.__main__": ("openmachine.service.checks", "openmachine.foundation.*"),
            "openmachine.service.state": ("openmachine.config", "openmachine.foundation.*"),
            "openmachine.service.checks": ("openmachine", "openmachine.config", "openmachine.execution",
                                           "openmachine.tasks", "openmachine.workspace", "openmachine.service.state",
                                           "openmachine.foundation.*", "httpx2", "mcp.*",
                                           "integrations.ssh.recovery", "integrations.ssh.client", "integrations.connections"),
            "openmachine.service.install": ("openmachine.config", "openmachine.service.checks",
                                            "openmachine.service.state", "openmachine.service.payload", "openmachine.foundation.*"),
            "openmachine.service.maintenance": ("openmachine", "openmachine.config", "openmachine.foundation.*",
                                                "openmachine.service.checks", "openmachine.service.install",
                                                "openmachine.service.payload", "openmachine.service.runtime",
                                                "openmachine.service.state", "openmachine.service.transition", "integrations.ssh.client", "pydantic"),
            "openmachine.service.payload": ("openmachine.foundation.*",),
            "openmachine.service.runtime": ("openmachine.config", "openmachine.service.checks",
                                            "openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.service.transition": ("openmachine.config", "openmachine.tasks", "openmachine.service.checks",
                                               "openmachine.service.install", "openmachine.service.runtime",
                                               "openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.app": ("openmachine", "openmachine.app.machines", "openmachine.app.task_admission", "openmachine.app.task_attention", "openmachine.app.task_observer", "openmachine.app.task_receipts", "openmachine.config",
                                "openmachine.execution", "openmachine.workspace", "openmachine.tasks",
                                "openmachine.skills", "openmachine.service", "openmachine.foundation.*"),
            "openmachine.app.machines": ("openmachine.config", "openmachine.execution", "openmachine.workspace",
                                         "openmachine.tasks", "openmachine.foundation.*"),
            "openmachine.app.task_admission": ("openmachine.execution", "openmachine.tasks", "openmachine.app.task_attention",
                                               "openmachine.app.task_observation", "openmachine.app.task_receipts",
                                               "openmachine.tasks.records", "openmachine.tasks.transitions", "openmachine.tasks.effects", "openmachine.foundation.*"),
            "openmachine.app.task_attention": ("openmachine.tasks.records",),
            "openmachine.app.task_observer": ("openmachine.tasks", "openmachine.tasks.records", "openmachine.app.task_observation", "openmachine.app.task_receipts", "openmachine.foundation.*"),
            "openmachine.app.task_observation": ("openmachine.tasks", "openmachine.tasks.effects", "openmachine.tasks.records", "openmachine.tasks.transitions", "openmachine.foundation.*"),
            "openmachine.app.task_receipts": ("openmachine.tasks.records", "openmachine.foundation.*"),
            "openmachine.bootstrap": ("openmachine.app", "openmachine.app.machines", "openmachine.config",
                                      "integrations.github", "integrations.github.connection", "integrations.github.auth", "integrations.ssh.client", "integrations.ssh.recovery", "integrations.connections", "integrations.codex"),
            "openmachine.server": ("openmachine", "openmachine.app", "openmachine.bootstrap", "openmachine.config",
                                   "openmachine.service", "openmachine.skills", "openmachine.tasks", "openmachine.foundation.*", "mcp.*", "jsonschema", "pydantic", "uvicorn"),
            # The CLI owns serve/worker-bundle dispatch, not execution internals.
            "openmachine.cli": ("openmachine", "openmachine.config", "openmachine.foundation.*",
                                "openmachine.bootstrap", "openmachine.server", "openmachine.service", "openmachine.service.maintenance",
                                "integrations.ssh.bundle"),
            "integrations.codex": ("pydantic", "openmachine.execution", "openmachine.foundation.*"),
            "integrations.connections": ("pydantic", "integrations.credentials", "integrations.github.connection", "openmachine.config", "openmachine.foundation.*"),
            "integrations.credentials": ("pydantic", "openmachine.foundation.*"),
            "integrations.github.auth": ("httpx2", "integrations.credentials", "openmachine.foundation.*"),
            "integrations.github": ("integrations.github.operations",),
            "integrations.github.connection": ("openmachine.config", "openmachine.foundation.*"),
            "integrations.github.client": ("integrations.github.connection", "openmachine.foundation.*"),
            "integrations.github.evidence": ("integrations.github.client", "integrations.github.connection",
                                             "openmachine.foundation.*"),
            "integrations.github.operations": ("integrations.github.client", "integrations.github.connection",
                                               "integrations.github.evidence", "openmachine.config", "openmachine.foundation.*"),
            "integrations.ssh": (),
            "integrations.ssh.client": ("openmachine", "integrations.ssh", "openmachine.foundation.*"),
            "integrations.ssh.recovery": ("openmachine.foundation.*",),
            "integrations.ssh.bundle": ("openmachine", "integrations.ssh", "openmachine.foundation.*"),
            # The archive's worker process entry is the sole cross-domain worker caller.
            "integrations.ssh.endpoint": ("openmachine", "openmachine.execution", "openmachine.execution.worker",
                                          "openmachine.workspace", "openmachine.foundation.*",
                                          "integrations.ssh", "integrations.ssh.bundle"),
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
            # Type-only annotations do not pull controller dependencies onto SSH targets.
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
