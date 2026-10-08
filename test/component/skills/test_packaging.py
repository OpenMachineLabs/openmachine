"""The installed Skill package contains the canonical authored bytes once."""

from importlib.resources import files
from pathlib import Path
import sys
import unittest

import openmachine


class InstalledSkillTests(unittest.TestCase):
    def test_installed_skills_match_canonical_source_without_a_legacy_copy(self):
        source = Path(__file__).resolve().parents[3] / "openmachine/skills"
        installed = Path(str(files("openmachine.skills")))
        self.assertTrue(installed.resolve().is_relative_to(Path(sys.prefix).resolve()))
        self.assertNotEqual(source.resolve(), installed.resolve())

        def payload(root):
            return {path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*") if path.is_file() and path.suffix in {".py", ".yaml", ".md"}}

        expected = payload(source)
        self.assertIn("bindings.yaml", expected)
        self.assertIn("overview/SKILL.md", expected)
        self.assertEqual(payload(installed), expected)
        self.assertFalse((Path(openmachine.__file__).parent / "_skills").exists())
