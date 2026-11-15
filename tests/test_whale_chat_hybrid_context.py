from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import whale_chat
from memory.semantic_memory import SemanticRetrievalError


class WhaleChatHybridContextTest(unittest.TestCase):
    def _combined(self):
        return [
            {
                "source_kind": "keyword_message", "source_id": "m-1", "memory_space": "whale",
                "rrf_score": 0.016, "original_score": None, "content_preview": "keyword preview",
                "source_platform": "deepseek", "summary_type": None, "conversation_id": "c-1",
            },
            {
                "source_kind": "semantic_summary", "source_id": "s-1", "memory_space": "whale",
                "rrf_score": 0.015, "original_score": 0.8, "content_preview": "summary preview",
                "source_platform": None, "summary_type": "conversation_summary", "conversation_id": "c-1",
            },
        ]

    @patch.object(whale_chat, "retrieve", return_value=[])
    @patch.object(whale_chat, "load_whale_summary_rows", return_value=[])
    @patch.object(whale_chat, "load_persona", return_value="persona")
    def test_default_legacy_keeps_existing_sections(self, _persona, _summaries, _retrieve):
        report = whale_chat.build_whale_prompt_context("whale", db_path="unused.db")

        self.assertEqual("legacy", report["retrieval_mode_effective"])
        self.assertIn("High-level Memory Summaries - whale", report["ordering"]["section_order"])
        self.assertIn("Relevant Memory - whale", report["ordering"]["section_order"])

    @patch.object(whale_chat, "hybrid_search_preview")
    @patch.object(whale_chat, "load_persona", return_value="persona")
    def test_hybrid_replaces_summary_and_raw_sections(self, _persona, hybrid):
        hybrid.return_value = {
            "keyword_count": 1, "semantic_count": 1, "combined_count": 2,
            "combined_results": self._combined(),
        }

        report = whale_chat.build_whale_prompt_context(
            "whale", db_path="source.db", retrieval_mode="hybrid", semantic_db_path="semantic_test.db"
        )

        self.assertEqual("hybrid", report["retrieval_mode_effective"])
        self.assertEqual(1, report["hybrid_keyword_count"])
        self.assertIn("Hybrid Relevant Memory - whale", report["ordering"]["section_order"])
        self.assertNotIn("[High-level Memory Summaries - whale]", report["prompt"])
        self.assertNotIn("[Relevant Memory - whale]", report["prompt"])
        self.assertIn("keyword preview", report["prompt"])
        self.assertIn("summary preview", report["prompt"])
        self.assertNotIn("embedding_json", report["prompt"])

    @patch.object(whale_chat, "hybrid_search_preview")
    @patch.object(whale_chat, "load_persona", return_value="persona")
    def test_hybrid_keeps_whale_space_and_bounded_combined_limit(self, _persona, hybrid):
        hybrid.return_value = {"keyword_count": 0, "semantic_count": 0, "combined_count": 0, "combined_results": []}

        whale_chat.build_whale_prompt_context(
            "whale", db_path="source.db", retrieval_mode="hybrid", semantic_db_path="semantic_test.db", combined_top_k=99
        )

        self.assertEqual("whale", hybrid.call_args.kwargs["memory_space"])
        self.assertEqual(8, hybrid.call_args.kwargs["combined_top_k"])

    @patch.object(whale_chat, "retrieve", return_value=[])
    @patch.object(whale_chat, "load_whale_summary_rows", return_value=[])
    @patch.object(whale_chat, "hybrid_search_preview", return_value={"semantic_error": "semantic index database is missing"})
    @patch.object(whale_chat, "load_persona", return_value="persona")
    def test_missing_hybrid_database_falls_back_to_legacy(self, _persona, _hybrid, _summaries, _retrieve):
        report = whale_chat.build_whale_prompt_context(
            "whale", db_path="source.db", retrieval_mode="hybrid", semantic_db_path="missing_test.db"
        )

        self.assertEqual("legacy_fallback", report["retrieval_mode_effective"])
        self.assertIn("missing", report["hybrid_warning"])

    @patch.object(whale_chat, "retrieve", return_value=[])
    @patch.object(whale_chat, "load_whale_summary_rows", return_value=[])
    @patch.object(whale_chat, "hybrid_search_preview", return_value={"status": "refused", "semantic_error": "Embedding provider does not match"})
    @patch.object(whale_chat, "load_persona", return_value="persona")
    def test_provider_mismatch_falls_back_to_legacy(self, _persona, _hybrid, _summaries, _retrieve):
        report = whale_chat.build_whale_prompt_context(
            "whale", db_path="source.db", retrieval_mode="hybrid", semantic_db_path="semantic_test.db"
        )

        self.assertEqual("legacy_fallback", report["retrieval_mode_effective"])
        self.assertIn("provider", report["hybrid_warning"])

    @patch.object(whale_chat, "hybrid_search_preview", side_effect=SemanticRetrievalError("Semantic index schema is incomplete"))
    @patch.object(whale_chat, "load_whale_summary_rows", return_value=[{"summary_text": "legacy summary"}])
    @patch.object(whale_chat, "retrieve", return_value=[{"snippet": "legacy memory", "memory_space": "whale"}])
    @patch.object(whale_chat, "load_persona", return_value="persona")
    def test_semantic_failure_rebuilds_complete_legacy_context(self, _persona, _retrieve, _summaries, _hybrid):
        report = whale_chat.build_whale_prompt_context(
            "whale", db_path="source.db", retrieval_mode="hybrid", semantic_db_path="semantic_test.db"
        )

        self.assertEqual("legacy_fallback", report["retrieval_mode_effective"])
        self.assertTrue(report["hybrid_warning"])
        self.assertNotIn("semantic_test.db", report["hybrid_warning"])
        self.assertIn("[System]", report["prompt"])
        self.assertIn("[High-level Memory Summaries - whale]", report["prompt"])
        self.assertIn("[Relevant Memory - whale]", report["prompt"])
        self.assertIn("[User Input]", report["prompt"])
        self.assertNotIn("[Hybrid Relevant Memory - whale]", report["prompt"])

    def test_hybrid_requires_semantic_database_path(self):
        with self.assertRaisesRegex(ValueError, "semantic_db_path"):
            whale_chat.build_whale_prompt_context("whale", retrieval_mode="hybrid")


if __name__ == "__main__":
    unittest.main()
