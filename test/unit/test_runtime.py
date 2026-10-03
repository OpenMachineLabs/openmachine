"""Local contracts with disposable directories and real short commands."""

import ast
from graphlib import CycleError, TopologicalSorter
from importlib.resources import files
from importlib.util import resolve_name
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from openmachine import execution, workspace
from openmachine.execution import logs
from openmachine.execution.records import TERMINAL
from openmachine.workspace.git import MAX_READ
from openmachine.config import Settings, Workspace, load
from openmachine.foundation.sanitize import Fault, redact


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.config = Settings.model_validate({
            "schema_version": 1, "machines": {"my-laptop": {"transport": "local", "execution_enabled": True}},
            "workspaces": {"project": {"machine": "my-laptop", "root": str(self.work)}},
        })

    def start(self, code, *, execution_id=None, cwd=None, **kwargs):
        return execution.start(self.config, self.state, "my-laptop", execution_id or uuid.uuid4().hex,
                               str(cwd or self.work), [sys.executable, "-c", code], **kwargs)

    def wait(self, result):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = execution.read(self.config, self.state, "my-laptop", result["execution_id"])
            if result["status"] in TERMINAL | {"outcome_unknown"}:
                return result
            time.sleep(0.03)
        self.fail(f"Execution did not finish: {result['execution_id']}")

    def assert_import_contract(self, sources):
        # Bare names are exact interfaces; .* permits an owned implementation tree.
        boundaries = {
            "openmachine": (),
            "integrations": (),
            "openmachine.foundation.*": ("openmachine.foundation.*",),
            "openmachine.execution.*": ("openmachine", "openmachine.execution.*",
                                        "openmachine.foundation.*", "openmachine.workspace.git"),
            "openmachine.workspace.*": ("openmachine", "openmachine.workspace.*", "openmachine.foundation.*"),
            "openmachine.tasks": ("openmachine.tasks.store", "openmachine.tasks.index", "openmachine.tasks.records"),
            "openmachine.tasks.store": ("openmachine.tasks.index", "openmachine.tasks.records", "openmachine.foundation.*"),
            "openmachine.tasks.index": ("openmachine.tasks.records", "openmachine.foundation.*"),
            "openmachine.tasks.records": ("pydantic", "openmachine.foundation.*"),
            "openmachine.tasks.transitions": ("openmachine.tasks.records", "openmachine.foundation.*"),
            "openmachine.config": ("pydantic", "openmachine.foundation.*"),
            "openmachine.skills": ("openmachine.foundation.*",),
            "openmachine.service": ("openmachine.service.state", "openmachine.service.checks",
                                    "openmachine.service.runtime", "openmachine.service.transition"),
            "openmachine.service.__main__": ("openmachine.service.checks", "openmachine.foundation.*"),
            "openmachine.service.state": ("openmachine.config", "openmachine.foundation.*"),
            "openmachine.service.checks": ("openmachine", "openmachine.config", "openmachine.execution",
                                           "openmachine.tasks", "openmachine.workspace", "openmachine.service.state",
                                           "openmachine.foundation.*", "httpx2", "mcp.*",
                                           "integrations.ssh.recovery", "integrations.connections", "integrations.chatgpt"),
            "openmachine.service.install": ("openmachine.config", "openmachine.service.checks",
                                            "openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.service.runtime": ("openmachine.config", "openmachine.service.checks",
                                            "openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.service.transition": ("openmachine.config", "openmachine.service.checks",
                                               "openmachine.service.install", "openmachine.service.runtime",
                                               "openmachine.service.state", "openmachine.foundation.*"),
            "openmachine.app": ("openmachine", "openmachine.app.chat_recovery", "openmachine.app.machines", "openmachine.app.task_admission", "openmachine.app.task_observer", "openmachine.config",
                                "openmachine.execution", "openmachine.workspace", "openmachine.tasks",
                                "openmachine.skills", "openmachine.service", "openmachine.foundation.*"),
            "openmachine.app.chat_recovery": ("integrations.chatgpt", "openmachine.tasks", "openmachine.tasks.records",
                                              "openmachine.tasks.transitions", "openmachine.foundation.*"),
            "openmachine.app.machines": ("openmachine.config", "openmachine.execution", "openmachine.workspace",
                                         "openmachine.tasks", "openmachine.foundation.*"),
            "openmachine.app.task_admission": ("openmachine.execution", "openmachine.tasks",
                                               "openmachine.tasks.records", "openmachine.tasks.transitions", "openmachine.foundation.*"),
            "openmachine.app.task_observer": ("openmachine.tasks", "openmachine.tasks.records", "openmachine.app.task_observation", "openmachine.foundation.*"),
            "openmachine.app.task_observation": ("openmachine.tasks.records", "openmachine.tasks.transitions", "openmachine.foundation.*"),
            "openmachine.bootstrap": ("openmachine.app", "openmachine.app.machines", "openmachine.config",
                                      "integrations.github", "integrations.github.connection", "integrations.github.auth", "integrations.ssh.client", "integrations.ssh.recovery", "integrations.connections", "integrations.codex", "integrations.chatgpt"),
            "openmachine.server": ("openmachine", "openmachine.app", "openmachine.bootstrap", "openmachine.config",
                                   "openmachine.service", "openmachine.tasks", "openmachine.foundation.*", "mcp.*", "jsonschema", "pydantic", "uvicorn"),
            # The CLI owns serve/worker-bundle dispatch, not execution internals.
            "openmachine.cli": ("openmachine", "openmachine.config", "openmachine.foundation.*",
                                "openmachine.bootstrap", "openmachine.server", "openmachine.service",
                                "integrations.ssh.bundle"),
            "integrations.chatgpt": ("pydantic", "openmachine.foundation.*"),
            "integrations.codex": ("pydantic", "openmachine.execution", "openmachine.foundation.*"),
            "integrations.connections": ("pydantic", "integrations.credentials", "openmachine.config", "openmachine.foundation.*"),
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

    def test_runtime_import_boundaries_and_dependency_graph(self):
        sources = {}
        for root in ("openmachine", "integrations"):
            directory = Path(str(files(root)))
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
            ("openmachine.tasks.index", "from . import store", "must not depend"),
            ("integrations.github.connection", "from . import client", "must not depend"),
            ("openmachine.execution.records", "from openmachine.config import Settings", "must not depend"),
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

    def test_configuration_rejects_unknown_or_invalid_fields(self):
        path = self.root / "config.toml"
        for text in (
            'schema_version = 1\n[machines.any]\ntransport = "ssh"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\nextra = true\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\n[workspaces.x]\nmachine = "missing"\nroot = "/tmp"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\n[workspaces.x]\nmachine = "any"\nroot = "relative"\n',
            'schema_version = 1\n[machines.any]\ntransport = "local"\nexecution_enabled = "true"\n',
        ):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(Fault) as caught:
                    load(path)
                self.assertEqual(caught.exception.code, "invalid_config")

    def test_init_is_disabled_private_and_never_overwrites(self):
        path = self.root / "config.toml"
        command = [sys.executable, "-I", "-m", "openmachine.cli", "--config", str(path),
                   "init", "--workspace", str(self.work)]
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse(load(path).machines["local"].execution_enabled)
        self.assertIs(load(path).machines["local"].workspace_write_enabled, False)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        content = path.read_bytes()
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(path.read_bytes(), content)

    def test_doctor_does_not_create_state_or_claim_live_health(self):
        path = self.root / "config.toml"
        path.write_text('schema_version = 1\n[machines.local]\ntransport = "local"\n')
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(path),
                                 "--state-dir", str(self.state), "doctor"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual(json.loads(result.stdout)["chatgpt"], "not_probed")

    def test_disabled_machine_cannot_execute(self):
        self.config.machines["my-laptop"].execution_enabled = False
        with self.assertRaises(Fault) as caught:
            self.start('print("no")')
        self.assertEqual(caught.exception.code, "execution_disabled")
        self.assertFalse(self.state.exists())

    def test_dotfiles_are_read_but_credentials_and_escaping_symlinks_are_not(self):
        (self.work / ".github").mkdir()
        (self.work / ".github/AGENTS.md").write_text("Read this instruction.\n")
        result = workspace.inspect(self.config, "my-laptop", "project", "read", ".github/AGENTS.md")
        self.assertEqual(result["text"], "Read this instruction.\n")
        (self.work / ".env").write_text("private fixture\n")
        (self.root / "outside").write_text("outside fixture\n")
        (self.work / "escape").symlink_to(self.root / "outside")
        (self.work / "hidden-secret").symlink_to(self.work / ".env")
        for path in (".env", "../outside", "escape", "hidden-secret"):
            with self.subTest(path=path), self.assertRaises(Fault):
                workspace.inspect(self.config, "my-laptop", "project", "read", path)
        names = [item["name"] for item in workspace.inspect(self.config, "my-laptop", "project", "files")["entries"]]
        self.assertNotIn(".env", names)

    def test_binary_and_fifo_reads_do_not_block(self):
        (self.work / "binary").write_bytes(b"a\0b")
        os.mkfifo(self.work / "fifo")
        for path in ("binary", "fifo"):
            with self.subTest(path=path), self.assertRaises(Fault):
                workspace.inspect(self.config, "my-laptop", "project", "read", path)

    def test_file_byte_pagination_and_listing(self):
        content = "x" * (MAX_READ * 3)
        (self.work / "large.txt").write_text(content)
        parts, offset = [], 0
        while True:
            page = workspace.inspect(self.config, "my-laptop", "project", "read", "large.txt", offset)
            parts.append(page["text"])
            offset = page["next_offset"]
            if not page["has_more"]:
                break
        self.assertEqual("".join(parts), content)
        self.assertEqual(len(parts), 6)
        for name in ("a", "b", "c"):
            (self.work / name).touch()
        page = workspace.inspect(self.config, "my-laptop", "project", "files", limit=2)
        self.assertEqual((len(page["entries"]), page["next_offset"]), (2, 2))

    def test_redaction_is_applied(self):
        self.assertNotIn("synthetic-value", redact("password=synthetic-value"))

    def test_native_write_readback_and_identical_id_deduplication(self):
        code = 'from pathlib import Path; p=Path("counter"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print("written")'
        first = self.wait(self.start(code))
        self.assertEqual((first["status"], first["exit_code"]), ("succeeded", 0))
        again = self.start(code, execution_id=first["execution_id"])
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.assertIn("written", first["output"])
        with self.assertRaises(Fault) as caught:
            self.start('print("different")', execution_id=first["execution_id"])
        self.assertEqual(caught.exception.code, "execution_conflict")

    def test_empty_positional_argument_is_preserved(self):
        result = execution.start(self.config, self.state, "my-laptop", uuid.uuid4().hex,
                                 str(self.work), [sys.executable, "-c", "import sys; print(repr(sys.argv[1]))", ""])
        self.assertEqual(self.wait(result)["output"], "''\n")


    def test_timeout_has_a_terminal_receipt(self):
        result = self.wait(self.start("import time; time.sleep(20)", timeout_seconds=1))
        self.assertEqual(result["status"], "timed_out")
        self.assertNotEqual(result["exit_code"], 0)

    def test_same_worktree_is_exclusive_but_other_workspaces_can_run(self):
        first = self.start("import time; time.sleep(1)")
        try:
            with self.assertRaises(Fault) as caught:
                self.start('print("conflict")')
            self.assertEqual(caught.exception.code, "resource_busy")
            other = self.root / "other"
            other.mkdir()
            self.config.workspaces["other"] = Workspace(machine="my-laptop", root=str(other))
            self.assertEqual(self.wait(self.start('print("independent")', cwd=other))["status"], "succeeded")
        finally:
            self.assertEqual(self.wait(first)["status"], "succeeded")

    def test_expected_head_mismatch_prevents_launch(self):
        with self.assertRaises(Fault) as caught:
            self.start('from pathlib import Path; Path("should-not-exist").touch()', expected_head_sha="0" * 40)
        self.assertEqual(caught.exception.code, "context_mismatch")
        self.assertFalse((self.work / "should-not-exist").exists())

    def test_log_limit_does_not_kill_command_and_pages_remain_bounded(self):
        result = self.wait(self.start('import sys; sys.stdout.write("x" * (4*1024*1024+100))'))
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["log_truncated"])
        self.assertLessEqual(result["retained_bytes"], execution.MAX_LOG)
        self.assertEqual(result["log_start_offset"], logs.SEGMENT_BYTES)
        self.assertTrue(result["output_gap"])
        self.assertEqual(len(result["output"]), execution.MAX_READ)
        page = execution.read(self.config, self.state, "my-laptop", result["execution_id"], offset=10, max_bytes=7)
        self.assertEqual((page["output"], page["next_offset"]), ("xxxxxxx", logs.SEGMENT_BYTES + 7))

    def test_missing_worker_and_corrupt_receipt_never_become_success(self):
        result = self.wait(self.start('print("original")'))
        path = self.state / "executions" / result["execution_id"] / "record.json"
        record = json.loads(path.read_text())
        record.update(status="running", exit_code=None)
        path.write_text(json.dumps(record))
        unknown = execution.read(self.config, self.state, "my-laptop", result["execution_id"])
        self.assertEqual(unknown["status"], "outcome_unknown")
        path.write_text("broken json")
        with self.assertRaises(Fault) as caught:
            execution.read(self.config, self.state, "my-laptop", result["execution_id"])
        self.assertEqual(caught.exception.code, "corrupt_state")

    def test_missing_unrelated_workspace_does_not_disable_valid_workspace(self):
        valid = self.config.workspaces.pop("project")
        self.config.workspaces["missing"] = Workspace(machine="my-laptop", root=str(self.root / "absent"))
        self.config.workspaces["project"] = valid
        self.assertEqual(self.wait(self.start('print("available")'))["status"], "succeeded")

    def test_incomplete_terminal_receipt_is_corrupt(self):
        result = self.wait(self.start('print("original")'))
        path = self.state / "executions" / result["execution_id"] / "record.json"
        original = json.loads(path.read_text())
        for key in ("version", "log_truncated", "exit_code", "finished_at"):
            with self.subTest(key=key):
                partial = dict(original)
                del partial[key]
                path.write_text(json.dumps(partial))
                with self.assertRaises(Fault) as caught:
                    execution.read(self.config, self.state, "my-laptop", result["execution_id"])
                self.assertEqual(caught.exception.code, "corrupt_state")

    def test_worktree_alias_does_not_bypass_lock_and_dirty_files_survive(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.work), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial")
        head = git("rev-parse", "HEAD")
        (self.work / "unrelated.txt").write_text("preserve me")
        sub = self.work / "sub"
        sub.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.work, target_is_directory=True)
        self.config.workspaces["alias"] = Workspace(machine="my-laptop", root=str(alias))
        first = self.start("import time; time.sleep(1)", expected_head_sha=head)
        try:
            for cwd in (sub, alias):
                with self.subTest(cwd=cwd), self.assertRaises(Fault) as caught:
                    self.start('print("conflict")', cwd=cwd)
                self.assertEqual(caught.exception.code, "resource_busy")
        finally:
            self.assertEqual(self.wait(first)["status"], "succeeded")
        self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve me")
        self.assertEqual(git("rev-parse", "HEAD"), head)

    def test_unsafe_state_directory_is_rejected_without_chmod(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaises(Fault) as caught:
            self.start('print("no")')
        self.assertEqual(caught.exception.code, "unsafe_state")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)


    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.work), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def _repository(self):
        self._git("init", "-q")
        (self.work / "code.py").write_text("value = 1\n")
        self._git("add", "code.py")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")

    def test_git_status_and_diff_preserve_index_and_distinguish_changes(self):
        self._repository()
        code = self.work / "code.py"
        code.write_text("value = 2\n")
        self._git("add", "code.py")
        code.write_text("value = 3\n")
        (self.work / "unrelated.txt").write_text("preserve me\n")
        (self.work / ".env").write_text("private fixture\n")
        index = (self.work / ".git/index").read_bytes()
        result = workspace.inspect(self.config, "my-laptop", "project", "status")
        items = {item["path"]: item for item in result["entries"]}
        self.assertEqual((items["code.py"]["index"], items["code.py"]["worktree"]), ("M", "M"))
        self.assertTrue(items["unrelated.txt"]["untracked"])
        self.assertNotIn(".env", items)
        unstaged = workspace.inspect(self.config, "my-laptop", "project", "diff")
        staged = workspace.inspect(self.config, "my-laptop", "project", "diff", staged=True)
        self.assertIn("+value = 3", unstaged["text"])
        self.assertIn("+value = 2", staged["text"])
        self.assertEqual((self.work / ".git/index").read_bytes(), index)
        self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve me\n")

    def test_staged_deletions_and_nested_workspace_scope(self):
        self._repository()
        sub = self.work / "sub"
        sub.mkdir()
        (sub / "deleted.txt").write_text("old content\n")
        (sub / ".env").write_text("secret fixture\n")
        self._git("add", "sub")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "nested")
        self._git("rm", "sub/deleted.txt")
        (sub / ".env").write_text("new secret fixture\n")
        self._git("add", "sub/.env")
        (self.work / "code.py").write_text("outside scope\n")
        self.config.workspaces["nested"] = Workspace(machine="my-laptop", root=str(sub))
        result = workspace.inspect(self.config, "my-laptop", "nested", "diff", staged=True)
        self.assertIn("-old content", result["text"])
        self.assertNotIn("secret fixture", result["text"])
        self.assertEqual(result["skipped_paths"], 1)
        direct = workspace.inspect(self.config, "my-laptop", "nested", "diff", "deleted.txt", staged=True)
        self.assertIn("deleted file", direct["text"])
        self.assertEqual(workspace.inspect(self.config, "my-laptop", "nested", "diff")["text"], "")
        status = workspace.inspect(self.config, "my-laptop", "nested", "status")
        self.assertEqual([entry["path"] for entry in status["entries"]], ["deleted.txt"])

    def test_git_pages_reject_changed_snapshot_and_literal_paths_are_safe(self):
        self._repository()
        (self.work / "code.py").write_text("changed line\n" * 10000)
        first = workspace.inspect(self.config, "my-laptop", "project", "diff")
        self.assertTrue(first["has_more"])
        second = workspace.inspect(self.config, "my-laptop", "project", "diff", offset=first["next_offset"],
                                   expected_snapshot=first["snapshot"])
        self.assertEqual(first["snapshot"], second["snapshot"])
        (self.work / "code.py").write_text("different\n")
        with self.assertRaises(Fault) as caught:
            workspace.inspect(self.config, "my-laptop", "project", "diff", expected_snapshot=first["snapshot"])
        self.assertEqual(caught.exception.code, "source_changed")
        for path in ("../code.py", ".env", str(self.work / "code.py")):
            with self.subTest(path=path), self.assertRaises(Fault):
                workspace.inspect(self.config, "my-laptop", "project", "diff", path)

    def test_git_readers_never_run_external_filters_or_fsmonitor(self):
        self._repository()
        hook = self.work / "reader-hook.sh"
        hook.write_text('#!/bin/sh\ntouch "' + str(self.work / 'hook-ran') + '"\ncat\n')
        hook.chmod(0o700)
        (self.work / ".gitattributes").write_text("*.py diff=fixture filter=fixture\n")
        for name in ("diff.fixture.command", "diff.fixture.textconv", "filter.fixture.clean", "filter.fixture.process", "core.fsmonitor"):
            self._git("config", name, str(hook))
        self._git("config", "filter.fixture.required", "true")
        (self.work / "code.py").write_text("value = 5\n")
        status = workspace.inspect(self.config, "my-laptop", "project", "status")
        diff = workspace.inspect(self.config, "my-laptop", "project", "diff")
        self.assertTrue(status["entries"])
        self.assertIn("+value = 5", diff["text"])
        self.assertTrue(any("filters" in warning for warning in diff["warnings"]))
        self.assertFalse((self.work / "hook-ran").exists())

    def test_literal_search_pages_skips_and_edit_detection(self):
        (self.work / ".github").mkdir()
        (self.work / ".github/AGENTS.md").write_text("needle instruction\n")
        (self.work / "source.py").write_text("needle first\nneedle second\n")
        (self.work / ".env").write_text("needle secret fixture\n")
        (self.work / "binary").write_bytes(b"needle\0")
        (self.work / "link").symlink_to(self.root / "outside")
        (self.root / "outside").write_text("needle outside\n")
        first = workspace.inspect(self.config, "my-laptop", "project", "search", query="needle", limit=1)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["matches"][0]["path"], ".github/AGENTS.md")
        self.assertGreater(first["skipped"]["excluded"], 0)
        self.assertEqual(first["skipped"]["binary"], 1)
        second = workspace.inspect(self.config, "my-laptop", "project", "search", query="needle", offset=1,
                                   expected_snapshot=first["snapshot"])
        self.assertEqual(len(second["matches"]), 2)
        (self.work / "source.py").write_text("needle changed\n")
        with self.assertRaises(Fault) as caught:
            workspace.inspect(self.config, "my-laptop", "project", "search", query="needle", expected_snapshot=first["snapshot"])
        self.assertEqual(caught.exception.code, "source_changed")

    def test_search_limits_and_non_git_failure_are_explicit(self):
        (self.work / "large.txt").write_text("x" * (workspace.MAX_CAPTURE + 1) + "needle")
        result = workspace.inspect(self.config, "my-laptop", "project", "search", query="needle")
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["skipped"]["oversized"], 1)
        with self.assertRaises(Fault) as caught:
            workspace.inspect(self.config, "my-laptop", "project", "status")
        self.assertEqual(caught.exception.code, "git_unavailable")

    def test_timeout_default_and_explicit_day_limit_are_separate(self):
        first = self.wait(self.start('print("default")'))
        record = json.loads((self.state / "executions" / first["execution_id"] / "record.json").read_text())
        self.assertEqual(record["request"]["timeout_seconds"], 600)
        self.assertEqual(self.wait(self.start('print("long limit, short command")', timeout_seconds=86400))["status"], "succeeded")
        for value in (0, 86401):
            with self.subTest(value=value), self.assertRaises(Fault) as caught:
                self.start('print("not started")', timeout_seconds=value)
            self.assertEqual(caught.exception.code, "invalid_timeout")

    def test_cancellation_cleans_owned_children_and_preserves_other_work(self):
        other = self.root / "other"
        other.mkdir()
        self.config.workspaces["other"] = Workspace(machine="my-laptop", root=str(other))
        child = 'import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("child-ready",flush=True); time.sleep(1.5); Path("escaped").touch(); time.sleep(10)'
        command = 'import signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(10)'
        owned = self.start(command, termination_grace_seconds=0.1)
        independent = self.start('import time; from pathlib import Path; time.sleep(0.7); Path("done").touch()', cwd=other)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "child-ready" in execution.read(self.config, self.state, "my-laptop", owned["execution_id"])["output"]:
                break
            time.sleep(0.03)
        requested = execution.cancel(self.config, self.state, "my-laptop", owned["execution_id"])
        self.assertTrue(requested["cancel_requested"])
        execution.cancel(self.config, self.state, "my-laptop", owned["execution_id"])
        result = self.wait(owned)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(self.wait(independent)["status"], "succeeded")
        time.sleep(1.5)
        self.assertFalse((self.work / "escaped").exists())
        self.assertTrue((other / "done").exists())

    def test_completed_execution_cancellation_is_a_noop(self):
        result = self.wait(self.start('from pathlib import Path; Path("once").write_text("once")'))
        path = self.state / "executions" / result["execution_id"] / "record.json"
        original = path.read_bytes()
        cancelled = execution.cancel(self.config, self.state, "my-laptop", result["execution_id"])
        self.assertEqual(cancelled["status"], "succeeded")
        self.assertFalse(cancelled["cancel_requested"])
        self.assertEqual(path.read_bytes(), original)

    def test_shared_workspaces_and_exclusive_named_resources(self):
        first = self.start('import time; time.sleep(0.7)', workspace_access="shared", resource_keys=["device:0"])
        try:
            self.assertEqual(self.wait(self.start('print("shared")', workspace_access="shared"))["status"], "succeeded")
            with self.assertRaises(Fault) as caught:
                self.start('print("writer")')
            self.assertEqual(caught.exception.code, "resource_busy")
            other = self.root / "other"
            other.mkdir()
            self.config.workspaces["other"] = Workspace(machine="my-laptop", root=str(other))
            with self.assertRaises(Fault) as caught:
                self.start('print("resource conflict")', cwd=other, resource_keys=["device:0"])
            self.assertEqual(caught.exception.code, "resource_busy")
        finally:
            self.wait(first)
        result = self.wait(self.start('print("normalized")', resource_keys=["b", "a"]))
        again = self.start('print("normalized")', execution_id=result["execution_id"], resource_keys=["a", "b", "a"])
        self.assertEqual(again["status"], "succeeded")

    def test_execution_listing_pages_and_reports_corruption_without_commands(self):
        results = [self.wait(self.start('print("not in listing")', execution_id=f"{i:032x}")) for i in (1, 2, 3)]
        damaged = self.state / "executions" / f"{2:032x}" / "record.json"
        damaged.write_text("broken")
        first = execution.list_records(self.config, self.state, "my-laptop", limit=2)
        self.assertEqual(first["next_cursor"], f"{2:032x}")
        self.assertEqual(len(first["unreadable"]), 1)
        second = execution.list_records(self.config, self.state, "my-laptop", cursor=first["next_cursor"], limit=2)
        self.assertEqual(second["executions"][0]["execution_id"], results[2]["execution_id"])
        self.assertIsNone(second["next_cursor"])
        self.assertNotIn("not in listing", json.dumps(first))

    def test_legacy_receipts_and_prefix_logs_remain_readable_without_rewriting(self):
        result = self.wait(self.start('print("legacy")'))
        directory = self.state / "executions" / result["execution_id"]
        record = json.loads((directory / "record.json").read_text())
        del record["format_version"]
        record["version"] = "0.1.0a1"
        del record["request"]["workspace_access"]
        del record["request"]["resource_keys"]
        (directory / "record.json").write_text(json.dumps(record))
        (directory / "output-00000000000000000000.log").rename(directory / "output.log")
        (directory / "control.lock").unlink()
        original = (directory / "record.json").read_bytes()
        observed = execution.read(self.config, self.state, "my-laptop", result["execution_id"])
        self.assertEqual((observed["output"], observed["log_retention"]), ("legacy\n", "prefix_only"))
        self.assertEqual(self.start('print("legacy")', execution_id=result["execution_id"])["status"], "succeeded")
        self.assertEqual((directory / "record.json").read_bytes(), original)

    def test_malformed_log_segment_is_reported_as_corrupt_output(self):
        directory = self.root / "bad-log-fixture"
        directory.mkdir()
        with logs.Writer(directory) as stream:
            stream.write(b"original")
        (directory / "output-invalid.log").write_text("broken")
        with self.assertRaises(Fault) as caught:
            logs.read(directory, 0, 100)
        self.assertEqual(caught.exception.code, "corrupt_output")

    def test_segment_crossing_tail_retention_and_absolute_offsets(self):
        directory = self.root / "log-fixture"
        directory.mkdir()
        with logs.Writer(directory) as stream:
            stream.write(b"a" * (logs.SEGMENT_BYTES - 3))
            stream.write(b"boundary")
            page = logs.read(directory, logs.SEGMENT_BYTES - 3, 8)
            self.assertEqual(page["output"], "boundary")
            stream.write(b"x" * logs.MAX_BYTES)
            stream.write(b"final-message")
        paths = list(directory.glob("output-*.log"))
        self.assertLessEqual(len(paths), 4)
        self.assertLessEqual(sum(path.stat().st_size for path in paths), logs.MAX_BYTES)
        gap = logs.read(directory, 0, 7)
        self.assertTrue(gap["output_gap"])
        tail = logs.read(directory, gap["log_bytes"] - 13, 13)
        self.assertEqual(tail["output"], "final-message")
        self.assertEqual(tail["next_offset"], gap["log_bytes"])



if __name__ == "__main__":
    unittest.main()
