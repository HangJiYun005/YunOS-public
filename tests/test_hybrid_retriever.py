from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from memory.hybrid_retriever import hybrid_search_preview
from memory.semantic_memory import SemanticRetrievalError


class HybridRetrieverTest(unittest.TestCase):
    SEMANTIC_DB = str(Path(__file__).resolve().parents[1] / "data" / "test" / "semantic_test.db")

    def _keyword_rows(self):
        return [
            {
                "message_id": "m-1", "conversation_id": "c-1", "memory_space": "whale",
                "snippet": "keyword one", "source_platform": "deepseek",
            },
            {
                "message_id": "m-2", "conversation_id": "c-1", "memory_space": "whale",
                "snippet": "keyword two", "source_platform": "deepseek",
            },
        ]

    def _semantic_payload(self):
        return {
            "provider": "BAAI/bge-small-zh-v1.5", "embedding_dim": 512,
            "results": [
                {
                    "source_id": "s-1", "memory_space": "whale", "rank": 1, "score": 0.9,
                    "content_preview": "semantic one", "summary_type": "conversation_summary",
                    "conversation_id": "c-1",
                },
                {
                    "source_id": "s-2", "memory_space": "whale", "rank": 2, "score": 0.8,
                    "content_preview": "semantic two", "summary_type": "memory_space_overview",
                    "conversation_id": None,
                },
            ],
        }

    @patch("memory.hybrid_retriever.semantic_summary_search_preview")
    @patch("memory.hybrid_retriever.retrieve")
    def test_combines_keyword_and_semantic_results_with_stable_rrf(self, retrieve, semantic):
        retrieve.return_value = self._keyword_rows()
        semantic.return_value = self._semantic_payload()

        first = hybrid_search_preview("whale memory", "source.db", self.SEMANTIC_DB)
        second = hybrid_search_preview("whale memory", "source.db", self.SEMANTIC_DB)

        self.assertEqual(2, first["keyword_count"])
        self.assertEqual(2, first["semantic_count"])
        self.assertEqual(4, first["combined_count"])
        self.assertEqual(first["combined_results"], second["combined_results"])
        self.assertEqual("keyword_message", first["combined_results"][0]["source_kind"])
        self.assertAlmostEqual(1 / 61, first["combined_results"][0]["rrf_score"])
        self.assertNotIn("embedding_json", str(first))

    @patch("memory.hybrid_retriever.semantic_summary_search_preview")
    @patch("memory.hybrid_retriever.retrieve")
    def test_preserves_memory_space_isolation_for_both_retrievers(self, retrieve, semantic):
        retrieve.return_value = []
        semantic.return_value = {"provider": "BAAI/bge-small-zh-v1.5", "embedding_dim": 512, "results": []}

        hybrid_search_preview("whale", "source.db", self.SEMANTIC_DB, memory_space="whale")

        self.assertEqual("whale", retrieve.call_args.kwargs["memory_space"])
        self.assertEqual("whale", semantic.call_args.kwargs["memory_space"])

    @patch("memory.hybrid_retriever.semantic_summary_search_preview", side_effect=SemanticRetrievalError("Stored embedding identity is incompatible"))
    @patch("memory.hybrid_retriever.retrieve", return_value=[])
    def test_refuses_semantic_provider_model_mismatch(self, _retrieve, _semantic):
        with self.assertRaises(SemanticRetrievalError):
            hybrid_search_preview("whale", "source.db", self.SEMANTIC_DB)

    @patch("memory.hybrid_retriever.semantic_summary_search_preview", side_effect=SemanticRetrievalError("Semantic index schema is incomplete"))
    @patch("memory.hybrid_retriever.retrieve", return_value=[])
    def test_does_not_swallow_semantic_retrieval_errors(self, _retrieve, _semantic):
        with self.assertRaises(SemanticRetrievalError):
            hybrid_search_preview("whale", "source.db", self.SEMANTIC_DB)

    @patch("memory.hybrid_retriever.semantic_summary_search_preview")
    @patch("memory.hybrid_retriever.retrieve", return_value=[])
    def test_returns_safe_error_for_missing_semantic_index(self, _retrieve, semantic):
        semantic.return_value = {
            "provider": "BAAI/bge-small-zh-v1.5", "embedding_dim": 512,
            "readonly_error": "memory_embeddings table is missing", "results": [],
        }

        result = hybrid_search_preview("whale", "source.db", self.SEMANTIC_DB)

        self.assertEqual("memory_embeddings table is missing", result["semantic_error"])
        self.assertEqual(0, result["semantic_count"])

    def test_rejects_deterministic_provider_and_non_test_semantic_db(self):
        with self.assertRaisesRegex(ValueError, "local_sentence_transformer"):
            hybrid_search_preview("whale", "source.db", "semantic_test.db", provider_name="deterministic_test")
        with self.assertRaisesRegex(ValueError, "test"):
            hybrid_search_preview("whale", "source.db", "semantic.db")

    def test_preview_code_has_no_write_flag_or_forbidden_runtime_integration(self):
        root = Path(__file__).resolve().parents[1]
        cli_text = (root / "tools" / "hybrid_search_preview.py").read_text(encoding="utf-8")
        retriever_text = (root / "memory" / "hybrid_retriever.py").read_text(encoding="utf-8")

        self.assertNotIn('"--write"', cli_text)
        for token in ("whale_chat", "requests", "openai", "DeepSeek", "sqlite3.connect"):
            self.assertNotIn(token, retriever_text)


if __name__ == "__main__":
    unittest.main()
