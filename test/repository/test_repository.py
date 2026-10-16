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
    def test_valid_titles(self):
        cases = [
            ("commit", "governance(repo): establish a minimal scaffold"),
            ("pr", "ci(repository): check contribution metadata"),
            ("issue", "[SPEC] architecture(runtime): define the first behavior"),
            ("issue", "[REQUEST] runtime(workspace): support path exclusions"),
            ("issue", "[BUG] runtime(connection): handle a failed retry"),
            ("issue", "[BUG] \u6587\u4ef6\u65e0\u6cd5\u6253\u5f00"),
            ("issue", "[REQUEST] \u652f\u6301\u66f4\u591a\u5de5\u4f5c\u533a"),
            ("issue", "[SURVEY] research(transport): compare current options"),
            ("issue", "[SURVEY] experiment(latency): measure overhead"),
            ("issue", "[BENCHMARK] eval(transport): compare frozen candidates"),
            ("issue", "[ROADMAP] Repository foundation"),
        ]
        for kind, title in cases:
            with self.subTest(title=title):
                self.assertEqual(rules.title_errors(title, kind), [])

    def test_invalid_titles(self):
        cases = [
            ("commit", "[SPEC] docs(repo): describe the rules"),
            ("pr", " "),
            ("pr", " docs(repo): explain the contribution"),
            ("pr", "docs(repo): explain the contribution."),
            ("pr", "x" * 101),
            ("issue", "[RESEARCH] research(repo): investigate governance"),
            ("issue", "[Program] Repository foundation"),
            ("issue", "[PROGRAM] Repository foundation"),
            ("issue", "[Roadmap] Repository foundation"),
            ("issue", "[ROADMAP]  Repository foundation"),
            ("issue", "[REQUEST]  runtime(repo): add a new capability"),
            ("issue", "[Request] runtime(repo): add a new capability"),
            ("issue", "[BUG]  runtime(connection): handle a failed retry"),
            ("issue", "[Bug] runtime(connection): handle a failed retry"),
            ("issue", "[SURVEY]  research(repo): investigate governance"),
            ("issue", "[SURVEY] governance(repo): change the rules"),
            ("issue", "[BENCHMARK] research(repo): compare candidates"),
            ("commit", "eval(repo): compare candidates"),
            ("commit", "docs(repo): update"),
            ("commit", "docs(repo): a change."),
            ("commit", "docs(repo): a change\nsecond line"),
            ("commit", "docs(repo): " + "a" * 73),
            ("pr", "docs(repo): \u4e2d\u6587"),
        ]
        for kind, title in cases:
            with self.subTest(title=title):
                self.assertTrue(rules.title_errors(title, kind))

    def test_nonconventional_pr_title_warns_without_blocking(self):
        for title in ("Update README.md", "[SURVEY] research(repo): investigate governance",
                      "docs(repo): update"):
            with self.subTest(title=title):
                obj = {"title": title, "body": "Explain the change, verification, and limitations."}
                self.assertEqual(rules.metadata_errors(obj, "pr"), [])
                self.assertIn("presentation: consider domain(scope): concrete English outcome",
                              rules.metadata_warnings(obj, "pr"))
        conventional = {"title": "docs(readme): clarify setup instructions",
                        "body": (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text()}
        self.assertEqual(rules.metadata_warnings(conventional, "pr"), [])
        self.assertTrue(rules.title_errors("Update README.md", "commit"))
        self.assertTrue(rules.title_errors("Update README.md", "issue"))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps({"pull_request": {
                "title": "Update README.md",
                "body": "Explain the change, verification, and limitations.",
            }}))
            result, output, errors = self.checker([
                "--mode", "metadata", "--event-name", "pull_request", "--event-path", str(path)])
            self.assertEqual(result, 0)
            self.assertIn("PASS: repository checks", output)
            self.assertIn("WARN: presentation: consider domain(scope):", errors)

    def test_language_backstop_does_not_claim_ascii_is_english(self):
        self.assertEqual(rules.english_errors("English prose with a caf\u00e9 name and an em dash\u2014fine."), [])
        for text in ["\u4e2d\u6587", "\u30ab\u30ca", "\ud55c\uae00"]:
            self.assertTrue(rules.english_errors(text))

    def test_label_manifest(self):
        labels = json.loads((ROOT / ".github/labels.json").read_text())
        self.assertEqual(rules.label_errors(labels), [])
        self.assertEqual(sum(label["name"] == "feature-request" for label in labels), 1)
        for bad in [{}, [], labels + [labels[0]], [{"name": "bug"}],
                    [{"name": "Bug", "color": "ffffff", "description": "A bug."}],
                    [{"name": "bug", "color": "red", "description": "A bug."}],
                    [{"name": "bug", "color": "ffffff", "description": "\u4e2d\u6587"}]]:
            with self.subTest(bad=bad):
                self.assertTrue(rules.label_errors(bad))

    def test_text_hygiene_and_relative_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target.md").write_text("# Target\n")
            page = root / "README.md"
            page.write_text("[Target](target.md#anchor) and [Web](https://example.com)\n")
            self.assertEqual(rules.file_errors(page, root), [])
            for content in ["[Missing](missing.md)\n", "[Outside](../outside.md)\n",
                            "trailing space \n", "no final newline", "\ufeffBOM\n",
                            "\u4e2d\u6587\n", "Windows\r\n"]:
                page.write_bytes(content.encode("utf-8"))
                self.assertTrue(rules.file_errors(page, root), repr(content))

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

    def test_issue_templates_match_scoped_metadata_policy(self):
        templates = list((ROOT / ".github/ISSUE_TEMPLATE").glob("*.md"))
        self.assertEqual(len(templates), 8)
        for path in templates:
            body = path.read_text()
            title = next(line.split('"', 2)[1] for line in body.splitlines()
                         if line.startswith('title: "'))
            with self.subTest(template=path.name):
                self.assertEqual(rules.metadata_errors({"title": title, "body": body}, "issue"), [])

        for prefix, headings in rules.FORMAL_ISSUE_HEADINGS.items():
            template = next(path.read_text() for path in templates
                            if f'title: "[{prefix}] ' in path.read_text())
            title = next(line.split('"', 2)[1] for line in template.splitlines()
                         if line.startswith('title: "'))
            for heading in headings:
                trimmed = template.replace(f"## {heading}", f"## Omitted {heading}")
                with self.subTest(prefix=prefix, heading=heading):
                    self.assertTrue(rules.metadata_errors({"title": title, "body": trimmed}, "issue"))

        body = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text()
        obj = {"title": "governance(repo): establish a minimal scaffold", "body": body}
        self.assertEqual(rules.metadata_errors(obj, "pr"), [])
        self.assertEqual(rules.metadata_warnings(obj, "pr"), [])
        for candidate in ("A concise change and actual test result.", body + "\n## Testing\n"):
            self.assertEqual(rules.metadata_errors({**obj, "body": candidate}, "pr"), [])
            self.assertTrue(rules.metadata_warnings({**obj, "body": candidate}, "pr"))
        self.assertEqual(rules.metadata_errors(
            {"title": "governance(repo): propose a scoped task", "body": "Short task."}, "issue"), [])

    def test_community_issues_support_unicode_and_short_reports(self):
        for title, body in (
            ("[BUG] \u6587\u4ef6\u65e0\u6cd5\u6253\u5f00", "\u91cd\u73b0\u6b65\u9aa4：\u8fd0\u884c\u540e\u51fa\u73b0\u9519\u8bef\u3002"),
            ("[REQUEST] \u652f\u6301\u66f4\u591a\u5de5\u4f5c\u533a", "\u5e0c\u671b\u652f\u6301\u989d\u5916\u7684\u5de5\u4f5c\u76ee\u5f55\u3002"),
            ("[BUG] Connection fails", "Observed an error on startup."),
        ):
            with self.subTest(title=title):
                self.assertEqual(rules.metadata_errors({"title": title, "body": body}, "issue"), [])
        self.assertTrue(rules.metadata_errors({"title": "[SPEC] architecture(api): add fields",
                                              "body": "Too short"}, "issue"))
        self.assertTrue(rules.metadata_errors({"title": "[BUG] Short report",
                                              "body": "A private URL " + "https://chatgpt.com/" + "c/secret"}, "issue"))

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

    def test_historical_program_events_keep_editability(self):
        issue = {"title": "[Program] Existing roadmap",
                 "body": (ROOT / ".github/ISSUE_TEMPLATE/roadmap.md").read_text()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            for action, expected in (("edited", 0), ("reopened", 0), ("opened", 1)):
                path.write_text(json.dumps({"action": action, "issue": issue}))
                with self.subTest(action=action), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(rules.main(["--mode", "metadata", "--event-name", "issues",
                                                 "--event-path", str(path)]), expected)
        self.assertEqual(issue["title"], "[Program] Existing roadmap")

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

    def test_metadata_keeps_title_language_and_nonempty_body_checks(self):
        obj = {"title": "docs(repo): explain the bounded change", "body": "Actual change and validation."}
        for body in (None, "", " \t\r\n ", [], "Embedded \0 control", "\u4e2d\u6587"):
            with self.subTest(body=body):
                self.assertTrue(rules.metadata_errors({**obj, "body": body}, "pr"))
        for title in (None, 3, "", " invalid", "Invalid title.", "x" * 101):
            with self.subTest(title=title):
                self.assertTrue(rules.metadata_errors({**obj, "title": title}, "pr"))
        self.assertTrue(rules.metadata_errors([], "pr"))
        self.assertEqual(rules.metadata_errors(obj, "pr"), [])

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
                source.write_text("# Failed source \n")
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

    def test_source_mode_ignores_pr_event_payload_but_retains_push_commit_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github").mkdir()
            (root / ".github/labels.json").write_text(json.dumps([
                {"name": "maintenance", "color": "ffffff", "description": "Repository maintenance."}]))
            event = root / "event.json"
            event.write_text("not JSON")
            arguments = ["--mode", "source", "--event-name", "pull_request", "--event-path", str(event)]
            with patch.object(rules, "ROOT", root), patch.object(rules, "git", return_value=""), \
                    patch.object(rules, "commit_errors", return_value=["invalid final commit"]) as audit:
                self.assertEqual(self.checker(arguments)[0], 0)
                audit.assert_not_called()
                event.write_text(json.dumps({"before": "a" * 40, "after": "b" * 40}))
                arguments[3] = "push"
                self.assertEqual(self.checker(arguments)[0], 1)
                audit.assert_called_once_with(root, "a" * 40, "b" * 40)
                audit.reset_mock()
                self.assertEqual(self.checker(arguments[2:])[0], 1)
                audit.assert_called_once_with(root, "a" * 40, "b" * 40)

    def test_workflow_modes_preserve_code_and_metadata_check_identity(self):
        quality = (ROOT / ".github/workflows/quality.yml").read_text()
        metadata = (ROOT / ".github/workflows/metadata.yml").read_text()
        self.assertIn("python3 scripts/check_repository.py --mode source", quality)
        self.assertIn("python3 scripts/check_repository.py --mode metadata", metadata)
        self.assertIn("python3 scripts/premerge.py --ci --force", quality)
        self.assertIn("types: [opened, synchronize, edited, reopened, ready_for_review]", metadata)
        # Required contexts must keep their names and run for metadata-only edits,
        # otherwise branch protection can wait forever for a status not emitted.
        self.assertIn("types: [opened, synchronize, reopened, edited, ready_for_review]", quality)
        self.assertIn("  repository:\n    name: repository\n", quality)
        self.assertIn("  runtime:\n    needs: repository\n    name: runtime\n", quality)
        self.assertNotIn("\n    if:", quality)
        self.assertNotIn("-metadata-edit", quality)
        self.assertNotIn("    name: ${{", quality)
        self.assertIn("github.event.changes.base && 'metadata' || 'code'", quality)
        self.assertIn("group: metadata-", metadata)
        for workflow in (quality, metadata):
            self.assertIn("persist-credentials: false", workflow)
            self.assertNotIn("pull_request_target:", workflow)
            self.assertNotIn(": write", workflow)

    def test_codeowners_routes_all_paths_to_core_maintainers(self):
        lines = (ROOT / '.github/CODEOWNERS').read_text().splitlines()
        rules = [line.strip() for line in lines
                 if line.strip() and not line.lstrip().startswith('#')]
        self.assertEqual(rules, ["* @" + "OpenMachineLabs/" + "core-maintainers"])

    def test_all_workflows_use_explicit_github_hosted_runners(self):
        expected_jobs = {"quality.yml": 2, "metadata.yml": 1,
                         "spec-development.yml": 1, "labels.yml": 1,
                         "documentation.yml": 2, "npm-package.yml": 1,
                         "auto-metadata.yml": 1, "review-policy-signal.yml": 1,
                         "trusted-review-gate.yml": 1}
        workflows = {path.name: path.read_text()
                     for path in (ROOT / ".github/workflows").glob("*.yml")}
        self.assertEqual(set(workflows), set(expected_jobs))
        for name, expected_count in expected_jobs.items():
            with self.subTest(workflow=name):
                workflow = workflows[name]
                runner = "macos-15" if name == "npm-package.yml" else "ubuntu-24.04"
                self.assertEqual(workflow.count("    runs-on: " + runner), expected_count)
                self.assertNotIn("CORE_CI_RUNNER", workflow)
                self.assertNotIn("runs-on: " + chr(36) + "{{", workflow)
                if name == "npm-package.yml":
                    self.assertIn("workflow_dispatch:", workflow)
                    self.assertIn("test \"$(uname -m)\" = \"arm64\"", workflow)
                    self.assertNotIn("npm publish", workflow)

    def test_metadata_is_data_not_executable_code(self):
        body = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text()
        obj = {"title": "docs(repo): describe $(false) literally", "body": body + "\n`exit 1`\n"}
        self.assertEqual(rules.metadata_errors(obj, "pr"), [])

    def test_commit_audit_is_prospective_and_rejects_invalid_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", "-C", directory, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git("init", "-q")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit",
                "--allow-empty", "-qm", "Initial commit")
            base = git("rev-parse", "HEAD")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit",
                "--allow-empty", "-qm", "docs(repo): establish contribution rules")
            head = git("rev-parse", "HEAD")
            self.assertEqual(rules.commit_errors(root, base, head), [])
            self.assertTrue(rules.commit_errors(root, "--all", head))
            self.assertTrue(rules.commit_errors(root, "0" * 40, head))
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit",
                "--allow-empty", "-qm", "Invalid new subject")
            self.assertTrue(rules.commit_errors(root, base, git("rev-parse", "HEAD")))


if __name__ == "__main__":
    unittest.main()
