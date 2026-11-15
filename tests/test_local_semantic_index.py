from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.semantic_memory import (
    SemanticRetrievalError,
    build_embedding_records_from_summaries,
    semantic_summary_search_preview,
)
from tools.migrate_semantic_memory import migrate_semantic_memory


LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"


class _FakeLocalProvider:
    embedding_model = LOCAL_MODEL
    dim = 2

    def __init__(self) -> None:
        self.batch_calls: list[list[str]] = []

    def get_embedding_identity(self) -> dict:
        return {
            "provider_name": "local_sentence_transformer",
            "model_name": LOCAL_MODEL,
            "model_revision": "test-revision",
            "model_fingerprint": "a" * 64,
            "embedding_protocol": "sentence_transformers_normalized_v1",
            "embedding_dim": self.dim,
            "normalized": True,
        }

    def embed_text(self, text: str) -> list[float]:
        return [1.0, 0.0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(texts)
        return [[1.0, 0.0] for _ in texts]


class LocalSemanticIndexTest(unittest.TestCase):
    def test_summary_record_builder_batches_nonempty_rows_with_one_provider(self):
        provider = _FakeLocalProvider()
        rows = [
            {
                "id": 1,
                "memory_space": "whale",
                "summary_text": "first summary",
                "summary_type": "conversation_summary",
                "conversation_id": "conv-1",
                "method": "extractive_v1",
                "source_message_count": 2,
            },
            {
                "id": 2,
                "memory_space": "whale",
                "summary_text": "second summary",
                "summary_type": "memory_space_overview",
                "conversation_id": None,
                "method": "extractive_v1",
                "source_message_count": 3,
            },
        ]
        with patch("memory.semantic_memory.get_embedding_provider", return_value=provider) as factory:
            records, skipped = build_embedding_records_from_summaries(
                rows, provider_name="local_sentence_transformer"
            )

        factory.assert_called_once_with("local_sentence_transformer")
        self.assertEqual([["first summary", "second summary"]], provider.batch_calls)
        self.assertEqual(0, skipped)
        self.assertEqual([LOCAL_MODEL, LOCAL_MODEL], [record["embedding_model"] for record in records])
        self.assertEqual([2, 2], [record["embedding_dim"] for record in records])

    def test_preview_filters_using_actual_model_id_not_provider_alias(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.executemany(
                    """
                    INSERT INTO memory_embeddings (
                        source_table, source_id, memory_space, embedding_provider, embedding_model,
                        model_revision, model_fingerprint, embedding_protocol, embedding_normalized,
                        embedding_dim, embedding_json, content_hash, content_preview, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "memory_summaries", "local-1", "whale", "local_sentence_transformer", LOCAL_MODEL,
                            "test-revision", "a" * 64, "sentence_transformers_normalized_v1", 1, 2,
                            json.dumps([1.0, 0.0]), "hash-local", "local BGE summary", "{}",
                        ),
                        (
                            "memory_summaries", "test-1", "whale", "deterministic_test", "deterministic_test",
                            "deterministic_v1", "b" * 64, "deterministic_hash_normalized_v1", 1, 32,
                            json.dumps([1.0] + [0.0] * 31), "hash-test", "test summary", "{}",
                        ),
                    ],
                )
                conn.commit()

            with patch("memory.semantic_memory.get_embedding_provider", return_value=_FakeLocalProvider()):
                result = semantic_summary_search_preview(
                    "local query", db_path, provider_name="local_sentence_transformer"
                )
            self.assertEqual(1, result["candidate_count"])
            self.assertEqual("local-1", result["results"][0]["source_id"])

    def test_preview_refuses_records_with_a_mismatched_embedding_dimension(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO memory_embeddings (
                        source_table, source_id, memory_space, embedding_provider, embedding_model,
                        model_revision, model_fingerprint, embedding_protocol, embedding_normalized,
                        embedding_dim, embedding_json, content_hash, content_preview, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "memory_summaries", "bad-dim", "whale", "local_sentence_transformer", LOCAL_MODEL,
                        "test-revision", "a" * 64, "sentence_transformers_normalized_v1", 1, 3,
                        json.dumps([1.0, 0.0, 0.0]), "hash-bad", "bad dimension", "{}",
                    ),
                )
                conn.commit()

            with patch("memory.semantic_memory.get_embedding_provider", return_value=_FakeLocalProvider()):
                with self.assertRaisesRegex(SemanticRetrievalError, "identity") as context:
                    semantic_summary_search_preview(
                        "local query", db_path, provider_name="local_sentence_transformer"
                    )
                self.assertNotIn(str(PROJECT_ROOT), str(context.exception))
                self.assertNotIn(json.dumps([1.0, 0.0, 0.0]), str(context.exception))


if __name__ == "__main__":
    unittest.main()
