from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.lifecycle_executor import execute_memory_lifecycle
from tools.migrate_semantic_memory import migrate_semantic_memory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "data" / "test"


class LifecycleExecutorTest(unittest.TestCase):
    def _fixture(self, summary_text: str = "controlled summary"):
        holder = TemporaryDirectory(dir=TEST_ROOT)
        root = Path(holder.name)
        source, target = root / "source.db", root / "target.db"
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute("CREATE TABLE memory_summaries (id TEXT PRIMARY KEY, memory_space TEXT, summary_type TEXT, conversation_id TEXT, summary_text TEXT)")
            conn.execute("INSERT INTO memory_summaries VALUES ('s1', 'whale', 'conversation_summary', 'c1', ?)", (summary_text,))
        self.assertEqual("created", migrate_semantic_memory(target, write=True)["status"])
        return holder, source, target

    def _execute(self, source, target, dry_run=True):
        return execute_memory_lifecycle(source, target, memory_space="whale", provider_name="deterministic_test", dry_run=dry_run)

    def test_default_dry_run_returns_plan_without_writing(self):
        holder, source, target = self._fixture()
        try:
            before = target.read_bytes()
            result = self._execute(source, target)
            self.assertTrue(result["dry_run"])
            self.assertFalse(result["executed"])
            self.assertEqual("planned", result["operations"][0]["status"])
            self.assertFalse(result["operations"][0]["audit"]["raw_messages_read"])
            self.assertEqual(before, target.read_bytes())
            self.assertNotIn("controlled summary", str(result))
        finally:
            holder.cleanup()

    def test_execute_inserts_then_is_idempotent(self):
        holder, source, target = self._fixture()
        try:
            first = self._execute(source, target, dry_run=False)
            second = self._execute(source, target, dry_run=False)
            self.assertEqual(1, first["result_counts"]["inserted"])
            self.assertEqual(0, second["result_counts"]["planned"])
            with closing(sqlite3.connect(target)) as conn, conn:
                self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0])
        finally:
            holder.cleanup()

    def test_rebuild_appends_current_embedding_without_deleting_old_record(self):
        holder, source, target = self._fixture("new text")
        try:
            with closing(sqlite3.connect(target)) as conn, conn:
                conn.execute("INSERT INTO memory_embeddings (source_table, source_id, memory_space, embedding_provider, embedding_model, model_revision, model_fingerprint, embedding_protocol, embedding_normalized, embedding_dim, embedding_json, content_hash, content_preview, metadata_json) VALUES ('memory_summaries', 's1', 'whale', 'deterministic_test', 'deterministic_test', 'deterministic_hash_normalized_v1', 'fixture', 'deterministic_hash_normalized_v1', 1, 32, '[0]', 'old', '', '{}')")
            result = self._execute(source, target, dry_run=False)
            self.assertEqual("rebuild_embedding", result["operations"][0]["recommended_action"])
            self.assertEqual(1, result["result_counts"]["inserted"])
            with closing(sqlite3.connect(target)) as conn, conn:
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0])
        finally:
            holder.cleanup()

    def test_real_database_target_is_refused_before_planning_or_embedding(self):
        holder, source, _ = self._fixture()
        try:
            with self.assertRaises(ValueError):
                self._execute(source, PROJECT_ROOT / "data" / "ayunos_chatgpt.db", dry_run=False)
        finally:
            holder.cleanup()

    def test_executor_source_has_no_messages_query_or_web_integration(self):
        text = (PROJECT_ROOT / "memory" / "lifecycle_executor.py").read_text(encoding="utf-8")
        for forbidden in ("whale_chat", "web", "FROM messages", "requests", "openai"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
