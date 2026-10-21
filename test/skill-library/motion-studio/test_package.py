"""Structural regression checks for the self-contained Motion Studio Skill."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skill-library" / "motion-studio"
PILOT = SKILL / "examples" / "pilot-film"


class MotionStudioPackageTests(unittest.TestCase):
    def test_skill_documents_and_style_are_present(self):
        for relative in (
            "SKILL.md",
            "references/workflow.md",
            "references/remotion.md",
            "references/sources.md",
            "styles/anthropic-editorial/STYLE.md",
            "styles/anthropic-editorial/references.md",
            "examples/pilot-film/brief.md",
            "examples/pilot-film/art-directions.svg",
        ):
            with self.subTest(path=relative):
                self.assertTrue((SKILL / relative).is_file(), relative)

    def test_remotion_dependencies_are_exactly_pinned(self):
        package = json.loads((PILOT / "package.json").read_text())
        lock = json.loads((PILOT / "package-lock.json").read_text())
        self.assertEqual(package["dependencies"]["remotion"], "4.0.537")
        self.assertEqual(package["dependencies"]["@remotion/cli"], "4.0.537")
        for name, version in package["dependencies"].items():
            self.assertEqual(lock["packages"]["node_modules/" + name]["version"], version)

    def test_composition_is_40_seconds_full_hd_30_fps(self):
        source = (PILOT / "src" / "root.tsx").read_text()
        self.assertRegex(source, r'width=\{1920\}')
        self.assertRegex(source, r'height=\{1080\}')
        self.assertRegex(source, r'fps=\{30\}')
        self.assertRegex(source, r'durationInFrames=\{1200\}')
        self.assertIn("ContextualRetrieval", source)

    def test_context_label_has_explicit_readable_width(self):
        source = (PILOT / "src" / "Film.tsx").read_text()
        self.assertIn("w={540}", source)
        self.assertIn("ACME CORP. / Q2 2023 SEC FILING", source)

    def test_research_claims_keep_evaluation_scope_and_caveat(self):
        brief = (PILOT / "brief.md").read_text()
        self.assertIn("top-20-chunk evaluation", brief)
        self.assertIn("not a universal guarantee", brief)
        self.assertIn("illustrative", brief.lower())


if __name__ == "__main__":
    unittest.main()
