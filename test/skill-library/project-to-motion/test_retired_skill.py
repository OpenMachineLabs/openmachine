"""Guard the intentional retirement of project-to-motion v1.

This remains as a behavior check for removed files under the repository's
module-aware test selection. It does not install or run the retired Skill.
"""

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[3]
RETIRED_SKILL = "skill-library/project-to-motion"
TEST_PATH = "test/skill-library/project-to-motion/test_retired_skill.py"


class RetiredProjectToMotionTests(unittest.TestCase):
    def test_old_skill_is_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files", "--", RETIRED_SKILL],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "", "Retired Skill is still in the Git index")

    def test_retirement_has_no_production_sources(self):
        ownership = json.loads((ROOT / "test/ownership.json").read_text(encoding="utf-8"))
        module = ownership["modules"]["project_to_motion"]
        self.assertEqual(module["sources"], [])
        self.assertEqual(module["tests"], [TEST_PATH])
        self.assertEqual(module["boundary_tests"], [])


if __name__ == "__main__":
    unittest.main()
