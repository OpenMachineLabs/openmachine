"""Offline checks that keep the minimal-code Skill loadable and its evals runnable."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3] / "skill-library/minimal-code"


class MinimalCodeSkillTests(unittest.TestCase):
    def test_metadata_and_size_fit_skill_loaders(self):
        _, header, body = (ROOT / "SKILL.md").read_text().split("---\n", 2)
        metadata = yaml.safe_load(header)
        self.assertEqual(metadata["name"], ROOT.name)
        self.assertRegex(metadata["name"], r"^[a-z0-9-]{1,64}$")
        self.assertLessEqual(len(metadata["description"]), 1024)
        self.assertLessEqual(len(body.split()), 1000)

    def test_evals_name_existing_projects(self):
        evals = json.loads((ROOT / "evals/evals.json").read_text())
        self.assertEqual(evals["skill_name"], ROOT.name)
        ids = [case["id"] for case in evals["evals"]]
        self.assertEqual(len(ids), len(set(ids)))
        for case in evals["evals"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(case["files"], [f"files/{case['id']}"])
                self.assertTrue((ROOT / "evals" / case["files"][0]).is_dir())
                self.assertTrue(case["prompt"] and case["expectations"])
        self.assertEqual(sorted(path.name for path in (ROOT / "evals/files").iterdir()), sorted(ids))
        triggers = json.loads((ROOT / "evals/triggers.json").read_text())
        self.assertEqual({item["should_trigger"] for item in triggers}, {True, False})

    def test_fixture_projects_start_green(self):
        projects = [path.parent for path in (ROOT / "evals/files").glob("*/test_*.py")]
        self.assertTrue(projects)
        for project in sorted(set(projects)):
            with self.subTest(project=project.name), tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp) / project.name
                shutil.copytree(project, work)
                result = subprocess.run([sys.executable, "-I", "-m", "unittest", "-q"], cwd=work,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertRegex(result.stderr, re.compile(r"^Ran [1-9]", re.M))


if __name__ == "__main__":
    unittest.main()
