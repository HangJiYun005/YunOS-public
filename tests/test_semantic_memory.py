from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.embedding_provider import (
    DeterministicTestEmbeddingProvider,
    cosine_similarity,
    get_embedding_provider,
)
from memory.semantic_memory import (
    build_embedding_record,
    rank_by_semantic_similarity,
    save_embedding_record,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"


class SemanticMemoryTest(unittest.TestCase):
    def test_deterministic_embedding_is_stable_for_same_text(self):
        provider = DeterministicTestEmbeddingProvider(dim=32)

        first = provider.embed_text("hello semantic memory")
        second = provider.embed_text("hello semantic memory")

        self.assertEqual(first, second)
        self.assertEqual(32, len(first))

    def test_deterministic_embedding_changes_for_different_text(self):
        provider = DeterministicTestEmbeddingProvider(dim=32)

        first = provider.embed_text("hello semantic memory")
        second = provider.embed_text("different semantic memory")

        self.assertNotEqual(first, second)

    def test_cosine_similarity_handles_normal_vectors(self):
        self.assertAlmostEqual(1.0, cosine_similarity([1.0, 0.0], [1.0, 0.0]))
        self.assertAlmostEqual(0.0, cosine_similarity([1.0, 0.0], [0.0, 1.0]))
        self.assertAlmostEqual(0.0, cosine_similarity([0.0, 0.0], [1.0, 0.0]))

    def test_build_embedding_record_has_required_fields(self):
        record = build_embedding_record(
            source_table="messages",
            source_id="msg-1",
            text="hello semantic memory",
            memory_space="whale",
            source_platform="deepseek",
            metadata={"role": "user"},
        )

        self.assertEqual(
            {
                "source_table",
                "source_id",
                "memory_space",
                "source_platform",
                "embedding_provider",
                "embedding_model",
                "model_revision",
                "model_fingerprint",
                "embedding_protocol",
                "embedding_normalized",
                "embedding_dim",
                "embedding_json",
                "content_hash",
                "content_preview",
                "metadata_json",
            },
            set(record),
        )
        self.assertEqual("messages", record["source_table"])
        self.assertEqual("msg-1", record["source_id"])
        self.assertEqual("whale", record["memory_space"])
        self.assertEqual("deepseek", record["source_platform"])
        self.assertEqual("deterministic_test", record["embedding_model"])
        self.assertEqual("deterministic_test", record["embedding_provider"])
        self.assertTrue(record["embedding_normalized"])
        self.assertEqual(32, record["embedding_dim"])
        self.assertEqual({"role": "user"}, json.loads(record["metadata_json"]))

    def test_content_hash_is_stable(self):
        first = build_embedding_record("messages", "msg-1", "same content")
        second = build_embedding_record("messages", "msg-2", "same content")

        self.assertEqual(first["content_hash"], second["content_hash"])

    def test_embedding_json_is_parseable(self):
        record = build_embedding_record("messages", "msg-1", "parseable content")

        embedding = json.loads(record["embedding_json"])

        self.assertIsInstance(embedding, list)
        self.assertEqual(record["embedding_dim"], len(embedding))
        self.assertTrue(all(isinstance(value, float) for value in embedding))

    def test_rank_by_semantic_similarity_returns_top_k_with_scores(self):
        records = [
            build_embedding_record("messages", "msg-a", "alpha beta"),
            build_embedding_record("messages", "msg-b", "hello memory"),
            build_embedding_record("messages", "msg-c", "hello semantic memory"),
        ]

        ranked = rank_by_semantic_similarity("hello memory", records, top_k=2)

        self.assertEqual(2, len(ranked))
        self.assertEqual(["msg-b", "msg-c"], [item["source_id"] for item in ranked])
        self.assertGreaterEqual(ranked[0]["score"], ranked[1]["score"])

    def test_dry_run_does_not_write_database(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            before_exists = db_path.exists()
            record = build_embedding_record("messages", "msg-1", "dry run only")

            result = save_embedding_record(record, db_path, dry_run=True)

            self.assertFalse(before_exists)
            self.assertEqual({"dry_run": True, "written": False, "db_path": str(db_path)}, result)
            self.assertFalse(db_path.exists())

    def test_real_database_write_is_rejected(self):
        real_db = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
        record = build_embedding_record("messages", "msg-1", "must not write real db")

        with self.assertRaises(ValueError):
            save_embedding_record(record, real_db, dry_run=False)

    def test_missing_memory_embeddings_table_is_rejected(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            record = build_embedding_record("messages", "msg-1", "no auto migration")

            with self.assertRaises(RuntimeError):
                save_embedding_record(record, db_path, dry_run=False)

    def test_embedding_provider_does_not_call_api_or_llm(self):
        provider = get_embedding_provider("deterministic_test")

        with patch("socket.create_connection", side_effect=AssertionError("network not allowed")):
            embedding = provider.embed_text("local deterministic text")

        self.assertEqual(32, len(embedding))

    def test_semantic_memory_code_does_not_integrate_forbidden_surfaces(self):
        forbidden_tokens = (
            "whale_chat",
            "web/",
            "persona",
            "runtime_context",
            "context_profiles",
            "DeepSeek",
            "requests",
            "openai",
        )
        checked_paths = [
            PROJECT_ROOT / "memory" / "embedding_provider.py",
            PROJECT_ROOT / "memory" / "semantic_memory.py",
            PROJECT_ROOT / "tools" / "semantic_memory.py",
            PROJECT_ROOT / "tools" / "migrate_semantic_memory.py",
        ]

        for module_name in ("whale_chat", "tools.local_web"):
            sys.modules.pop(module_name, None)

        for path in checked_paths:
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                self.assertNotIn(token, text)

        self.assertNotIn("whale_chat", sys.modules)
        self.assertNotIn("tools.local_web", sys.modules)


if __name__ == "__main__":
    unittest.main()
