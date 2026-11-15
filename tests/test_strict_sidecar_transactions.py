from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.sidecar_maintenance import _maintain_sidecar_transaction
from tools.init_semantic_sidecar import CREATE_MEMORY_EMBEDDINGS_SQL


ROOT = Path(__file__).resolve().parents[1]


def record(source_id: str, *, source_table: str | None = "memory_summaries") -> dict:
    return {
        "source_table": source_table, "source_id": source_id, "memory_space": "whale",
        "source_platform": None, "embedding_provider": "test", "embedding_model": "test",
        "model_revision": "r", "model_fingerprint": "f", "embedding_protocol": "p",
        "embedding_normalized": 1, "embedding_dim": 1, "embedding_json": "[1.0]",
        "content_hash": source_id, "content_preview": "", "metadata_json": "{}",
    }


class StrictSidecarTransactionTest(unittest.TestCase):
    def setUp(self):
        self.holder = TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.holder.name)
        self.db = root / "sidecar.db"
        self.backups = root / "backups"
        with sqlite3.connect(self.db) as conn:
            conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
        self.policy = patch("memory.sidecar_maintenance.validate_semantic_sidecar_write_target", return_value=self.db)
        self.backup_root = patch("memory.sidecar_maintenance.BACKUP_ROOT", self.backups)
        self.policy.start(); self.backup_root.start()

    def tearDown(self):
        self.backup_root.stop(); self.policy.stop(); self.holder.cleanup()

    def count(self) -> int:
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]

    def test_two_actions_commit_in_one_batch(self):
        result = _maintain_sidecar_transaction([record("1"), record("2")], self.db, "plan")
        self.assertEqual("committed", result["status"])
        self.assertTrue(result["transaction_committed"])
        self.assertEqual((2, 0, 2), (result["inserted_count"], result["duplicate_count"], self.count()))

    def test_unique_conflict_is_duplicate_and_other_action_commits(self):
        _maintain_sidecar_transaction([record("existing")], self.db, "seed")
        result = _maintain_sidecar_transaction([record("new"), record("existing")], self.db, "plan")
        self.assertEqual("committed", result["status"])
        self.assertEqual((1, 1, 2), (result["inserted_count"], result["duplicate_count"], self.count()))

    def test_not_null_failure_rolls_back_first_action_and_preserves_hash(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = _maintain_sidecar_transaction([record("good"), record("bad", source_table=None)], self.db, "plan")
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual("rolled_back", result["status"])
        self.assertTrue(result["transaction_rolled_back"])
        self.assertEqual((0, 0, before), (result["inserted_count"], self.count(), after))

    def test_trigger_abort_rolls_back_entire_batch(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TRIGGER reject_bad BEFORE INSERT ON memory_embeddings WHEN NEW.source_id='bad' BEGIN SELECT RAISE(ABORT, 'rejected'); END")
        result = _maintain_sidecar_transaction([record("good"), record("bad")], self.db, "plan")
        self.assertEqual("rolled_back", result["status"])
        self.assertEqual(0, self.count())

    def test_schema_and_other_operational_errors_are_not_busy(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE memory_embeddings")
        result = _maintain_sidecar_transaction([record("1")], self.db, "plan")
        self.assertEqual("rolled_back", result["status"])
        with patch("memory.sidecar_maintenance.insert_embedding_record", side_effect=sqlite3.OperationalError("simulated io")):
            result = _maintain_sidecar_transaction([record("1")], self.db, "plan2")
        self.assertEqual("rolled_back", result["status"])

    def test_real_busy_is_distinct_and_writes_nothing(self):
        lock = sqlite3.connect(self.db)
        lock.execute("BEGIN IMMEDIATE")
        try:
            result = _maintain_sidecar_transaction([record("1")], self.db, "plan")
        finally:
            lock.rollback(); lock.close()
        self.assertEqual("maintenance_busy", result["status"])
        self.assertFalse(result["backup_created"])
        self.assertFalse(self.backups.exists() and list(self.backups.glob("*.db")))
        self.assertEqual(0, self.count())

    def test_real_chat_database_hash_is_unchanged(self):
        real = ROOT / "data" / ("ayunos_" + "chatgpt.db")
        if not real.exists():
            self.skipTest("real database is unavailable")
        before = hashlib.sha256(real.read_bytes()).hexdigest()
        _maintain_sidecar_transaction([record("1")], self.db, "plan")
        self.assertEqual(before, hashlib.sha256(real.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
