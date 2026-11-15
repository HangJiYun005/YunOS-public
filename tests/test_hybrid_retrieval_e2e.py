from __future__ import annotations

import hashlib
import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from memory.embedding_provider import DeterministicTestEmbeddingProvider
from memory.hybrid_retriever import hybrid_search_preview
from memory.read_only_retriever import retrieve
from memory.semantic_memory import semantic_summary_search_preview
from tools.evaluate_hybrid_retrieval_e2e import (
    FIXTURE_PATH,
    evaluate_cases,
    quality_gate,
    run_evaluation,
)


class OfflineHybridFixtureProvider(DeterministicTestEmbeddingProvider):
    """Synthetic provider identity for the redacted E2E fixture only."""

    provider_name = "local_sentence_transformer"
    embedding_model = "offline_hybrid_fixture"

    def __init__(self) -> None:
        super().__init__(dim=512)

    def get_embedding_identity(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "model_name": self.embedding_model,
            "model_revision": "offline_fixture_v1",
            "model_fingerprint": hashlib.sha256(b"offline_hybrid_fixture_v1").hexdigest(),
            "embedding_protocol": "deterministic_hash_normalized_v1",
            "embedding_dim": self.dim,
            "normalized": True,
        }


@contextmanager
def offline_fixture_provider():
    provider = OfflineHybridFixtureProvider()
    with patch(
        "tools.evaluate_hybrid_retrieval_e2e.get_embedding_provider",
        return_value=provider,
    ), patch(
        "memory.semantic_memory.get_embedding_provider",
        return_value=provider,
    ):
        yield


class HybridRetrievalE2ETest(unittest.TestCase):
    def test_fixture_has_cases_but_no_prefilled_results(self):
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]
        self.assertGreaterEqual(len(cases), 6)
        for case in cases:
            self.assertNotIn("keyword_results", case)
            self.assertNotIn("semantic_results", case)
            self.assertNotIn("hybrid_results", case)

    def test_real_keyword_semantic_and_hybrid_functions_drive_evaluation(self):
        with offline_fixture_provider(), patch("tools.evaluate_hybrid_retrieval_e2e.retrieve", wraps=retrieve) as keyword, patch(
            "tools.evaluate_hybrid_retrieval_e2e.semantic_summary_search_preview",
            wraps=semantic_summary_search_preview,
        ) as semantic, patch(
            "tools.evaluate_hybrid_retrieval_e2e.hybrid_search_preview", wraps=hybrid_search_preview
        ) as hybrid:
            result = run_evaluation()

        self.assertEqual(6, result["case_count"])
        self.assertGreaterEqual(result["hybrid_hit_at_3"], max(result["keyword_hit_at_3"], result["semantic_hit_at_3"]))
        self.assertGreaterEqual(result["hybrid_nonempty_rate"], max(result["keyword_nonempty_rate"], result["semantic_nonempty_rate"]))
        self.assertEqual("passed", result["quality_gate"])
        self.assertGreater(keyword.call_count, 0)
        self.assertGreater(semantic.call_count, 0)
        self.assertGreater(hybrid.call_count, 0)
        self.assertTrue(result["temporary_databases_cleaned"])
        self.assertTrue(result["readonly_source"])
        self.assertFalse(result["sent_to_model"])

    def test_evaluation_output_contains_only_top3_ids_and_source_kinds(self):
        with offline_fixture_provider():
            result = run_evaluation()
        for case in result["cases"]:
            for route in ("keyword_top3", "semantic_top3", "hybrid_top3"):
                self.assertLessEqual(len(case[route]), 3)
                for item in case[route]:
                    self.assertEqual({"source_id", "source_kind"}, set(item))
        self.assertNotIn("embedding_json", json.dumps(result))

    def test_quality_gate_rejects_hybrid_below_best_single_route(self):
        metrics = {
            "keyword_hit_at_3": 1.0,
            "semantic_hit_at_3": 0.5,
            "hybrid_hit_at_3": 0.5,
            "keyword_nonempty_rate": 1.0,
            "semantic_nonempty_rate": 1.0,
            "hybrid_nonempty_rate": 1.0,
        }
        self.assertEqual("failed", quality_gate(metrics))


if __name__ == "__main__":
    unittest.main()
