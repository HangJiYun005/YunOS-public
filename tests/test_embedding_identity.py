from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.embedding_provider import DeterministicTestEmbeddingProvider
from memory.semantic_memory import (
    SemanticRetrievalError,
    build_embedding_record,
    rank_by_semantic_similarity,
    save_embedding_record,
)
from tools.migrate_semantic_memory import migrate_semantic_memory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"


class EmbeddingIdentityTest(unittest.TestCase):
    def test_deterministic_identity_is_fixed(self):
        self.assertEqual(
            DeterministicTestEmbeddingProvider().get_embedding_identity(),
            DeterministicTestEmbeddingProvider().get_embedding_identity(),
        )

    def test_incompatible_identity_fields_are_rejected(self):
        record = build_embedding_record("notes", "1", "identity test")
        for field, value in (
            ("model_fingerprint", "f" * 64),
            ("model_revision", "other"),
            ("embedding_protocol", "other_protocol"),
            ("embedding_normalized", 0),
        ):
            changed = dict(record)
            changed[field] = value
            with self.assertRaisesRegex(SemanticRetrievalError, "identity") as context:
                rank_by_semantic_similarity("identity test", [changed])
            self.assertNotIn(str(PROJECT_ROOT), str(context.exception))
            self.assertNotIn(record["embedding_json"], str(context.exception))

    def test_new_identity_versions_coexist_but_exact_identity_duplicates(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            path = Path(tmp) / "identity.db"
            migrate_semantic_memory(path, write=True)
            first = build_embedding_record("notes", "1", "same")
            changed = dict(first)
            changed["model_fingerprint"] = "e" * 64
            self.assertEqual("inserted", save_embedding_record(first, path, dry_run=False)["status"])
            self.assertEqual("inserted", save_embedding_record(changed, path, dry_run=False)["status"])
            self.assertEqual("duplicate", save_embedding_record(first, path, dry_run=False)["status"])

    def test_legacy_table_is_migrated_without_current_identity(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            path = Path(tmp) / "legacy.db"
            current = build_embedding_record("notes", "1", "legacy content")
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE memory_embeddings (id INTEGER PRIMARY KEY AUTOINCREMENT, source_table TEXT NOT NULL, source_id TEXT NOT NULL, memory_space TEXT NOT NULL DEFAULT 'general', source_platform TEXT, embedding_model TEXT NOT NULL, embedding_dim INTEGER NOT NULL, embedding_json TEXT NOT NULL, content_hash TEXT NOT NULL, content_preview TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, metadata_json TEXT, UNIQUE(source_table, source_id, embedding_model, content_hash))"
                )
                conn.execute(
                    "INSERT INTO memory_embeddings (source_table, source_id, memory_space, embedding_model, embedding_dim, embedding_json, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("notes", "1", "general", "deterministic_test", 32, current["embedding_json"], current["content_hash"]),
                )
                conn.commit()
            finally:
                conn.close()
            self.assertEqual("migrated", migrate_semantic_memory(path, write=True)["status"])
            conn = sqlite3.connect(path)
            try:
                legacy = conn.execute("SELECT embedding_provider FROM memory_embeddings").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual("legacy_unversioned", legacy)
            self.assertEqual("inserted", save_embedding_record(current, path, dry_run=False)["status"])
            with self.assertRaisesRegex(SemanticRetrievalError, "legacy_unversioned") as context:
                rank_by_semantic_similarity("identity", [{"embedding_provider": legacy}])
            self.assertNotIn(str(PROJECT_ROOT), str(context.exception))
            self.assertNotIn(current["embedding_json"], str(context.exception))


if __name__ == "__main__":
    unittest.main()
