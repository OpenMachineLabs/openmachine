"""Focused positive and rejection tests for repository checks."""

import importlib.util
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_repository", ROOT / "scripts/check_repository.py")
rules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rules)


class RepositoryChecks(unittest.TestCase):
    def test_metadata_accepts_content_without_presentation_gates(self):
        for title in ['Repair login.', 'fixup! temporary work', '[Custom] A useful report',
                      '\u4fee\u590d\u767b\u5f55', 'A' * 120]:
            for kind in ('pr', 'issue'):
                obj = {'title': title, 'body': 'A concise explanation without fixed headings.',
                       'labels': [{'name': 'spec'}]}
                self.assertEqual(rules.metadata_errors(obj, kind), [])
        obj = {'title': 'Update', 'body': '\u4e2d\u6587 path example'}
        self.assertEqual(rules.metadata_errors(obj, 'pr'), [])
        for bad in [None, '', '  ', 5, 'line\nbreak']:
            self.assertTrue(rules.metadata_errors({**obj, 'title': bad}, 'pr'))
        for bad in [None, '', ' \n', 5, 'bad\0control']:
            self.assertTrue(rules.metadata_errors({**obj, 'body': bad}, 'pr'))

    def test_unicode_data_and_style_do_not_hide_broken_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / 'README.md'
            (root / 'target.md').write_text('# Target\n')
            for text in ['[Target](target.md)\n', 'Unicode: \u4e2d\u6587',
                         'trailing whitespace \n', 'Windows\r\n', '\ufeffBOM\n']:
                page.write_bytes(text.encode())
                self.assertEqual(rules.file_errors(page, root), [])
            for text in ['[Missing](missing.md)\n', '[Outside](../outside.md)\n']:
                page.write_text(text)
                self.assertTrue(rules.file_errors(page, root))

    def test_label_manifest_keeps_api_shape_and_unique_names(self):
        labels = json.loads((ROOT / '.github/labels.json').read_text())
        self.assertEqual(rules.label_errors(labels), [])
        for bad in [{}, [], labels + [labels[0]], [{'name': 'bug'}],
                    [{'name': 'bug', 'color': 'red', 'description': 'Bug'}]]:
            self.assertTrue(rules.label_errors(bad))


    def test_public_boundary_guards_public_source_and_new_metadata(self):
        allowed = (
            "https://github.com/OpenMachineLabs/openmachine/issues/2",
            "https://github.com/OpenMachineLabs/openmachine.git",
            "https://github.com/desktop/dugite-native",
            "Core can support an independent desktop client.",
        )
        forbidden = (
            "OpenMachineLabs/" + "internal-client/issues/7",
            "https://github.com/OpenMachineLabs/" + "internal-service/pull/9",
            "https://chatgpt.com/" + "c/01234567-abcd-ef01-2345-0123456789ab",
            "https://chat.openai.com/" + "share/01234567-abcd-ef01-2345-0123456789ab",
            "https://claude.ai/" + "chat/01234567-abcd-ef01-2345-0123456789ab",
            "https://claude.ai/" + "share/01234567-abcd-ef01-2345-0123456789ab",
            "https://gemini.google.com/" + "app/0123456789abcdef",
            "https://gemini.google.com/" + "share/0123456789ab",
        )
        for text in allowed:
            with self.subTest(text=text):
                self.assertEqual(rules.public_boundary_errors(text), [])
        for text in forbidden:
            with self.subTest(text=text):
                self.assertTrue(rules.public_boundary_errors(text))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "README.md"
            for text in allowed + forbidden:
                page.write_text(text + "\n")
                with self.subTest(file=text):
                    self.assertEqual(bool(rules.file_errors(page, root)), text in forbidden)
        for kind in ("pr", "issue"):
            title = "docs(repo): describe the scope"
            body = "Describe the scoped change and its checks.\n"
            self.assertEqual(rules.metadata_errors({"title": title, "body": body}, kind), [])
            for text in forbidden:
                with self.subTest(kind=kind, field="body", text=text):
                    self.assertTrue(rules.metadata_errors(
                        {"title": title, "body": body + text}, kind
                    ))
                with self.subTest(kind=kind, field="title", text=text):
                    self.assertTrue(rules.metadata_errors(
                        {"title": title + " " + text, "body": body}, kind
                    ))


    def test_feature_request_template_has_only_one_label_and_no_type_field(self):
        template = (ROOT / ".github/ISSUE_TEMPLATE/feature-request.md").read_text()
        frontmatter = template.split("---", 2)[1]
        self.assertIn('title: "[REQUEST] ', frontmatter)
        self.assertEqual(frontmatter.count("\nlabels:"), 1)
        self.assertIn('\nlabels: "feature-request"\n', frontmatter)
        self.assertNotIn("- Type:", template)
        self.assertNotIn("- Status:", template)

    def test_roadmap_template_keeps_existing_label(self):
        template = (ROOT / ".github/ISSUE_TEMPLATE/roadmap.md").read_text()
        frontmatter = template.split("---", 2)[1]
        self.assertIn('name: "Roadmap"', frontmatter)
        self.assertIn('title: "[ROADMAP] ', frontmatter)
        self.assertEqual(frontmatter.count("\nlabels:"), 1)
        self.assertIn('\nlabels: "feature"\n', frontmatter)


    def test_security_contact_intake_excludes_public_vulnerability_details(self):
        template = (ROOT / ".github/ISSUE_TEMPLATE/security-contact.md").read_text()
        policy = (ROOT / "SECURITY.md").read_text()
        frontmatter = template.split("---", 2)[1]
        self.assertIn('labels: "security"', frontmatter)
        self.assertIn('title: "security(contact):', frontmatter)
        self.assertIn("No security evidence belongs in this public Issue", template)
        self.assertIn("template=security-contact.md", policy)

    def test_bug_template_keeps_single_existing_label(self):
        template = (ROOT / ".github/ISSUE_TEMPLATE/bug.md").read_text()
        frontmatter = template.split("---", 2)[1]
        self.assertIn('title: "[BUG] ', frontmatter)
        self.assertEqual(frontmatter.count("\nlabels:"), 1)
        self.assertIn('\nlabels: "bug"\n', frontmatter)


    def checker(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = rules.main(argv)
        return result, out.getvalue(), err.getvalue()

    def test_source_and_metadata_failures_are_independent_across_body_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            (root / ".github/labels.json").write_text(json.dumps([
                {"name": "maintenance", "color": "ffffff", "description": "Repository maintenance."}]))
            source = root / "README.md"
            source.write_text("# Valid source\n")
            event = root / "event.json"
            obj = {"title": "docs(repo): explain the bounded change", "body": ""}
            event.write_text(json.dumps({"pull_request": obj}))
            environment = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event)}
            with patch.object(rules, "ROOT", root), patch.object(rules, "git", return_value="README.md\0"), \
                    patch.dict(os.environ, environment):
                self.assertEqual(self.checker(["--mode", "source"])[0], 0)
                self.assertEqual(self.checker(["--mode", "metadata"])[0], 1)
                obj["body"] = "Actual change and measured validation; known limitation remains."
                event.write_text(json.dumps({"pull_request": obj}))
                self.assertEqual(self.checker(["--mode", "metadata"])[0], 0)
                self.assertEqual(self.checker(["--mode", "source"])[0], 0)
                source.write_text("[Missing](missing.md)\n")
                self.assertEqual(self.checker(["--mode", "source"])[0], 1)
                obj["body"] += "\nMore review context."
                event.write_text(json.dumps({"pull_request": obj}))
                self.assertEqual(self.checker(["--mode", "metadata"])[0], 0)
                self.assertEqual(self.checker(["--mode", "source"])[0], 1)

    def test_local_metadata_json_preflight_uses_the_ci_validator_without_source_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "pr.json"
            obj = {"title": "docs(repo): explain $(false) literally", "body": "Actual validation and limitations."}
            event.write_text(json.dumps({"pull_request": obj}))
            arguments = ["--mode", "metadata", "--event-name", "pull_request", "--event-path", str(event)]
            with patch.object(rules, "git", side_effect=AssertionError("Metadata must not read source")), \
                    patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": "/absent/event.json"}):
                self.assertEqual(self.checker(arguments)[0], bool(rules.metadata_errors(obj, "pr")))
                obj["body"] = " "
                event.write_text(json.dumps({"pull_request": obj}))
                self.assertEqual(self.checker(arguments)[0], bool(rules.metadata_errors(obj, "pr")))
                for incomplete in (["--mode", "metadata"], arguments[:-2], ["--event-path", str(event)]):
                    with self.subTest(arguments=incomplete), redirect_stderr(io.StringIO()), \
                            self.assertRaises(SystemExit) as failure:
                        rules.main(incomplete)
                    self.assertEqual(failure.exception.code, 2)

    def test_source_mode_never_audits_development_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            (root / ".github/labels.json").write_text(json.dumps([
                {"name": "maintenance", "color": "ffffff", "description": "Maintenance."}]))
            for event in ("push", "pull_request"):
                with patch.object(rules, "ROOT", root), patch.object(rules, "git", return_value="") as git:
                    self.assertEqual(self.checker(["--mode", "source", "--event-name", event,
                                                   "--event-path", "/absent/event.json"])[0], 0)
                    self.assertEqual(git.call_args.args[1], "ls-files")


    def test_metadata_edits_cannot_rerun_or_replace_required_code_checks(self):
        quality = (ROOT / ".github/workflows/quality.yml").read_text()
        metadata = (ROOT / ".github/workflows/metadata.yml").read_text()
        self.assertIn("check_repository.py --mode source", quality)
        self.assertIn("check_repository.py --mode metadata", metadata)
        self.assertIn("if: github.event.action != 'edited' || github.event.changes.base", quality)
        for name in ("repository", "runtime"):
            self.assertIn(f"'{name}-metadata-edit' || '{name}'", quality)
        self.assertIn("'metadata' || 'code'", quality)
        for workflow in (quality, metadata):
            self.assertIn("persist-credentials: false", workflow)
            self.assertNotIn("pull_request_target:", workflow)
            self.assertNotIn(": write", workflow)


    def test_go_connector_runs_only_for_its_module_and_never_for_text_edits(self):
        quality = (ROOT / ".github/workflows/quality.yml").read_text()
        connector = (ROOT / ".github/workflows/connector.yml").read_text()
        self.assertNotIn("go test", quality)
        # Not a required check: a path filter cannot strand a required status.
        self.assertIn("  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\n"
                      "    paths:\n      - \"connector/**\"\n      - \".github/workflows/connector.yml\"\n", connector)
        self.assertIn("  push:\n    branches: [main]\n    paths:", connector)
        self.assertNotIn("edited", connector)
        self.assertIn("  connector:\n    name: connector\n", connector)
        self.assertIn("go test -count=1 -v ./...", connector)
        self.assertIn('CGO_ENABLED: "0"', connector)
        self.assertIn("if: steps.tailnet-bin.outputs.cache-hit != 'true'", connector)
        self.assertIn("persist-credentials: false", connector)
        self.assertNotIn("pull_request_target:", connector)
        self.assertNotIn(": write", connector)

    def test_codeowners_routes_all_paths_to_core_maintainers(self):
        lines = (ROOT / '.github/CODEOWNERS').read_text().splitlines()
        rules = [line.strip() for line in lines
                 if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(rules, ["* @" + "OpenMachineLabs/" + "core-maintainers"])

    def test_all_workflows_use_explicit_github_hosted_runners(self):
        expected_jobs = {"quality.yml": 2, "connector.yml": 1, "metadata.yml": 1,
                         "spec-development.yml": 1, "labels.yml": 1, "runtime-payload.yml": 1,
                         "auto-metadata.yml": 1, "review-policy-signal.yml": 1,
                         "trusted-review-gate.yml": 1}
        workflows = {path.name: path.read_text()
                     for path in (ROOT / ".github/workflows").glob("*.yml")}
        self.assertEqual(set(workflows), set(expected_jobs))
        for name, expected_count in expected_jobs.items():
            with self.subTest(workflow=name):
                workflow = workflows[name]
                runner = "macos-15" if name == "runtime-payload.yml" else "ubuntu-24.04"
                self.assertEqual(workflow.count("    runs-on: " + runner), expected_count)
                self.assertNotIn("CORE_CI_RUNNER", workflow)
                self.assertNotIn("runs-on: " + chr(36) + "{{", workflow)
                if name == "runtime-payload.yml":
                    self.assertIn("workflow_dispatch:", workflow)
                    self.assertIn("test \"$(uname -m)\" = \"arm64\"", workflow)
                    self.assertNotIn("contents: write", workflow)  # It builds; it cannot publish a release.

    def test_metadata_is_data_not_executable_code(self):
        body = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text()
        obj = {"title": "docs(repo): describe $(false) literally", "body": body + "\n`exit 1`\n"}
        self.assertEqual(rules.metadata_errors(obj, "pr"), [])


if __name__ == "__main__":
    unittest.main()
