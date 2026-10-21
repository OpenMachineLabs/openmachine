"""Focused ownership tests for the self-contained Motion Studio production Skill."""
import importlib.util
import json
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skill-library" / "motion-studio"
PILOT = SKILL / "examples" / "pilot-film"


class MotionStudioPackageTests(unittest.TestCase):
    def test_skill_style_and_real_direction_artwork_are_present(self):
        for relative in (
            "SKILL.md",
            "references/workflow.md",
            "references/remotion.md",
            "references/sources.md",
            "styles/anthropic-editorial/STYLE.md",
            "styles/anthropic-editorial/references.md",
            "examples/pilot-film/brief.md",
            "examples/pilot-film/src/Art.tsx",
            "examples/pilot-film/src/DirectionB.tsx",
            "examples/pilot-film/scripts/generate_soundtrack.py",
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

    def test_composition_and_real_nine_second_proof(self):
        source = (PILOT / "src" / "root.tsx").read_text()
        self.assertRegex(source, r'width=\{1920\}')
        self.assertRegex(source, r'height=\{1080\}')
        self.assertRegex(source, r'fps=\{30\}')
        self.assertRegex(source, r'durationInFrames=\{1200\}')
        self.assertIn("ContextualRetrieval", source)
        self.assertIn("DirectionB", source)
        self.assertTrue((ROOT / "artifacts/motion-studio/direction-study.jpg").is_file())
        package = json.loads((PILOT / "package.json").read_text())
        self.assertIn("--frames=310-579", package["scripts"]["preview"])
        self.assertEqual((579 - 310 + 1) / 30, 9)

    def test_context_attaches_to_same_fragment_without_erasing_original(self):
        artwork = (PILOT / "src" / "Art.tsx").read_text()
        film = (PILOT / "src" / "Film.tsx").read_text()
        self.assertIn("GENERATED CONTEXT / PREPENDED", artwork)
        self.assertIn("The company's revenue grew by 3%", artwork)
        self.assertIn("context={0}", film)
        self.assertIn("context={dock}", film)
        self.assertIn("context={1}", film)
        self.assertIn("SemanticField", film)
        self.assertIn("LexicalIndex", film)

    def test_original_score_is_deterministic_and_fades_at_edges(self):
        path = PILOT / "scripts" / "generate_soundtrack.py"
        spec = importlib.util.spec_from_file_location("pilot_score", path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.SECONDS, 40)
        self.assertEqual(module.fade(0), 0)
        self.assertEqual(module.fade(20), 1)
        self.assertEqual(module.fade(40), 0)
        for second in (1.2, 12.8, 22.4, 38.7):
            result = module.score(second, int(second * module.RATE))
            self.assertEqual(result, module.score(second, int(second * module.RATE)))
            self.assertEqual(len(result), 2)
            self.assertTrue(all(math.isfinite(v) and abs(v) < 1 for v in result))

    def test_research_claim_keeps_evaluation_scope_and_example_boundary(self):
        brief = (PILOT / "brief.md").read_text()
        self.assertIn("top-20-chunk retrieval failure rate", brief)
        self.assertIn("5.7%", brief)
        self.assertIn("2.9%", brief)
        self.assertIn("49% relative reduction", brief)
        self.assertIn("illustrative", brief.lower())
        self.assertIn("not imply universal", brief)


    def test_nine_hours_article_film_has_exact_output_contract(self):
        source = (PILOT / "src" / "root.tsx").read_text()
        package = json.loads((PILOT / "package.json").read_text())
        film = (PILOT / "src" / "NineHours.tsx").read_text()
        artwork = (PILOT / "src" / "NineHoursArt.tsx").read_text()
        self.assertIn("id=\"NineHoursEngineering\"", source)
        self.assertIn("durationInFrames={1260}", source)
        self.assertIn("id=\"NineHoursAlt\"", source)
        self.assertIn("--frames=320-589", package["scripts"]["nine-hours-preview"])
        self.assertEqual((589 - 320 + 1) / 30, 9)
        self.assertIn("staticFile('nine-hours-score.wav')", film)
        self.assertIn("value={43}", film)
        self.assertIn("value={93}", film)
        self.assertIn("Skill", film)
        self.assertIn("ComputeRack", film)
        self.assertIn("MiniChip", artwork)

    def test_nine_hours_audio_and_claims_are_bounded(self):
        brief = (PILOT / "nine-hours-brief.md").read_text()
        self.assertIn("over 10 million hours of audio", brief)
        self.assertIn("43% to 93%", brief)
        self.assertIn("reported before/after", brief)
        self.assertIn("independent verification", brief)
        self.assertIn("six hours of research", brief)
        path = PILOT / "scripts" / "generate_nine_hours_score.py"
        spec = importlib.util.spec_from_file_location("nine_hours_score", path)
        self.assertIsNotNone(spec)
        score = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(score)
        self.assertEqual(score.SECONDS, 42)
        self.assertEqual(score.fade(0), 0)
        self.assertGreater(score.fade(41.95), 0)
        self.assertEqual(score.fade(42), 0)
        for second in (4.7, 17.5, 33.2, 40.1):
            l, r = score.sample(second)
            self.assertEqual((l, r), score.sample(second))
            self.assertTrue(all(math.isfinite(v) and abs(v) < 1 for v in (l, r)))


if __name__ == "__main__":
    unittest.main()
