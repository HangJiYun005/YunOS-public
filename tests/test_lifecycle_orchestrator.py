from __future__ import annotations

import hashlib
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.lifecycle_orchestrator import build_lifecycle_execution_plan, execute_lifecycle_plan
from memory.semantic_memory import build_embedding_record, save_embedding_record
from tools.migrate_semantic_memory import migrate_semantic_memory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "data" / "test"


class LifecycleOrchestratorTest(unittest.TestCase):
    def _fixture(self, rows=None):
        holder = TemporaryDirectory(dir=TEST_ROOT)
        root = Path(holder.name)
        source, semantic = root / "source.db", root / "semantic.db"
        rows = rows or [("s1", "first summary"), ("s2", "second summary")]
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute("CREATE TABLE memory_summaries (id TEXT PRIMARY KEY, memory_space TEXT, summary_type TEXT, conversation_id TEXT, summary_text TEXT)")
            conn.executemany("INSERT INTO memory_summaries VALUES (?, 'whale', 'conversation_summary', 'c1', ?)", rows)
        migrate_semantic_memory(semantic, write=True)
        return holder, source, semantic

    def _plan(self, source, semantic, max_actions=20):
        return build_lifecycle_execution_plan(source, semantic, "whale", "deterministic_test", max_actions)

    def _run(self, source, semantic, **kwargs):
        return execute_lifecycle_plan(source, semantic, "whale", "deterministic_test", **kwargs)

    def test_up_to_date_has_no_action(self):
        holder, source, semantic = self._fixture([( "s1", "current")])
        try:
            record = build_embedding_record("memory_summaries", "s1", "current", "whale", provider_name="deterministic_test")
            save_embedding_record(record, semantic, dry_run=False)
            self.assertEqual(0, self._plan(source, semantic)["action_count"])
        finally:
            holder.cleanup()

    def test_missing_and_stale_generate_expected_actions(self):
        holder, source, semantic = self._fixture()
        try:
            record = build_embedding_record("memory_summaries", "s2", "old text", "whale", provider_name="deterministic_test")
            save_embedding_record(record, semantic, dry_run=False)
            actions = {item["summary_id"]: item["action"] for item in self._plan(source, semantic)["items"]}
            self.assertEqual("create_embedding", actions["s1"])
            self.assertEqual("rebuild_embedding", actions["s2"])
        finally:
            holder.cleanup()

    def test_dry_run_does_not_write_and_confirmation_is_required(self):
        holder, source, semantic = self._fixture()
        try:
            before = semantic.read_bytes()
            dry = self._run(source, semantic)
            refused = self._run(source, semantic, execute=True)
            self.assertEqual("dry_run", dry["status"])
            self.assertEqual("refused", refused["status"])
            self.assertEqual(before, semantic.read_bytes())
        finally:
            holder.cleanup()

    def test_wrong_and_stale_plan_ids_are_refused(self):
        holder, source, semantic = self._fixture()
        try:
            plan = self._plan(source, semantic)
            wrong = self._run(source, semantic, execute=True, confirm_plan_id="not-the-plan")
            self.assertEqual("stale_plan", wrong["status"])
            with closing(sqlite3.connect(source)) as conn, conn:
                conn.execute("UPDATE memory_summaries SET summary_text = 'changed' WHERE id = 's1'")
            stale = self._run(source, semantic, execute=True, confirm_plan_id=plan["plan_id"])
            self.assertEqual("stale_plan", stale["status"])
        finally:
            holder.cleanup()

    def test_confirmed_execution_reaches_up_to_date_and_is_idempotent(self):
        holder, source, semantic = self._fixture()
        try:
            plan = self._plan(source, semantic)
            first = self._run(source, semantic, execute=True, confirm_plan_id=plan["plan_id"])
            second_plan = self._plan(source, semantic)
            second = self._run(source, semantic, execute=True, confirm_plan_id=second_plan["plan_id"])
            self.assertEqual("committed", first["status"])
            self.assertEqual({"up_to_date": 2}, first["after_status_counts"])
            self.assertEqual(0, second_plan["action_count"])
            self.assertEqual([], second["items"])
            with closing(sqlite3.connect(semantic)) as conn, conn:
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0])
        finally:
            holder.cleanup()

    def test_max_actions_and_real_target_refusal(self):
        holder, source, semantic = self._fixture()
        try:
            self.assertEqual(1, self._plan(source, semantic, max_actions=0)["action_count"])
            self.assertEqual(2, self._plan(source, semantic, max_actions=1000)["action_count"])
            refused = self._run(source, PROJECT_ROOT / "data" / "ayunos_chatgpt.db")
            self.assertEqual("refused", refused["status"])
        finally:
            holder.cleanup()

    def test_source_hash_is_unchanged_and_forbidden_integrations_are_absent(self):
        holder, source, semantic = self._fixture()
        try:
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            plan = self._plan(source, semantic)
            self._run(source, semantic, execute=True, confirm_plan_id=plan["plan_id"])
            self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
            text = (PROJECT_ROOT / "memory" / "lifecycle_orchestrator.py").read_text(encoding="utf-8")
            for forbidden in ("whale_chat", "FROM messages", "requests", "openai"):
                self.assertNotIn(forbidden, text)
        finally:
            holder.cleanup()


if __name__ == "__main__":
    unittest.main()
