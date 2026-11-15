from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from tools.evaluate_embedding_quality import evaluate_provider, load_cases


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "embedding_quality_cases.json"


class EmbeddingQualityEvaluationTest(unittest.TestCase):
    def test_fixture_has_non_private_topic_coverage_and_expected_ids(self):
        cases = load_cases(FIXTURE_PATH)

        self.assertGreaterEqual(len(cases["queries"]), 8)
        self.assertLessEqual(len(cases["queries"]), 12)
        self.assertGreaterEqual(len(cases["candidates"]), 12)
        self.assertLessEqual(len(cases["candidates"]), 20)
        candidate_ids = {candidate["id"] for candidate in cases["candidates"]}
        for query in cases["queries"]:
            self.assertTrue(set(query["expected_source_ids"]).issubset(candidate_ids))

    def test_evaluation_reports_ranked_ids_scores_and_metrics_without_vectors(self):
        result = evaluate_provider("deterministic_test", FIXTURE_PATH)

        self.assertEqual("deterministic_test", result["provider"])
        self.assertEqual(10, result["case_count"])
        self.assertEqual(16, result["candidate_count"])
        self.assertIn("top1_accuracy", result)
        self.assertIn("hit_rate_at_3", result)
        self.assertIn("mean_reciprocal_rank", result)
        self.assertEqual(10, len(result["queries"]))
        self.assertEqual(3, len(result["queries"][0]["top3"]))
        self.assertNotIn("embedding_json", str(result))

    def test_evaluation_uses_one_provider_instance_and_batches_candidates(self):
        with patch("tools.evaluate_embedding_quality.get_embedding_provider") as factory:
            provider = factory.return_value
            provider.embedding_model = "fake"
            provider.dim = 2
            provider.embed_texts.side_effect = [
                [[1.0, 0.0]] * 16,
                [[1.0, 0.0]] * 10,
            ]

            result = evaluate_provider("local_sentence_transformer", FIXTURE_PATH)

        factory.assert_called_once_with("local_sentence_transformer")
        self.assertEqual(2, provider.embed_texts.call_count)
        self.assertEqual(10, result["case_count"])


if __name__ == "__main__":
    unittest.main()
