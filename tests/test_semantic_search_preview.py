from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.semantic_memory import (
    SemanticRetrievalError,
    build_embedding_record,
    save_embedding_record,
    semantic_summary_search_preview,
)
from tools.migrate_semantic_memory import migrate_semantic_memory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"


def create_preview_semantic_db(path: Path) -> None:
    migrate_semantic_memory(path, write=True)
    records = [
        build_embedding_record(
            "memory_summaries",
            "1",
            "我们之前聊过莫泊桑和鲸鱼，也聊过写作记忆。",
            memory_space="whale",
            metadata={
                "summary_type": "conversation_summary",
                "conversation_id": "conv-whale",
                "method": "extractive_v1",
                "source_message_count": 8,
            },
        ),
        build_embedding_record(
            "memory_summaries",
            "2",
            "General summary about local tools and semantic preview.",
            memory_space="general",
            metadata={
                "summary_type": "memory_space_overview",
                "conversation_id": None,
                "method": "extractive_v1",
                "source_message_count": 4,
            },
        ),
        build_embedding_record(
            "messages",
            "msg-1",
            "This message record must not be searched.",
            memory_space="whale",
            metadata={"summary_type": "not_a_summary"},
        ),
    ]
    for record in records:
        save_embedding_record(record, path, dry_run=False)


class SemanticSearchPreviewTest(unittest.TestCase):
    def _write_corrupt_record(self, path: Path, embedding_json: str, embedding_dim: int = 32) -> None:
        migrate_semantic_memory(path, write=True)
        record = build_embedding_record("memory_summaries", "bad", "corrupt vector")
        record["embedding_json"] = embedding_json
        record["embedding_dim"] = embedding_dim
        save_embedding_record(record, path, dry_run=False)

    def test_preview_queries_only_summary_embeddings(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(db_path)

            result = semantic_summary_search_preview("鲸鱼", db_path, top_k=5)

            self.assertEqual("鲸鱼", result["query"])
            self.assertEqual("deterministic_test", result["provider"])
            self.assertEqual(32, result["embedding_dim"])
            self.assertEqual(2, result["candidate_count"])
            self.assertEqual(2, result["result_count"])
            self.assertLessEqual(len(result["results"]), 2)
            self.assertTrue(all(item["source_table"] == "memory_summaries" for item in result["results"]))
            self.assertNotIn("embedding_json", json.dumps(result, ensure_ascii=False))

    def test_memory_space_filter_top_k_clamp_and_min_score(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(db_path)

            result = semantic_summary_search_preview(
                "鲸鱼",
                db_path,
                memory_space="whale",
                top_k=99,
                min_score=0.0,
            )

            self.assertEqual("whale", result["memory_space"])
            self.assertEqual(20, result["top_k"])
            self.assertEqual(1, result["candidate_count"])
            self.assertLessEqual(result["result_count"], 1)
            self.assertTrue(all(item["score"] >= 0.0 for item in result["results"]))

    def test_invalid_metadata_json_does_not_crash(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            record = build_embedding_record("memory_summaries", "bad", "metadata bad", metadata={})
            record["metadata_json"] = "{bad json"
            save_embedding_record(record, db_path, dry_run=False)

            result = semantic_summary_search_preview("metadata", db_path)

            self.assertEqual(1, result["result_count"])
            self.assertIsNone(result["results"][0]["summary_type"])
            self.assertIsNone(result["results"][0]["conversation_id"])

    def test_missing_table_returns_readonly_error_without_migration(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            sqlite3.connect(db_path).close()

            result = semantic_summary_search_preview("hello", db_path)

            self.assertEqual("memory_embeddings table is missing", result["readonly_error"])
            self.assertEqual(0, result["candidate_count"])
            self.assertEqual(0, result["result_count"])
            with closing(sqlite3.connect(db_path)) as conn, conn:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_embeddings'"
                ).fetchone()
            self.assertIsNone(exists)

    def test_preview_does_not_read_messages_table_or_write_db(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(db_path)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("CREATE VIEW messages AS SELECT text FROM missing_messages")
                before = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]

            result = semantic_summary_search_preview("鲸鱼", db_path)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                after = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]

            self.assertEqual(2, result["candidate_count"])
            self.assertEqual(before, after)

    def test_preview_does_not_call_api_or_llm(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(db_path)

            with patch("socket.create_connection", side_effect=AssertionError("network not allowed")):
                result = semantic_summary_search_preview("鲸鱼", db_path)

            self.assertEqual(2, result["candidate_count"])

    def test_corrupt_schema_and_vectors_raise_semantic_retrieval_error(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            root = Path(tmp)
            cases = {
                "missing_dim": "CREATE TABLE memory_embeddings (id INTEGER PRIMARY KEY, embedding_json TEXT)",
                "missing_json": "CREATE TABLE memory_embeddings (id INTEGER PRIMARY KEY, embedding_dim INTEGER)",
            }
            for name, sql in cases.items():
                path = root / f"{name}.db"
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute(sql)
                with self.subTest(name=name), self.assertRaises(SemanticRetrievalError):
                    semantic_summary_search_preview("bad", path)

            vector_cases = ["{bad json", "{}", "[1, \"bad\"]", "[1, null]"]
            for index, value in enumerate(vector_cases):
                path = root / f"bad-vector-{index}.db"
                self._write_corrupt_record(path, value)
                with self.subTest(value=value), self.assertRaises(SemanticRetrievalError):
                    semantic_summary_search_preview("bad", path)

            path = root / "bad-dimension.db"
            self._write_corrupt_record(path, "[1.0]", embedding_dim=32)
            with self.assertRaises(SemanticRetrievalError):
                semantic_summary_search_preview("bad", path)

    def test_damaged_sqlite_and_query_operational_error_raise_semantic_retrieval_error(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            damaged = Path(tmp) / "damaged.db"
            damaged.write_bytes(b"not a sqlite database")
            with self.assertRaises(SemanticRetrievalError):
                semantic_summary_search_preview("bad", damaged)

        with patch(
            "memory.semantic_memory.connect_readonly",
            side_effect=sqlite3.OperationalError("query failed"),
        ):
            with self.assertRaises(SemanticRetrievalError):
                semantic_summary_search_preview("bad", PROJECT_ROOT / "data" / "test" / "semantic_test.db")

    def test_cli_can_query_test_semantic_db_json(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            create_preview_semantic_db(db_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools" / "semantic_search_preview.py"),
                    "--db",
                    str(db_path),
                    "--query",
                    "鲸鱼",
                    "--memory-space",
                    "whale",
                    "--top-k",
                    "5",
                    "--json",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["readonly"])
            self.assertEqual("deterministic_test", payload["provider"])
            self.assertTrue(payload["provider_is_test_only"])
            self.assertIn("warning", payload)
            self.assertEqual(1, payload["candidate_count"])
            self.assertNotIn("embedding_json", result.stdout)

    def test_code_does_not_integrate_forbidden_surfaces(self):
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
            PROJECT_ROOT / "memory" / "semantic_memory.py",
            PROJECT_ROOT / "tools" / "semantic_search_preview.py",
        ]

        for path in checked_paths:
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
