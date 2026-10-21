"""Check portable research integrity and the runnable authored examples."""

import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skill-library" / "project-to-motion"
SPEC = importlib.util.spec_from_file_location("motion_catalog_subject", SKILL / "scripts" / "motion.py")
motion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(motion)


def read(name):
    return json.loads((SKILL / "references" / name).read_text(encoding="utf-8"))


class CatalogTests(unittest.TestCase):
    def test_unique_portable_sources_and_grounded_cases(self):
        sources = read("source-index.json")["sources"]
        cases = read("motion-cases.json")["cases"]
        by_id = {row["id"]: row for row in sources}
        self.assertEqual(len(by_id), len(sources))
        self.assertEqual(len({row["url"] for row in sources}), len(sources))
        self.assertEqual(len({row["id"] for row in cases}), len(cases))
        for row in sources:
            self.assertEqual(urlsplit(row["url"]).scheme, "https")
            for key in ("title", "publisher", "kind", "topics", "learning_targets", "verified_at"):
                self.assertTrue(row[key], (row["id"], key))
            self.assertIn(row["evidence_level"], ("page_verified", "frames_inspected", "segment_watched"))
        for row in cases:
            self.assertIn(row["source_id"], by_id)
            for key in ("source_stated", "observed", "inference", "viewer_problem", "story_structure", "why_motion", "object_continuity", "transfer_rules", "limits", "counterexample"):
                self.assertTrue(row[key], (row["id"], key))

    def test_frame_claims_have_reproducible_scope_without_local_assets(self):
        sources = read("source-index.json")["sources"]
        observations = read("frame-observations.json")
        observed = {row["source_id"] for row in observations["assets"]}
        for row in sources:
            if row["evidence_level"] == "frames_inspected":
                self.assertIn(row["id"], observed)
        for row in observations["assets"]:
            self.assertEqual(urlsplit(row["media_url"]).scheme, "https")
            self.assertRegex(row["media_sha256"], r"^[a-f0-9]{64}$")
            self.assertGreater(row["duration_seconds"], 0)
            points = row["sample_offsets_seconds"]
            self.assertEqual(points, sorted(set(points)))
            self.assertTrue(all(0 <= p < row["duration_seconds"] for p in points))
            self.assertNotIn("local_review_artifacts", row)

    def test_catalog_is_compact_text_not_a_media_archive(self):
        names = ("source-index.json", "motion-cases.json", "frame-observations.json")
        self.assertLess(sum((SKILL / "references" / n).stat().st_size for n in names), 160 * 1024)
        for name in names:
            text = (SKILL / "references" / name).read_text(encoding="utf-8")
            self.assertNotIn("data:video", text)
            self.assertNotIn("data:image", text)
            self.assertNotIn("/workspace/scratch/", text)

    def test_article_and_repository_examples_are_valid_and_distinct(self):
        examples = [json.loads((SKILL / "examples" / n).read_text()) for n in ("article-motion.json", "repository-motion.json")]
        for example in examples:
            motion.validate_spec(example)
        article = (SKILL / "examples" / "article.md").read_text(encoding="utf-8")
        self.assertEqual(examples[0]["sources"][0]["sha256"], hashlib.sha256(article.encode()).hexdigest())
        self.assertIn("/blob/", examples[1]["sources"][0]["locator"])
        self.assertEqual(examples[1]["sources"][0]["kind"], "repository_excerpt")

    def test_natural_multiword_queries_recover_useful_partial_matches(self):
        for query in ("routing continuity", "teaser proof", "parallel join dependencies"):
            with self.subTest(query=query):
                results = motion.search_catalog(query, limit=4)
                self.assertTrue(results)
                self.assertLessEqual(len(results), 4)
                self.assertTrue(results[0]["matched_terms"])
        self.assertEqual(motion.search_catalog("unfindablewordxyz"), [])


if __name__ == "__main__":
    unittest.main()
