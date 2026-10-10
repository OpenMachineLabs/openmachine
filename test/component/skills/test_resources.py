"""Tests for skills behavior at the component layer."""

import hashlib
import os
import unittest
from unittest.mock import patch

import yaml

from openmachine import skills
from openmachine.foundation.files import MAX_TEXT_FILE, TEXT_PAGE
from openmachine.foundation.sanitize import Fault
from openmachine.skills import catalog, reader
from test.support.continuity_cases import ContinuityCaseFixture


class ContinuityTests(ContinuityCaseFixture, unittest.TestCase):
    def test_bundled_worktree_reference_is_packaged_listed_and_readable(self):
        main = skills.inspect(self.config, "read")
        self.assertEqual(main["skill"], "om.overview")
        self.assertFalse(main["has_more"])
        self.assertLessEqual(main["size_bytes"], TEXT_PAGE)
        self.assertEqual(main["snapshot"], hashlib.sha256(main["text"].encode()).hexdigest())
        self.assertEqual(main, skills.inspect(self.config, "read", skills.BUNDLED))
        listed = skills.inspect(self.config, "files", limit=1)
        self.assertEqual(listed["entries"][0]["name"], "SKILL.md")
        entries = listed["entries"]
        while listed["next_offset"] is not None:
            listed = skills.inspect(self.config, "files", offset=listed["next_offset"], limit=1,
                                    expected_snapshot=listed["snapshot"])
            entries.extend(listed["entries"])
        self.assertIn({"name": "worktrees.md", "directory": False, "symlink": False}, entries)
        reference = skills.inspect(self.config, "read", path="worktrees.md")
        self.assertIn("Choose and reuse a worktree", reference["text"])
        self.assertIn("create_worktree", reference["text"])
        self.assertEqual(reference["path"], "worktrees.md")
        self.assertEqual(reference["skill"], "om.workspace")
        self.assertEqual(reference, skills.inspect(self.config, "read", skills.BUNDLED, "worktrees.md"))
        self.fault("source_changed", skills.inspect, self.config, "read", path="worktrees.md",
                   expected_snapshot=main["snapshot"])
        for path in ("../worktrees.md", "/worktrees.md", "missing.md", "missing/file.md"):
            with self.subTest(path=path):
                self.fault("path_denied", skills.inspect, self.config, "read", path=path)

    def test_skills_are_explicit_live_and_directory_links_are_deduplicated(self):
        self.assertEqual(self.config.skills.roots, [])
        _, selected = self.skill_fixture()
        listed = skills.inspect(self.config)
        self.assertEqual(len(listed["skills"]), 9)
        self.assertIn("om.environment", {item["skill"] for item in listed["skills"]})
        self.assertTrue(listed["warnings"])
        source = str(selected / "SKILL.md")
        first = skills.inspect(self.config, "read", source)
        self.assertIn("Original instructions", first["text"])
        (selected / "SKILL.md").write_text("Updated instructions.\n")
        second = skills.inspect(self.config, "read", source)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["text"], "Updated instructions.\n")
        self.fault("source_changed", skills.inspect, self.config, "read", source, expected_snapshot=first["snapshot"])
        self.assertIn("Operate Open Machine", skills.inspect(self.config, "read")["text"])

    def test_skill_resource_files_dotfiles_names_and_escape_are_explicit(self):
        collection, selected = self.skill_fixture()
        other = collection / "other"
        other.mkdir()
        (other / "SKILL.md").write_text("---\nname: format\n---\nAnother source.\n")
        named = skills.inspect(self.config, query="format")["skills"]
        self.assertEqual(len(named), 2)
        self.assertEqual(len({item["skill"] for item in named}), 2)
        source = str(selected / "SKILL.md")
        text = skills.inspect(self.config, "read", source, ".github/policy.md")
        self.assertIn("Useful dotfile", text["text"])
        first = skills.inspect(self.config, "files", source)
        self.assertIn(".github", [item["name"] for item in first["entries"]])
        (selected / "more.md").write_text("new resource")
        self.fault("source_changed", skills.inspect, self.config, "files", source, expected_snapshot=first["snapshot"])
        (selected / "escape.md").symlink_to(self.root / "outside.md")
        (self.root / "outside.md").write_text("outside")
        for path in ("../outside.md", ".env", "escape.md"):
            with self.subTest(path=path), self.assertRaises((Fault, OSError)):
                skills.inspect(self.config, "read", source, path)
        self.fault("skill_unavailable", skills.inspect, self.config, "read", source, "missing/file.md")

    def test_builtin_metadata_generates_catalog_and_default_navigation(self):
        listed = skills.inspect(self.config)
        identities = {item["skill"] for item in listed["skills"]}
        self.assertEqual(identities, {f"om.{name}" for name in
                                    ("overview", "workspace", "execution", "tasks", "github", "browser", "service", "environment")})
        self.assertEqual([item["skill"] for item in skills.inspect(self.config, query="om.tasks")["skills"]], ["om.tasks"])
        first = skills.inspect(self.config, limit=2)
        second = skills.inspect(self.config, offset=first["next_offset"], limit=2,
                                expected_snapshot=first["snapshot"])
        self.assertEqual(first["skills"] + second["skills"], listed["skills"][:4])
        main = skills.inspect(self.config, "read")
        for item in listed["skills"]:
            self.assertIn(item["skill"], main["text"])
            self.assertIn(item["name"], main["text"])
            self.assertIn(" ".join(item["description"].splitlines()), main["text"])
            self.assertEqual(skills.inspect(self.config, "read", item["source"])["skill"], item["skill"])

    def test_tool_selection_is_exact_and_does_not_scan_external_sources(self):
        self.skill_fixture()
        with patch.object(catalog, "discover", side_effect=AssertionError("unexpected external discovery")):
            result = skills.inspect(self.config, "read", tool="create_worktree")
            listed = skills.inspect(self.config, tool="create_worktree")
            skills.inspect(self.config, "read", "om.workspace")
        self.assertEqual((result["skill"], result["path"]), ("om.workspace", "worktrees.md"))
        self.assertEqual(result["discovery"], "bundled_tool_binding")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["skills"][0]["path"], "worktrees.md")
        self.assertEqual(result, skills.inspect(self.config, "read", "om.workspace", "worktrees.md",
                                                tool="create_worktree"))
        for kwargs in ({"skill": "om.execution"}, {"path": "files.md"}):
            self.fault("invalid_skill_selector", skills.inspect, self.config, "read", tool="create_worktree", **kwargs)
        self.fault("invalid_skill_selector", skills.inspect, self.config, "files", tool="create_worktree")
        self.fault("skill_unavailable", skills.inspect, self.config, "read", tool="create-worktree")
        self.assertTrue(skills.inspect(self.config)["warnings"])

    def test_each_binding_reads_a_canonical_resource_and_preserves_local_description(self):
        bindings = yaml.safe_load(reader.resource(reader.bundled_root(), "bindings.yaml"))
        for name in bindings:
            with self.subTest(tool=name):
                binding = skills.for_tool(name)
                result = skills.inspect(self.config, "read", tool=name)
                self.assertEqual((result["skill"], result["path"]), (binding["skill"], binding["path"]))
                self.assertTrue(result["text"])
                hint = skills.describe_tool(name, "Keep the call-local restriction.")
                self.assertTrue(hint.startswith("Keep the call-local restriction.\n\n"))
                self.assertIn(f"inspect_skills(action='read', tool='{name}')", hint)
                self.assertLess(len(hint), 180)
        self.fault("skill_unavailable", skills.for_tool, "unknown_tool")

    def test_yaml_metadata_supports_multiline_text_and_warns_on_malformed_fields(self):
        collection, selected = self.skill_fixture()
        raw = ("---\nid: om.workspace\nname: 'format: safely'\ndescription: >-\n  Run bounded checks\n  before editing.\n"
               "license: MIT\nmetadata:\n  owner: example\n---\nBody stays unchanged.\n").encode()
        (selected / "SKILL.md").write_bytes(raw)
        for name, header in (("bad-description", "description: [not, text]"),
                             ("bad-yaml", "name: [unterminated"),
                             ("bad-id", "id: {nested: object}")):
            candidate = collection / name
            candidate.mkdir()
            (candidate / "SKILL.md").write_text(f"---\n{header}\n---\nBody\n")
        listed = skills.inspect(self.config)
        source = str(selected / "SKILL.md")
        item = next(item for item in listed["skills"] if item["skill"] == source)
        self.assertEqual(item["name"], "format: safely")
        self.assertEqual(item["description"], "Run bounded checks before editing.")
        self.assertEqual(sum(warning["code"] == "invalid_skill_metadata" for warning in listed["warnings"]), 3)
        self.assertFalse(listed["complete"])
        result = skills.inspect(self.config, "read", source)
        self.assertEqual(result["text"].encode(), raw)
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        (selected / "SKILL.md").write_text("---\nname: format\ndescription: |\n  First line.\n  Second line.\n---\nBody\n")
        literal = next(item for item in skills.inspect(self.config)["skills"] if item["skill"] == source)
        self.assertEqual(literal["description"], "First line.\nSecond line.")
        self.fault("source_changed", skills.inspect, self.config, expected_snapshot=listed["snapshot"])

    def test_external_pages_retain_raw_hash_utf8_redaction_and_size_guards(self):
        _, selected = self.skill_fixture()
        source = str(selected / "SKILL.md")
        raw = b"password=do-not-expose\n" + ("safe\N{LATIN SMALL LETTER E WITH ACUTE}" * 8000).encode()
        (selected / "pages.md").write_bytes(raw)
        first = skills.inspect(self.config, "read", source, "pages.md")
        self.assertTrue(first["has_more"])
        self.assertTrue(first["content_redacted"])
        self.assertNotIn("do-not-expose", first["text"])
        self.assertLessEqual(len(first["text"].encode()), TEXT_PAGE)
        self.assertEqual(first["sha256"], hashlib.sha256(raw).hexdigest())
        second = skills.inspect(self.config, "read", source, "pages.md", offset=first["next_offset"],
                                expected_snapshot=first["snapshot"])
        self.assertFalse(second["has_more"])
        self.assertEqual(second["next_offset"], len(raw))
        self.assertEqual(first["text"] + second["text"], raw.decode().replace("do-not-expose", "[REDACTED]"))
        (selected / "large.md").write_bytes(b"x" * (MAX_TEXT_FILE + 1))
        self.fault("source_limit", skills.inspect, self.config, "read", source, "large.md")
        (selected / "invalid.md").write_bytes(b"\xff")
        self.fault("invalid_file", skills.inspect, self.config, "read", source, "invalid.md")
        os.link(selected / "pages.md", selected / "hardlink.md")
        self.fault("invalid_file", skills.inspect, self.config, "read", source, "hardlink.md")


if __name__ == "__main__":
    unittest.main()
