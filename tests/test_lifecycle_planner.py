from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.lifecycle_planner import (
    ACTION_BLOCKED,
    ACTION_CREATE_EMBEDDING,
    ACTION_MIGRATE_LEGACY_EMBEDDING,
    ACTION_NONE,
    ACTION_REBUILD_EMBEDDING,
    ACTION_REVIEW_SUMMARY,
    BLOCKED,
    EMBEDDING_IDENTITY_MISMATCH,
    EMBEDDING_MISSING,
    EMBEDDING_STALE,
    LEGACY_UNVERSIONED,
    SEMANTIC_TABLE_MISSING,
    SUMMARY_EMPTY,
    UP_TO_DATE,
    plan_memory_lifecycle,
)


IDENTITY = {
    "provider_name": "local_sentence_transformer",
    "model_name": "BAAI/bge-small-zh-v1.5",
    "model_revision": "fixture-revision",
    "model_fingerprint": "a" * 64,
    "embedding_protocol": "sentence_transformers_normalized_v1",
    "embedding_dim": 512,
    "normalized": True,
}


class _Provider:
    def get_embedding_identity(self):
        return dict(IDENTITY)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def create_source(path: Path, rows: list[dict]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE memory_summaries (id TEXT PRIMARY KEY, memory_space TEXT, summary_type TEXT, conversation_id TEXT, summary_text TEXT)"
        )
        conn.executemany(
            "INSERT INTO memory_summaries VALUES (?, ?, ?, ?, ?)",
            [(row["id"], row.get("memory_space", "whale"), row.get("summary_type", "conversation_summary"), row.get("conversation_id"), row.get("summary_text", "")) for row in rows],
        )


def create_semantic(path: Path, rows: list[dict], table: bool = True) -> None:
    with sqlite3.connect(path) as conn:
        if not table:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
            return
        conn.execute(
            """
            CREATE TABLE memory_embeddings (
                id INTEGER PRIMARY KEY, source_table TEXT, source_id TEXT, memory_space TEXT,
                embedding_provider TEXT, embedding_model TEXT, model_revision TEXT,
                model_fingerprint TEXT, embedding_protocol TEXT, embedding_normalized INTEGER,
                embedding_dim INTEGER, content_hash TEXT
            )
            """
        )
        for row in rows:
            value = {
                "source_table": "memory_summaries",
                "source_id": "1",
                "memory_space": "whale",
                "embedding_provider": IDENTITY["provider_name"],
                "embedding_model": IDENTITY["model_name"],
                "model_revision": IDENTITY["model_revision"],
                "model_fingerprint": IDENTITY["model_fingerprint"],
                "embedding_protocol": IDENTITY["embedding_protocol"],
                "embedding_normalized": 1,
                "embedding_dim": IDENTITY["embedding_dim"],
                "content_hash": _hash("summary"),
            }
            value.update(row)
            conn.execute(
                "INSERT INTO memory_embeddings (source_table, source_id, memory_space, embedding_provider, embedding_model, model_revision, model_fingerprint, embedding_protocol, embedding_normalized, embedding_dim, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(value[key] for key in ("source_table", "source_id", "memory_space", "embedding_provider", "embedding_model", "model_revision", "model_fingerprint", "embedding_protocol", "embedding_normalized", "embedding_dim", "content_hash")),
            )


