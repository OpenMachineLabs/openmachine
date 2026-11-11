"""Behavioral boundaries for module ownership, selection, and candidate execution."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test.support.repository import RepositoryFixture


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("test_runner", ROOT / "scripts/run_tests.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class TestSelection(RepositoryFixture, unittest.TestCase):
    def test_github_changes_include_declared_boundaries_without_unrelated_processes(self):
        self.write("openmachine/github.py", "VALUE = 2\n")
        plan = runner.make_plan(self.root, self.base)
        self.assertEqual(self.product_tests(plan), {
            "test/unit/github/test_values.py", "test/component/github/test_calls.py",
            "test/component/server/test_dispatch.py",
        })
        self.assertEqual(plan["scope"], "affected")
        self.assertTrue(plan["dirty"])

    def test_test_changes_and_fixture_changes_do_not_expand_through_reverse_owners(self):
        path = "test/component/server/test_dispatch.py"
        self.write(path, self.files[path] + "\n")
        self.assertEqual(self.product_tests(runner.make_plan(self.root, self.base)), {path})
        self.git("restore", path)
        self.write("test/support/wire.py", '"""Changed wire helper."""\n')
        self.assertEqual(self.product_tests(runner.make_plan(self.root, self.base)), {path})

    def test_support_package_initializer_selects_all_declared_consumers(self):
        self.write("test/support/__init__.py", '"""Support package."""\n')
        self.write("openmachine/github.py", "VALUE = 2\n")
        self.manifest["support"]["test/support/wire.py"].append("test/resilience/execution/test_process.py")
        self.save_manifest()
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.write("test/support/__init__.py", '"""Changed support initialization."""\n')
        self.write("openmachine/github.py", "VALUE = 3\n")
        self.assertIn("test/resilience/execution/test_process.py", runner.make_plan(self.root, self.base)["tests"])

    def test_removing_empty_test_package_needs_no_nonexistent_behavior(self):
        path = "test/component/retired/__init__.py"
        self.write(path, "")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        (self.root / path).unlink()
        plan = runner.make_plan(self.root, self.base)
        self.assertFalse(plan["runtime_required"])
        self.assertFalse(self.product_tests(plan))

    def test_documentation_runs_repository_checks_without_an_environment(self):
        self.write("README.md", "# Updated documentation\n")
        plan = runner.make_plan(self.root, self.base)
        self.assertFalse(plan["runtime_required"])
        self.assertEqual(plan["tests"], [])
        self.write("spec/fixture/inputs.json", '{"format": 1}\n')
        plan = runner.make_plan(self.root, self.base)
        self.assertFalse(plan["runtime_required"])
        self.assertEqual(plan["tests"], [])

    def test_go_connector_changes_run_repository_checks_only(self):
        self.write("connector/test/proof_test.go", "package test\n")
        plan = runner.make_plan(self.root, self.base)
        self.assertFalse(plan["runtime_required"])
        self.assertEqual(plan["tests"], [])

    def test_community_policy_documents_select_repository_checks(self):
        for path in ("CONTRIBUTING.md", "MAINTAINERS.md", "SECURITY.md",
                     "CODE_OF_CONDUCT.md"):
            with self.subTest(path=path):
                self.write(path, "# Community policy\n")
                plan = runner.make_plan(self.root, self.base)
                self.assertFalse(plan["runtime_required"])
                self.assertEqual(plan["tests"], [])

    def test_repository_execution_is_independent_of_ambient_pr_metadata(self):
        for path in ("scripts/run_tests.py", "scripts/check_repository.py"):
            self.write(path, (ROOT / path).read_text())
        self.write(".github/labels.json", json.dumps([
            {"name": "maintenance", "color": "ffffff", "description": "Repository maintenance."}]) + "\n")
        self.write(".gitignore", "__pycache__/\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.write("README.md", "# Updated documentation\n")
        plan = runner.make_plan(self.root, self.base)
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "pr.json"
            event.write_text(json.dumps({"pull_request": {"title": "Invalid title", "body": ""}}))
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event)}):
                result = runner.execute_plan(self.root, plan)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["layers"], [])
        self.assertFalse(any("environment" in stage for stage in result["stages"]))

    def test_skill_library_named_folder_is_selected_by_ownership(self):
        test = "test/skill-library/example-skill/test_behavior.py"
        source = "skill-library/example-skill/SKILL.md"
        self.write(test, "import unittest\nclass Check(unittest.TestCase):\n    def test_example(self): self.assertTrue(True)\n")
        self.write(source, "---\nname: example-skill\n---\n# Example\n")
        self.manifest["modules"]["example_skill"] = {
            "sources": [source], "tests": [test], "boundary_tests": []}
        self.save_manifest()
        plan = runner.make_plan(self.root, self.base)
        self.assertIn(test, plan["tests"])
        self.assertTrue(plan["runtime_required"])
        self.assertEqual(plan["environment"], "wheel")
        with self.assertRaisesRegex(ValueError, "named Skill directory"):
            runner.test_path("test/skill-library/test_example.py")

    def test_repository_fixture_change_does_not_require_product_installation(self):
        self.manifest["support"]["test/support/wire.py"] = ["test/repository/test_rules.py"]
        self.save_manifest()
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.write("test/support/wire.py", '"""Changed repository fixture."""\n')
        plan = runner.make_plan(self.root, self.base)
        self.assertFalse(plan["runtime_required"])
        self.assertEqual(plan["tests"], ["test/repository/test_rules.py"])

    def test_unregistered_code_and_tests_fall_back_to_complete_discovery(self):
        self.write("openmachine/new_module.py", "VALUE = 1\n")
        new = "test/unit/new_module/test_new.py"
        self.write(new, self.files["test/unit/github/test_values.py"])
        plan = runner.make_plan(self.root, self.base)
        self.assertEqual(plan["scope"], "full")
        self.assertIn(new, plan["tests"])
        self.assertIn("test/resilience/execution/test_process.py", plan["tests"])
        self.write("unregistered-tool.py", "VALUE = 1\n")
        self.assertEqual(runner.make_plan(self.root, self.base)["scope"], "full")

    def test_new_tests_in_an_existing_directory_inherit_the_owner(self):
        new = "test/unit/github/test_more.py"
        self.write(new, self.files["test/unit/github/test_values.py"])
        self.write("openmachine/github.py", "VALUE = 2\n")
        plan = runner.make_plan(self.root, self.base)
        self.assertIn(new, plan["tests"])
        self.assertNotIn("test/resilience/execution/test_process.py", plan["tests"])
        self.assertEqual(plan["scope"], "affected")


    def test_staged_and_unstaged_changes_are_both_selected(self):
        self.write("openmachine/github.py", "VALUE = 2\n")
        self.git("add", "openmachine/github.py")
        self.write("openmachine/execution.py", "VALUE = 2\n")
        plan = runner.make_plan(self.root, self.base)
        self.assertIn("test/component/github/test_calls.py", plan["tests"])
        self.assertIn("test/resilience/execution/test_process.py", plan["tests"])

    def test_staged_change_reverted_only_in_worktree_still_selects_its_owner(self):
        self.write("openmachine/github.py", "VALUE = 2\n")
        self.git("add", "openmachine/github.py")
        self.write("openmachine/github.py", "VALUE = 1\n")
        self.assertEqual(self.git("diff", "HEAD", "--", "openmachine/github.py"), "")
        plan = runner.make_plan(self.root, self.base)
        self.assertIn("test/component/github/test_calls.py", plan["tests"])

    def test_rename_keeps_both_endpoints_and_their_declared_tests(self):
        self.git("mv", "openmachine/github.py", "openmachine/provider.py")
        self.manifest["modules"]["github"]["sources"] = ["openmachine/provider.py"]
        self.save_manifest()
        plan = runner.make_plan(self.root, self.base)
        self.assertIn("openmachine/github.py", plan["changed_paths"])
        self.assertIn("openmachine/provider.py", plan["changed_paths"])
        self.assertNotIn("test/resilience/execution/test_process.py", plan["tests"])

    def test_deleted_test_runs_its_retained_owning_behavior(self):
        path = "test/unit/github/test_values.py"
        (self.root / path).unlink()
        self.manifest["modules"]["github"]["tests"].remove(path)
        self.save_manifest()
        plan = runner.make_plan(self.root, self.base)
        self.assertEqual(self.product_tests(plan), {
            "test/component/github/test_calls.py", "test/component/server/test_dispatch.py"})

    def test_a_surviving_owner_cannot_hide_behind_another_modules_tests(self):
        (self.root / "test/resilience/execution/test_process.py").unlink()
        self.manifest["modules"]["execution"]["tests"] = []
        self.manifest["modules"]["execution"]["boundary_tests"] = ["test/repository/test_rules.py"]
        self.save_manifest()
        self.write("openmachine/execution.py", "VALUE = 2\n")
        self.write("openmachine/github.py", "VALUE = 2\n")
        for full in (False, True):
            with self.subTest(full=full), self.assertRaisesRegex(ValueError, "no retained behavior coverage"):
                runner.make_plan(self.root, self.base, full=full)

    def test_a_baseline_may_name_a_layer_the_runner_retired(self):
        retired = "test/contract/old/test_old.py"
        self.write(retired, self.files["test/unit/github/test_values.py"])
        self.write("openmachine/old.py", "VALUE = 1\n")
        self.manifest["modules"]["old"] = {"sources": ["openmachine/old.py"], "tests": [retired], "boundary_tests": []}
        self.save_manifest()
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        for path in (retired, "openmachine/old.py"):
            (self.root / path).unlink()
        del self.manifest["modules"]["old"]
        self.save_manifest()
        self.write("openmachine/github.py", "VALUE = 2\n")
        self.assertIn("test/unit/github/test_values.py", runner.make_plan(self.root, self.base)["tests"])

    def test_retiring_a_whole_owner_needs_no_coverage_for_what_it_deleted(self):
        (self.root / "openmachine/execution.py").unlink()
        (self.root / "test/resilience/execution/test_process.py").unlink()
        del self.manifest["modules"]["execution"]
        self.save_manifest()
        self.write("openmachine/github.py", "VALUE = 2\n")
        plan = runner.make_plan(self.root, self.base)
        self.assertEqual(self.product_tests(plan), {"test/unit/github/test_values.py", "test/component/github/test_calls.py",
                                                    "test/component/server/test_dispatch.py"})
        # Anything the retired owner left behind is unowned, not retired.
        self.write("openmachine/execution.py", "VALUE = 1\n")
        self.assertEqual(runner.make_plan(self.root, self.base)["scope"], "full")

    def test_manifest_only_addition_selects_that_owner_without_a_full_run(self):
        path = "test/unit/github/test_more.py"
        self.write(path, self.files["test/unit/github/test_values.py"])
        self.manifest["modules"]["github"]["tests"].append(path)
        self.save_manifest()
        plan = runner.make_plan(self.root, self.base)
        self.assertEqual(plan["scope"], "affected")
        self.assertIn(path, plan["tests"])
        self.assertNotIn("test/resilience/execution/test_process.py", plan["tests"])

    def test_support_consumer_removal_selects_old_and_new_consumers(self):
        path = "test/resilience/execution/test_process.py"
        self.manifest["support"]["test/support/wire.py"].append(path)
        self.save_manifest()
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.manifest["support"]["test/support/wire.py"].remove(path)
        self.save_manifest()
        plan = runner.make_plan(self.root, self.base)
        self.assertIn(path, plan["tests"])
        self.assertIn("test/component/server/test_dispatch.py", plan["tests"])

    def test_missing_transitive_fixture_consumer_is_rejected_before_selection(self):
        helper = "test/support/shared.py"
        consumer = "test/component/server/test_dispatch.py"
        self.write(helper, '"""Shared helper."""\n')
        self.write("test/support/wire.py", "from test.support import shared\n")
        self.write(consumer, "from test.support import wire\n" + self.files[consumer])
        self.manifest["support"][helper] = ["test/component/github/test_calls.py"]
        self.save_manifest()
        plan = runner.make_plan(self.root, self.base)
        self.assertIn(consumer, plan["tests"])

    def test_unknown_duplicate_stale_and_empty_ownership_fail_closed(self):
        original = copy.deepcopy(self.manifest)
        invalid = []
        value = copy.deepcopy(original)
        value["modules"]["server"]["sources"].append("openmachine/github.py")
        invalid.append(value)
        value = copy.deepcopy(original)
        value["modules"]["github"]["tests"].append("test/unit/github/test_missing.py")
        invalid.append(value)
        value = copy.deepcopy(original)
        value["modules"]["github"]["sources"] = ["openmachine/*.py"]
        invalid.append(value)
        value = copy.deepcopy(original)
        value["modules"]["github"]["tests"] = []
        value["modules"]["github"]["boundary_tests"] = []
        invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    runner.validate_manifest(value, runner.inventory(self.root), self.root)

    def test_missing_wrong_and_empty_bases_never_become_an_implicit_full_pass(self):
        with self.assertRaisesRegex(ValueError, "not available locally"):
            runner.make_plan(self.root, "missing-base")
        with self.assertRaisesRegex(ValueError, "no changes"):
            runner.make_plan(self.root, self.base, committed=True)
        with self.assertRaisesRegex(ValueError, "unknown test module"):
            runner.make_plan(self.root, modules=["missing"])
        with self.assertRaisesRegex(ValueError, "no required behavior tests"):
            runner.make_plan(self.root, modules=["github"], layer="resilience")
        self.assertEqual(runner.make_plan(self.root, modules=["github"], layer="unit")["environment"], "source")
        self.assertEqual(runner.make_plan(self.root, modules=["github"], layer="unit")["scope"], "focused")
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            runner.make_plan(self.root, full=True, layer="unit")

    def test_release_version_bump_verifies_the_complete_candidate(self):
        self.manifest["modules"]["server"]["sources"].append("openmachine/__init__.py")
        self.save_manifest()
        self.write("openmachine/__init__.py", '__version__ = "1.0.0"\n')
        self.commit()
        release_base = self.git("rev-parse", "HEAD")
        self.write("openmachine/__init__.py", '__version__ = "1.0.1"\n')
        plan = runner.make_plan(self.root, release_base)
        self.assertEqual(plan["scope"], "full")
        self.assertIn("global verification or dependency change: openmachine/__init__.py", plan["full_reasons"])
        self.assertEqual(self.product_tests(plan), {
            "test/unit/github/test_values.py", "test/component/github/test_calls.py",
            "test/resilience/execution/test_process.py", "test/component/server/test_dispatch.py",
        })

    def test_first_manifest_migration_is_explicitly_reported_as_full(self):
        self.git("rm", "test/ownership.json")
        self.commit()
        base_without_manifest = self.git("rev-parse", "HEAD")
        self.save_manifest()
        plan = runner.make_plan(self.root, base_without_manifest)
        self.assertEqual(plan["scope"], "full")
        self.assertTrue(any("predates" in reason for reason in plan["full_reasons"]))


    def test_zero_test_collection_is_an_error_not_a_successful_unittest_exit(self):
        path = "test/unit/github/test_empty.py"
        self.write(path, '"""No cases."""\n')
        before_path = sys.path[:]
        before_test = sys.modules.get("test")
        try:
            with self.assertRaisesRegex(ValueError, "zero tests"):
                runner.run_files(self.root, [path], installed=False)
        finally:
            sys.path[:] = before_path
            if before_test is None:
                sys.modules.pop("test", None)
            else:
                sys.modules["test"] = before_test

    def test_installed_loader_excludes_checkout_while_source_unit_loader_uses_it(self):
        path = "test/unit/github/test_values.py"
        self.write("openmachine/__init__.py", 'VALUE = "source"\n')
        with tempfile.TemporaryDirectory(prefix="om-installed-fixture-") as directory:
            installed = Path(directory)
            (installed / "openmachine").mkdir()
            (installed / "openmachine" / "__init__.py").write_text('VALUE = "installed"\n')
            for source in (False, True):
                expected = "source" if source else "installed"
                self.write(path, "import unittest\nimport openmachine\n\n"
                           "class Candidate(unittest.TestCase):\n"
                           "    def test_candidate(self):\n"
                           f"        self.assertEqual(openmachine.VALUE, {expected!r})\n")
                code = (
                    "import importlib.util,sys\nfrom pathlib import Path\n"
                    f"sys.path[:0] = {[str(self.root), str(installed)]!r}\n"
                    f"spec=importlib.util.spec_from_file_location('runner', {str(ROOT / 'scripts/run_tests.py')!r})\n"
                    "runner=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(runner)\n"
                    f"runner.run_files(Path({str(self.root)!r}), [{path!r}], installed={not source!r}, source={source!r})\n")
                result = subprocess.run([sys.executable, "-I", "-c", code],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
