from __future__ import annotations

import unittest
from pathlib import Path

from tools.evaluate_hybrid_retrieval import evaluate_cases, load_cases


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hybrid_retrieval_cases.json"


class HybridRetrievalEvaluationTest(unittest.TestCase):
    def test_fixture_contains_eight_safe_natural_and_short_cases(self):
        cases = load_cases(FIXTURE)
        self.assertEqual(8, len(cases))
        self.assertEqual({"natural", "short"}, {case["query_type"] for case in cases})
        self.assertTrue(all(case["expected_source_ids"] for case in cases))

    def test_metrics_keep_three_retrieval_lanes_separate(self):
        result = evaluate_cases(load_cases(FIXTURE))
        self.assertEqual(8, result["case_count"])
        self.assertEqual(0.75, result["keyword_hit_at_3"])
        self.assertEqual(1.0, result["semantic_hit_at_3"])
        self.assertEqual(1.0, result["hybrid_hit_at_3"])
        self.assertEqual(0.75, result["mixed_source_rate"])
        self.assertNotIn("embedding_json", str(result))
        self.assertEqual({"keyword_top3", "semantic_top3", "hybrid_top3"}, set(result["cases"][0]["results"]))

    def test_natural_and_short_keyword_rates_record_the_gap(self):
        result = evaluate_cases(load_cases(FIXTURE))
        self.assertEqual(0.5, result["natural_query_keyword_nonempty_rate"])
        self.assertEqual(1.0, result["short_query_keyword_nonempty_rate"])
        self.assertTrue(result["keyword_query_gap"])

    def test_quality_gate_fails_when_hybrid_coverage_drops_below_best_lane(self):
        cases = [{
            "id": "gate", "query_type": "short", "expected_source_ids": ["expected"],
            "keyword_results": [
                {"source_id": "k1", "source_kind": "keyword_message", "rank": 1},
                {"source_id": "k2", "source_kind": "keyword_message", "rank": 2},
                {"source_id": "k3", "source_kind": "keyword_message", "rank": 3},
            ],
            "semantic_results": [
                {"source_id": "s1", "source_kind": "semantic_summary", "rank": 1},
                {"source_id": "s2", "source_kind": "semantic_summary", "rank": 2},
                {"source_id": "expected", "source_kind": "semantic_summary", "rank": 3},
            ],
        }]
        result = evaluate_cases(cases)
        self.assertEqual("failed", result["quality_gate"])

    def test_evaluation_code_has_no_database_or_runtime_writes(self):
        source = (Path(__file__).resolve().parents[1] / "tools" / "evaluate_hybrid_retrieval.py").read_text(encoding="utf-8")
        for token in ("sqlite3", "whale_chat", "requests", "openai", "embedding_json"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