class LifecyclePlannerTest(unittest.TestCase):
    def _plan(self, source: Path, semantic: Path, memory_space=None):
        with patch("memory.lifecycle_planner.get_embedding_provider", return_value=_Provider()):
            return plan_memory_lifecycle(source, semantic, memory_space=memory_space)

    def _fixture(self, summary_text="summary", embedding=None):
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(directory.name)
        source, semantic = root / "source.db", root / "semantic.db"
        create_source(source, [{"id": "1", "summary_text": summary_text}])
        create_semantic(semantic, [] if embedding is None else [embedding])
        return directory, source, semantic

    def test_full_match_is_up_to_date_and_readonly(self):
        holder, source, semantic = self._fixture(embedding={"content_hash": _hash("summary")})
        try:
            before = (source.read_bytes(), semantic.read_bytes())
            plan = self._plan(source, semantic)
            self.assertEqual(UP_TO_DATE, plan["items"][0]["status"])
            self.assertEqual(ACTION_NONE, plan["items"][0]["recommended_action"])
            self.assertTrue(plan["readonly"])
            self.assertFalse(plan["executed"])
            self.assertEqual(before, (source.read_bytes(), semantic.read_bytes()))
        finally:
            holder.cleanup()

    def test_missing_stale_and_identity_variants_have_expected_actions(self):
        cases = [
            (None, EMBEDDING_MISSING, ACTION_CREATE_EMBEDDING),
            ({"content_hash": _hash("old")}, EMBEDDING_STALE, ACTION_REBUILD_EMBEDDING),
            ({"model_fingerprint": "b" * 64, "content_hash": _hash("summary")}, EMBEDDING_IDENTITY_MISMATCH, ACTION_REBUILD_EMBEDDING),
            ({"embedding_protocol": "other", "content_hash": _hash("summary")}, EMBEDDING_IDENTITY_MISMATCH, ACTION_REBUILD_EMBEDDING),
            ({"embedding_provider": LEGACY_UNVERSIONED, "content_hash": _hash("summary")}, LEGACY_UNVERSIONED, ACTION_MIGRATE_LEGACY_EMBEDDING),
        ]
        for embedding, status, action in cases:
            with self.subTest(status=status):
                holder, source, semantic = self._fixture(embedding=embedding)
                try:
                    item = self._plan(source, semantic)["items"][0]
                    self.assertEqual(status, item["status"])
                    self.assertEqual(action, item["recommended_action"])
                finally:
                    holder.cleanup()

    def test_current_match_takes_priority_over_legacy(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, semantic = root / "source.db", root / "semantic.db"
            create_source(source, [{"id": "1", "summary_text": "summary"}])
            create_semantic(semantic, [
                {"embedding_provider": LEGACY_UNVERSIONED},
                {"content_hash": _hash("summary")},
            ])
            item = self._plan(source, semantic)["items"][0]
            self.assertEqual(UP_TO_DATE, item["status"])
            self.assertEqual(ACTION_NONE, item["recommended_action"])
            self.assertEqual(2, item["matching_embedding_count"])
            self.assertEqual(1, item["compatible_embedding_count"])

    def test_stale_current_takes_priority_over_legacy(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, semantic = root / "source.db", root / "semantic.db"
            create_source(source, [{"id": "1", "summary_text": "summary"}])
            create_semantic(semantic, [
                {"embedding_provider": LEGACY_UNVERSIONED},
                {"content_hash": _hash("old")},
            ])
            item = self._plan(source, semantic)["items"][0]
            self.assertEqual(EMBEDDING_STALE, item["status"])
            self.assertEqual(ACTION_REBUILD_EMBEDDING, item["recommended_action"])

    def test_identity_mismatch_takes_priority_over_legacy(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, semantic = root / "source.db", root / "semantic.db"
            create_source(source, [{"id": "1", "summary_text": "summary"}])
            create_semantic(semantic, [
                {"embedding_provider": LEGACY_UNVERSIONED},
                {"model_fingerprint": "b" * 64, "content_hash": _hash("summary")},
            ])
            item = self._plan(source, semantic)["items"][0]
            self.assertEqual(EMBEDDING_IDENTITY_MISMATCH, item["status"])
            self.assertEqual(ACTION_REBUILD_EMBEDDING, item["recommended_action"])

    def test_embedding_order_does_not_change_result(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source = root / "source.db"
            create_source(source, [{"id": "1", "summary_text": "summary"}])
            plans = []
            for name, rows in (("first.db", [
                {"embedding_provider": LEGACY_UNVERSIONED},
                {"content_hash": _hash("summary")},
            ]), ("second.db", [
                {"content_hash": _hash("summary")},
                {"embedding_provider": LEGACY_UNVERSIONED},
            ])):
                semantic = root / name
                create_semantic(semantic, rows)
                plans.append(self._plan(source, semantic)["items"][0])
            self.assertEqual(plans[0], plans[1])

    def test_one_matching_current_among_multiple_current_is_up_to_date(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, semantic = root / "source.db", root / "semantic.db"
            create_source(source, [{"id": "1", "summary_text": "summary"}])
            create_semantic(semantic, [
                {"model_fingerprint": "b" * 64, "content_hash": _hash("summary")},
                {"content_hash": _hash("summary")},
            ])
            item = self._plan(source, semantic)["items"][0]
            self.assertEqual(UP_TO_DATE, item["status"])
            self.assertEqual(2, item["matching_embedding_count"])
            self.assertEqual(1, item["compatible_embedding_count"])

    def test_empty_summary_requires_review(self):
        holder, source, semantic = self._fixture(summary_text="", embedding={})
        try:
            item = self._plan(source, semantic)["items"][0]
            self.assertEqual(SUMMARY_EMPTY, item["status"])
            self.assertEqual(ACTION_REVIEW_SUMMARY, item["recommended_action"])
        finally:
            holder.cleanup()

    def test_missing_semantic_table_returns_safe_plan(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, semantic = root / "source.db", root / "semantic.db"
            create_source(source, [{"id": "1", "summary_text": "summary"}])
            create_semantic(semantic, [], table=False)
            plan = self._plan(source, semantic)
            self.assertEqual(SEMANTIC_TABLE_MISSING, plan["overall_status"])
            self.assertEqual(SEMANTIC_TABLE_MISSING, plan["items"][0]["status"])
            self.assertEqual(ACTION_BLOCKED, plan["items"][0]["recommended_action"])

    def test_damaged_source_is_blocked_without_path_disclosure(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, semantic = root / "source.db", root / "semantic.db"
            source.write_bytes(b"not sqlite")
            create_semantic(semantic, [])
            plan = self._plan(source, semantic)
            self.assertEqual(BLOCKED, next(iter(plan["status_counts"])))
            self.assertNotIn(str(source), json.dumps(plan))

    def test_memory_space_filter_and_safe_output(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, semantic = root / "source.db", root / "semantic.db"
            create_source(source, [
                {"id": "1", "memory_space": "whale", "summary_text": "secret whale summary"},
                {"id": "2", "memory_space": "general", "summary_text": "secret general summary"},
            ])
            create_semantic(semantic, [{"source_id": "1", "content_hash": _hash("secret whale summary")}])
            plan = self._plan(source, semantic, memory_space="whale")
            encoded = json.dumps(plan)
            self.assertEqual(1, plan["summary_count"])
            self.assertNotIn("secret whale summary", encoded)
            self.assertNotIn("embedding_json", encoded)
            self.assertNotIn(str(source), encoded)

    def test_planner_does_not_call_network_or_read_messages(self):
        holder, source, semantic = self._fixture(embedding={})
        try:
            with sqlite3.connect(source) as conn:
                conn.execute("CREATE VIEW messages AS SELECT text FROM missing_messages")
            with patch("socket.create_connection", side_effect=AssertionError("network not allowed")):
                self._plan(source, semantic)
        finally:
            holder.cleanup()

    def test_cli_has_no_write_flag(self):
        text = (Path(__file__).resolve().parents[1] / "tools" / "plan_memory_lifecycle.py").read_text(encoding="utf-8")
        self.assertNotIn('"--write"', text)
        self.assertIn("--real-readonly", text)


if __name__ == "__main__":
    unittest.main()
