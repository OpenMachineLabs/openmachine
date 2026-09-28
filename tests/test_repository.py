"""Focused positive and rejection tests for repository checks."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
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

    def test_templates_have_required_sections(self):
        templates = list((ROOT / ".github/ISSUE_TEMPLATE").glob("*.md"))
        self.assertEqual(len(templates), 6)
        for path in templates:
            self.assertEqual(rules.metadata_errors({"title": "governance(repo): propose a scoped task",
                                                    "body": path.read_text()}, "issue"), [])
        body = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text()
        obj = {"title": "governance(repo): establish a minimal scaffold", "body": body}
        self.assertEqual(rules.metadata_errors(obj, "pr"), [])
        self.assertTrue(rules.metadata_errors({**obj, "body": "Missing sections"}, "pr"))
        self.assertTrue(rules.metadata_errors({**obj, "body": body + "\n## Validation\n"}, "pr"))

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
