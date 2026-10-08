"""Tests for skills behavior at the component layer."""

import unittest
from openmachine import skills
from openmachine.foundation.sanitize import Fault
from test.support.continuity_cases import ContinuityCaseFixture


class ContinuityTests(ContinuityCaseFixture, unittest.TestCase):
    def test_bundled_worktree_reference_is_packaged_listed_and_readable(self):
        main = skills.inspect(self.config, "read")
        self.assertIn("(worktrees.md)", main["text"])
        listed = skills.inspect(self.config, "files", limit=1)
        self.assertEqual(listed["entries"][0]["name"], "SKILL.md")
        next_page = skills.inspect(self.config, "files", offset=listed["next_offset"],
                                  expected_snapshot=listed["snapshot"])
        self.assertEqual(next_page["entries"], [{"name": "worktrees.md", "directory": False, "symlink": False}])
        reference = skills.inspect(self.config, "read", path="worktrees.md")
        self.assertIn("Choose and reuse a worktree", reference["text"])
        self.assertIn("create_worktree", reference["text"])
        self.assertEqual(reference["path"], "worktrees.md")
        self.fault("source_changed", skills.inspect, self.config, "read", path="worktrees.md",
                   expected_snapshot=main["snapshot"])
        for path in ("../worktrees.md", "/worktrees.md", "missing.md"):
            with self.subTest(path=path):
                self.fault("path_denied", skills.inspect, self.config, "read", path=path)

    def test_skills_are_explicit_live_and_directory_links_are_deduplicated(self):
        self.assertEqual(self.config.skills.roots, [])
        _, selected = self.skill_fixture()
        listed = skills.inspect(self.config)
        self.assertEqual(len(listed["skills"]), 2)
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


if __name__ == "__main__":
    unittest.main()
