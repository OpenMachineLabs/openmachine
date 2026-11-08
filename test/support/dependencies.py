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
            "openmachine.chat": (),
            "openmachine.access": (),
            "openmachine.execution": (),
            "openmachine.chat.execution": ("openmachine.chat.execution.api", "openmachine.chat.execution.observation",
                                      "openmachine.chat.execution.control", "openmachine.chat.execution.logs", "openmachine.chat.execution.records", "openmachine.chat.execution.environment"),
            "openmachine.access.macos": ("openmachine.foundation.sanitize",),
            "openmachine.access.sessions": ("openmachine.access.macos", "openmachine.access.rules", "openmachine.config",
                                            "openmachine.foundation.*", "pydantic"),
            "openmachine.access.__main__": ("openmachine.access.sessions", "openmachine.config"),
            "openmachine.chat.execution.environment": ("openmachine.chat.workspace", "openmachine.chat.execution.resources", "openmachine.foundation.*"),
            "openmachine.chat.execution.resources": ("openmachine.foundation.sanitize",),
            "openmachine.execution.__main__": ("openmachine.chat.execution.worker",),
            "openmachine.chat.execution.api": ("openmachine", "openmachine.chat.execution.observation", "openmachine.chat.execution.control",
                                          "openmachine.chat.execution.process", "openmachine.chat.execution.records", "openmachine.access.macos",
                                          "openmachine.foundation.*", "openmachine.chat.workspace.git", "openmachine.chat.workspace.worktrees"),
            "openmachine.chat.execution.observation": ("openmachine.chat.execution.control", "openmachine.chat.execution.logs",
                                                  "openmachine.chat.execution.records", "openmachine.foundation.*"),
            "openmachine.chat.execution.records": ("openmachine.foundation.sanitize", "openmachine.chat.workspace.worktrees"),
            "openmachine.chat.execution.control": ("openmachine.chat.execution.records", "openmachine.foundation.*"),
            "openmachine.chat.execution.logs": ("openmachine.foundation.*",),
            "openmachine.chat.execution.process": ("openmachine.foundation.sanitize", "openmachine.access.macos"),
            "openmachine.chat.execution.worker": ("openmachine.chat.execution.control", "openmachine.chat.execution.logs",
                                             "openmachine.chat.execution.process", "openmachine.chat.execution.records", "openmachine.access.macos",
                                             "openmachine.foundation.*", "openmachine.chat.workspace.git", "openmachine.chat.workspace.worktrees"),
            "openmachine.chat.projects": ("openmachine.chat.projects.catalog",),
            "openmachine.chat.projects.catalog": ("openmachine.foundation.*", "openmachine.chat.workspace.files", "openmachine.chat.workspace.git"),
            "openmachine.chat.workspace": ("openmachine.chat.workspace.files", "openmachine.chat.workspace.change_records",
                                      "openmachine.chat.workspace.change_schema", "openmachine.chat.workspace.changes",
                                      "openmachine.chat.workspace.git", "openmachine.chat.workspace.inspect",
                                      "openmachine.chat.workspace.patch", "openmachine.chat.workspace.worktrees", "openmachine.chat.workspace.signals"),
            "openmachine.chat.workspace.change_schema": ("openmachine", "openmachine.foundation.files", "openmachine.foundation.sanitize",
                                                    "openmachine.chat.workspace.files", "openmachine.chat.workspace.patch"),
            "openmachine.chat.workspace.change_records": ("openmachine.chat.workspace.change_schema", "openmachine.foundation.*"),
            "openmachine.chat.workspace.changes": ("openmachine.chat.workspace.change_records", "openmachine.chat.workspace.change_schema",
                                              "openmachine.chat.workspace.preparation", "openmachine.chat.workspace.files",
                                              "openmachine.chat.workspace.git", "openmachine.chat.workspace.worktrees", "openmachine.foundation.*"),
            "openmachine.chat.workspace.preparation": ("openmachine.chat.workspace.change_schema", "openmachine.chat.workspace.files",
                                                  "openmachine.chat.workspace.patch", "openmachine.foundation.files", "openmachine.foundation.sanitize"),
            "openmachine.chat.workspace.signals": ("openmachine.chat.workspace.files", "openmachine.foundation.*"),
            "openmachine.chat.workspace.files": ("openmachine.chat.workspace.git", "openmachine.foundation.*"),
            "openmachine.chat.workspace.git": ("openmachine.foundation.sanitize",),
            "openmachine.chat.workspace.inspect": ("openmachine.chat.workspace.files", "openmachine.chat.workspace.git",
                                              "openmachine.chat.workspace.worktrees", "openmachine.foundation.*"),
            "openmachine.chat.workspace.patch": ("openmachine.foundation.files", "openmachine.foundation.sanitize"),
            "openmachine.chat.workspace.worktrees": ("openmachine.chat.workspace.files", "openmachine.chat.workspace.git", "openmachine.foundation.*"),
            "openmachine.chat.tasks": ("openmachine.chat.tasks.store", "openmachine.chat.tasks.index", "openmachine.chat.tasks.records",
                                  "openmachine.chat.tasks.database", "openmachine.chat.tasks.migration", "openmachine.chat.tasks.rows"),
            "openmachine.chat.tasks.store": ("openmachine.chat.tasks.index", "openmachine.chat.tasks.records", "openmachine.chat.tasks.effects",
                                        "openmachine.chat.tasks.database", "openmachine.chat.tasks.rows", "openmachine.foundation.*"),
            "openmachine.chat.tasks.database": ("openmachine.chat.tasks.index", "openmachine.chat.tasks.records", "openmachine.foundation.*"),
            "openmachine.chat.tasks.migration": ("openmachine.chat.tasks.database", "openmachine.chat.tasks.index",
                                            "openmachine.chat.tasks.records", "openmachine.chat.tasks.rows", "openmachine.foundation.*"),
            "openmachine.chat.tasks.rows": ("openmachine.chat.tasks.database", "openmachine.chat.tasks.effects",
                                       "openmachine.chat.tasks.records", "openmachine.foundation.*"),
            "openmachine.chat.tasks.index": ("openmachine.chat.tasks.records", "openmachine.foundation.*"),
            "openmachine.chat.tasks.records": ("pydantic", "openmachine.foundation.*"),
            "openmachine.chat.tasks.effects": ("openmachine.chat.tasks.records",),
            "openmachine.chat.tasks.transitions": ("openmachine.chat.tasks.records", "openmachine.chat.tasks.effects", "openmachine.foundation.*"),
            "openmachine.config": ("pydantic", "openmachine.foundation.*"),
            "openmachine.skills.*": ("openmachine.skills.*", "openmachine.foundation.*", "yaml"),
            "openmachine.service": ("openmachine.service.state", "openmachine.service.checks",
                                    "openmachine.service.install", "openmachine.service.runtime", "openmachine.service.transition"),
            "openmachine.service.__main__": ("openmachine.service.checks", "openmachine.foundation.*"),
            "openmachine.service.state": ("openmachine.config", "openmachine.foundation.*"),
            "openmachine.service.checks": ("openmachine", "openmachine.config", "openmachine.chat.execution",
                                           "openmachine.chat.tasks", "openmachine.chat.workspace", "openmachine.service.processes", "openmachine.service.state",
                                           "openmachine.foundation.*", "httpx2", "mcp.*", "integrations.connection_state"),
            "openmachine.service.processes": ("openmachine.foundation.*",),
            "openmachine.service.install": ("openmachine.config", "openmachine.service.checks",
                                            "openmachine.service.state", "openmachine.service.payload", "openmachine.service.processes", "openmachine.foundation.*"),
            "openmachine.service.maintenance": ("openmachine", "openmachine.config", "openmachine.foundation.*",
                                                "openmachine.service.checks", "openmachine.service.install",
                                                "openmachine.service.payload", "openmachine.service.processes", "openmachine.service.runtime",
                                                "openmachine.service.state", "openmachine.service.transition", "pydantic"),
            "openmachine.service.updates": ("openmachine", "openmachine.foundation.*", "httpx2"),
            "openmachine.service.payload": ("openmachine.foundation.*",),
            "openmachine.service.runtime": ("openmachine.config", "openmachine.service.checks",
                                            "openmachine.service.processes", "openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.service.transition": ("openmachine.config", "openmachine.chat.tasks", "openmachine.service.checks",
                                               "openmachine.service.install", "openmachine.service.runtime",
                                               "openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.chat.app": ("openmachine.chat.app.environment", "openmachine", "openmachine.chat.app.machines", "openmachine.access.rules", "openmachine.chat.app.task_admission", "openmachine.chat.app.task_attention", "openmachine.chat.app.task_observer", "openmachine.chat.app.task_receipts", "openmachine.config",
                                "openmachine.chat.execution", "openmachine.chat.workspace", "openmachine.chat.projects", "openmachine.chat.tasks",
                                "openmachine.skills", "openmachine.service", "openmachine.foundation.*"),
            "openmachine.chat.app.environment": ("openmachine.foundation.sanitize", "openmachine.chat.execution"),
            "openmachine.access.rules": ("openmachine", "openmachine.config", "openmachine.access.macos", "openmachine.chat.projects",
                                      "openmachine.chat.workspace", "openmachine.foundation.*", "pydantic"),
            "openmachine.chat.app.machines": ("openmachine.config", "openmachine.chat.execution", "openmachine.chat.workspace",
                                         "openmachine.chat.tasks", "openmachine.foundation.*"),
            "openmachine.chat.app.task_admission": ("openmachine.chat.execution", "openmachine.chat.tasks", "openmachine.chat.workspace", "openmachine.chat.app.task_attention",
                                               "openmachine.chat.app.task_observation", "openmachine.chat.app.task_receipts",
                                               "openmachine.chat.tasks.records", "openmachine.chat.tasks.transitions", "openmachine.chat.tasks.effects", "openmachine.foundation.*"),
            "openmachine.chat.app.task_attention": ("openmachine.chat.tasks.records",),
            "openmachine.chat.app.task_observer": ("openmachine.chat.tasks", "openmachine.chat.tasks.records", "openmachine.chat.app.task_observation", "openmachine.chat.app.task_receipts", "openmachine.foundation.*"),
            "openmachine.chat.app.task_observation": ("openmachine.chat.tasks", "openmachine.chat.tasks.effects", "openmachine.chat.tasks.records", "openmachine.chat.tasks.transitions", "openmachine.foundation.*"),
            "openmachine.chat.app.task_receipts": ("openmachine.chat.tasks.records", "openmachine.foundation.*"),
            "openmachine.chat.bootstrap": ("openmachine.chat.app", "openmachine.chat.app.machines", "openmachine.config",
                                      "integrations.github", "integrations.github.connection", "integrations.github.auth", "integrations.connections", "integrations.codex"),
            "openmachine.chat.mcp": (),
            "openmachine.chat.mcp.protocol": ("openmachine.chat.app", "openmachine.chat.tasks", "openmachine.foundation.*",
                                         "mcp.*", "pydantic"),
            "openmachine.chat.mcp.connections": ("openmachine.chat.tasks", "openmachine.chat.mcp.protocol", "mcp.*", "pydantic"),
            "openmachine.chat.mcp.projects": ("openmachine.chat.mcp.protocol", "mcp.*", "pydantic"),
            "openmachine.chat.mcp.environment": ("openmachine.chat.mcp.protocol", "mcp.*", "pydantic"),
            "openmachine.chat.mcp.workspace": ("openmachine.chat.tasks", "openmachine.chat.mcp.protocol", "mcp.*", "pydantic"),
            "openmachine.chat.mcp.tasks": ("openmachine.chat.tasks", "openmachine.chat.mcp.protocol", "mcp.*", "pydantic"),
            "openmachine.chat.mcp.execution": ("openmachine.chat.tasks", "openmachine.chat.mcp.protocol", "mcp.*", "pydantic"),
            "openmachine.chat.server": ("openmachine", "openmachine.chat.bootstrap", "openmachine.config",
                                   "openmachine.service", "openmachine.service.updates", "openmachine.skills", "openmachine.chat.mcp.protocol",
                                   "openmachine.chat.mcp.connections", "openmachine.chat.mcp.workspace", "openmachine.chat.mcp.environment", "openmachine.chat.mcp.projects",
                                   "openmachine.chat.mcp.tasks", "openmachine.chat.mcp.execution", "openmachine.chat.github_web_http",
                                   "openmachine.foundation.*", "mcp.*", "jsonschema", "pydantic", "uvicorn"),
            "openmachine.chat.github_web_http": ("openmachine.foundation.sanitize", "uvicorn.protocols.http.h11_impl"),
            # The CLI owns native serve dispatch, not execution internals.
            "openmachine.cli": ("openmachine", "openmachine.config", "openmachine.foundation.*",
                                "openmachine.chat.bootstrap", "openmachine.chat.server", "openmachine.service", "openmachine.service.maintenance"),
            "integrations.codex": ("pydantic", "openmachine.chat.execution", "openmachine.foundation.*"),
            "integrations.connection_state": ("pydantic", "openmachine.config", "openmachine.foundation.*"),
            "integrations.connections": ("integrations.connection_state", "integrations.credentials",
                                        "integrations.github.connection", "integrations.github.web_authorization", "integrations.github.cloud",
                                        "openmachine.config", "openmachine.foundation.*"),
            "integrations.github.web_authorization": ("integrations.connection_state", "integrations.credentials",
                                                     "integrations.github.auth", "integrations.github.web_broker",
                                                     "openmachine.foundation.sanitize"),
            "integrations.credentials": ("pydantic", "openmachine.foundation.*"),
            "integrations.github.auth": ("httpx2", "integrations.credentials", "openmachine.foundation.*"),
            "integrations.github.cloud": ("integrations.connection_state", "integrations.github.web_broker", "openmachine.foundation.*", "pydantic"),
            "integrations.github.web_broker": ("integrations.github.auth", "openmachine.foundation.*"),
            "integrations.github": ("integrations.github.operations", "integrations.github.actions"),
            "integrations.github.actions": ("integrations.github.client", "integrations.github.connection",
                                             "integrations.github.evidence", "openmachine.foundation.*", "pydantic"),
            "integrations.github.connection": ("openmachine.config", "openmachine.foundation.*"),
            "integrations.github.client": ("integrations.github.connection", "openmachine.foundation.*"),
            "integrations.github.evidence": ("integrations.github.client", "integrations.github.connection",
                                             "openmachine.foundation.*"),
            "integrations.github.operations": ("integrations.github.actions", "integrations.github.client", "integrations.github.connection",
                                               "integrations.github.evidence", "openmachine.config", "openmachine.foundation.*"),
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
