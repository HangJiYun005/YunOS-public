from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.semantic_memory import build_embedding_records_from_summaries, load_embedding_records
from tools.migrate_semantic_memory import migrate_semantic_memory
from tools.build_semantic_index import build_semantic_index


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"


def create_source_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE memory_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_space TEXT NOT NULL,
                conversation_id TEXT,
                summary_type TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                keywords_json TEXT,
                source_message_count INTEGER DEFAULT 0,
                source_conversation_count INTEGER DEFAULT 0,
                method TEXT DEFAULT 'extractive_v1',
                model_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO memory_summaries (
                memory_space,
                conversation_id,
                summary_type,
                summary_text,
                source_message_count,
                method,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("whale", "conv-1", "conversation_summary", "hello summary memory", 3, "extractive_v1", "2026-07-07"),
                ("general", None, "memory_space_overview", "general overview text", 5, "extractive_v1", "2026-07-07"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


class BuildSemanticIndexTest(unittest.TestCase):
    def test_build_embedding_records_from_summary_rows(self):
        rows = [
            {
                "id": 7,
                "memory_space": "whale",
                "conversation_id": "conv-1",
                "summary_type": "conversation_summary",
                "summary_text": "x" * 220,
                "source_message_count": 12,
                "method": "extractive_v1",
            }
        ]

        records, skipped = build_embedding_records_from_summaries(rows)

        self.assertEqual(0, skipped)
        self.assertEqual(1, len(records))
        self.assertEqual("memory_summaries", records[0]["source_table"])
        self.assertEqual("7", records[0]["source_id"])
        self.assertEqual("whale", records[0]["memory_space"])
        self.assertIsNone(records[0]["source_platform"])
        self.assertEqual("x" * 200, records[0]["content_preview"])
        metadata = json.loads(records[0]["metadata_json"])
        self.assertEqual("legacy", metadata["summary_source"])
        self.assertEqual("7", metadata["summary_id"])
        self.assertEqual("conversation_summary", metadata["summary_type"])
        self.assertEqual("conv-1", metadata["conversation_id"])
        self.assertIsNone(metadata["summary_version"])
        self.assertEqual(records[0]["content_hash"], metadata["summary_text_hash"])
        self.assertIsNone(metadata["source_snapshot_hash"])
        self.assertEqual("whale", metadata["memory_space"])
        self.assertEqual("summary_embedding", metadata["semantic_target_kind"])
        self.assertEqual("extractive_v1", metadata["method"])
        self.assertEqual(12, metadata["source_message_count"])

    def test_dry_run_does_not_write_target_db(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            target_db = Path(tmp) / "target_test.db"
            create_source_db(source_db)
            migrate_semantic_memory(target_db, write=True)

            result = build_semantic_index(source_db, target_db, write=False)

            self.assertTrue(result["dry_run"])
            self.assertEqual(2, result["source_count"])
            self.assertEqual(0, result["inserted_count"])
            self.assertEqual(0, len(load_embedding_records(target_db)))

    def test_write_indexes_summaries_into_target_test_db(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            target_db = Path(tmp) / "target_test.db"
            create_source_db(source_db)
            migrate_semantic_memory(target_db, write=True)

            result = build_semantic_index(source_db, target_db, write=True)

            self.assertFalse(result["dry_run"])
            self.assertEqual(2, result["source_count"])
            self.assertEqual(2, result["inserted_count"])
            self.assertEqual(0, result["duplicate_count"])
            self.assertEqual("deterministic_test", result["provider"])
            self.assertEqual(32, result["embedding_dim"])
            self.assertEqual("memory_summaries", result["source_table"])
            self.assertEqual("test", result["target_db_mode"])
            records = load_embedding_records(target_db, source_table="memory_summaries")
            self.assertEqual(2, len(records))

    def test_target_missing_memory_embeddings_table_is_rejected(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            target_db = Path(tmp) / "target_test.db"
            create_source_db(source_db)
            sqlite3.connect(target_db).close()

            with self.assertRaises(RuntimeError):
                build_semantic_index(source_db, target_db, write=True)

    def test_target_real_database_path_is_rejected(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            create_source_db(source_db)

            with self.assertRaises(ValueError):
                build_semantic_index(source_db, PROJECT_ROOT / "data" / "ayunos_chatgpt.db", write=True)

    def test_real_source_requires_real_readonly_flag(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            target_db = Path(tmp) / "target_test.db"
            migrate_semantic_memory(target_db, write=True)

            with self.assertRaises(ValueError):
                build_semantic_index(
                    PROJECT_ROOT / "data" / "ayunos_chatgpt.db",
                    target_db,
                    real_readonly=False,
                    write=False,
                )

    def test_build_reads_only_memory_summaries_not_messages(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            target_db = Path(tmp) / "target_test.db"
            create_source_db(source_db)
            conn = sqlite3.connect(source_db)
            try:
                conn.execute("CREATE VIEW messages AS SELECT text FROM missing_messages")
                conn.commit()
            finally:
                conn.close()
            migrate_semantic_memory(target_db, write=True)

            result = build_semantic_index(source_db, target_db, write=True)

            self.assertEqual(2, result["inserted_count"])

    def test_empty_summary_text_is_skipped(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            target_db = Path(tmp) / "target_test.db"
            create_source_db(source_db)
            conn = sqlite3.connect(source_db)
            try:
                conn.execute(
                    """
                    INSERT INTO memory_summaries (
                        memory_space, conversation_id, summary_type, summary_text,
                        source_message_count, method, created_at
                    )
                    VALUES ('whale', 'conv-empty', 'conversation_summary', '', 0, 'extractive_v1', '2026-07-07')
                    """
                )
                conn.commit()
            finally:
                conn.close()
            migrate_semantic_memory(target_db, write=True)

            result = build_semantic_index(source_db, target_db, write=True)

            self.assertEqual(3, result["source_count"])
            self.assertEqual(2, result["inserted_count"])
            self.assertEqual(1, result["skipped_count"])

    def test_duplicate_records_do_not_crash(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            target_db = Path(tmp) / "target_test.db"
            create_source_db(source_db)
            migrate_semantic_memory(target_db, write=True)

            first = build_semantic_index(source_db, target_db, write=True)
            second = build_semantic_index(source_db, target_db, write=True)

            self.assertEqual(2, first["inserted_count"])
            self.assertEqual(0, second["inserted_count"])
            self.assertEqual(2, second["duplicate_count"])

    def test_cli_output_omits_embedding_json(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            target_db = Path(tmp) / "target_test.db"
            create_source_db(source_db)
            migrate_semantic_memory(target_db, write=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools" / "build_semantic_index.py"),
                    "--source-db",
                    str(source_db),
                    "--target-db",
                    str(target_db),
                    "--source",
                    "memory_summaries",
                    "--write",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(2, payload["inserted_count"])
            self.assertNotIn("embedding_json", result.stdout)

    def test_does_not_call_api_or_llm(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            source_db = Path(tmp) / "source_test.db"
            target_db = Path(tmp) / "target_test.db"
            create_source_db(source_db)
            migrate_semantic_memory(target_db, write=True)

            with patch("socket.create_connection", side_effect=AssertionError("network not allowed")):
                result = build_semantic_index(source_db, target_db, write=True)

            self.assertEqual(2, result["inserted_count"])

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
            PROJECT_ROOT / "tools" / "build_semantic_index.py",
        ]

        for path in checked_paths:
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
