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
            ("issue", "[SURVEY] research(transport): compare current options"),
            ("issue", "[SURVEY] experiment(latency): measure overhead"),
            ("issue", "[BENCHMARK] eval(transport): compare frozen candidates"),
            ("issue", "[Program] Repository foundation"),
        ]
        for kind, title in cases:
            with self.subTest(title=title):
                self.assertEqual(rules.title_errors(title, kind), [])

    def test_invalid_titles(self):
        cases = [
            ("commit", "[SPEC] docs(repo): describe the rules"),
            ("pr", "[SURVEY] research(repo): investigate governance"),
            ("issue", "[RESEARCH] research(repo): investigate governance"),
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

    def test_language_backstop_does_not_claim_ascii_is_english(self):
        self.assertEqual(rules.english_errors("English prose with a caf\u00e9 name and an em dash\u2014fine."), [])
        for text in ["\u4e2d\u6587", "\u30ab\u30ca", "\ud55c\uae00"]:
            self.assertTrue(rules.english_errors(text))

    def test_label_manifest(self):
        labels = json.loads((ROOT / ".github/labels.json").read_text())
        self.assertEqual(rules.label_errors(labels), [])
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

    def test_issue_sections_remain_required_and_pr_sections_are_advisory(self):
        templates = list((ROOT / ".github/ISSUE_TEMPLATE").glob("*.md"))
        self.assertEqual(len(templates), 6)
        for path in templates:
            self.assertEqual(rules.metadata_errors({"title": "governance(repo): propose a scoped task",
                                                    "body": path.read_text()}, "issue"), [])
        body = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text()
        obj = {"title": "governance(repo): establish a minimal scaffold", "body": body}
        self.assertEqual(rules.metadata_errors(obj, "pr"), [])
        self.assertEqual(rules.metadata_warnings(obj, "pr"), [])
        compact = {**obj, "body": "Describe the bounded change, its checks, and the remaining limitation."}
        duplicate = {**obj, "body": body + "\n## Validation\n"}
        for candidate in (compact, duplicate):
            self.assertEqual(rules.metadata_errors(candidate, "pr"), [])
            self.assertTrue(rules.metadata_warnings(candidate, "pr"))
        legacy = {"title": obj["title"], "body": body + "\n## Classification\n\n## Traceability\n"
                  "\n## Project metadata\n\n## Review guide\n"}
        self.assertEqual(rules.metadata_errors(legacy, "pr"), [])
        self.assertEqual(rules.metadata_warnings(legacy, "pr"), [])
        self.assertTrue(rules.metadata_errors(compact, "issue"))

    def test_metadata_keeps_title_language_and_nonempty_body_checks(self):
        obj = {"title": "docs(repo): explain the bounded change", "body": "Actual change and validation."}
        for body in (None, "", " \t\r\n ", [], "Embedded \0 control", "\u4e2d\u6587"):
            with self.subTest(body=body):
                self.assertTrue(rules.metadata_errors({**obj, "body": body}, "pr"))
        for title in (None, 3, "Invalid title"):
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
        self.assertIn("'repository-metadata-edit' || 'repository'", quality)
        self.assertIn("'runtime-metadata-edit' || 'runtime'", quality)
        self.assertEqual(quality.count("if: github.event.action != 'edited' || github.event.changes.base"), 2)
        self.assertIn("github.event.changes.base && 'metadata' || 'code'", quality)
        self.assertIn("group: metadata-", metadata)
        for workflow in (quality, metadata):
            self.assertIn("persist-credentials: false", workflow)
            self.assertNotIn("pull_request_target:", workflow)
            self.assertNotIn(": write", workflow)

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
