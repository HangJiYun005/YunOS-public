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
    build_embedding_record,
    load_embedding_records,
    save_embedding_record,
    semantic_search_records,
)
from tools.migrate_semantic_memory import migrate_semantic_memory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"


class SemanticMemoryDbTest(unittest.TestCase):
    def test_migrate_semantic_memory_creates_table_in_test_database(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"

            result = migrate_semantic_memory(db_path, write=True)

            self.assertEqual("created", result["status"])
            with closing(sqlite3.connect(db_path)) as conn, conn:
                columns = {
                    row[1]: row[2]
                    for row in conn.execute("PRAGMA table_info(memory_embeddings)")
                }
                self.assertEqual("INTEGER", columns["id"])
                self.assertEqual("TEXT", columns["source_table"])
                self.assertEqual("TEXT", columns["embedding_json"])
                indexes = list(conn.execute("PRAGMA index_list(memory_embeddings)"))
                self.assertTrue(any(row[2] for row in indexes))

    def test_migrate_semantic_memory_rejects_real_database(self):
        real_db = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"

        with self.assertRaises(ValueError):
            migrate_semantic_memory(real_db, write=True)

    def test_save_embedding_record_writes_test_database(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            record = build_embedding_record("test_notes", "n1", "hello test db")

            result = save_embedding_record(record, db_path, dry_run=False)

            self.assertTrue(result["written"])
            self.assertEqual("inserted", result["status"])
            with closing(sqlite3.connect(db_path)) as conn, conn:
                count = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
            self.assertEqual(1, count)

    def test_save_embedding_record_rejects_real_database(self):
        real_db = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
        record = build_embedding_record("test_notes", "n1", "real db refused")

        with self.assertRaises(ValueError):
            save_embedding_record(record, real_db, dry_run=False)

    def test_save_embedding_record_rejects_missing_table(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            record = build_embedding_record("test_notes", "n1", "missing table")

            with self.assertRaises(RuntimeError):
                save_embedding_record(record, db_path, dry_run=False)

    def test_duplicate_save_is_skipped_without_crashing(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            record = build_embedding_record("test_notes", "n1", "duplicate")

            first = save_embedding_record(record, db_path, dry_run=False)
            second = save_embedding_record(record, db_path, dry_run=False)

            self.assertEqual("inserted", first["status"])
            self.assertEqual("duplicate", second["status"])
            self.assertFalse(second["written"])

    def test_load_embedding_records_reads_test_database_records(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            save_embedding_record(
                build_embedding_record(
                    "test_notes",
                    "n1",
                    "whale note",
                    memory_space="whale",
                    source_platform="manual",
                ),
                db_path,
                dry_run=False,
            )
            save_embedding_record(
                build_embedding_record("test_notes", "n2", "general note", memory_space="general"),
                db_path,
                dry_run=False,
            )

            records = load_embedding_records(db_path, memory_space="whale")

            self.assertEqual(1, len(records))
            self.assertEqual("n1", records[0]["source_id"])
            self.assertEqual("manual", records[0]["source_platform"])
            self.assertIn("embedding_json", records[0])

    def test_semantic_search_records_returns_top_k(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            for source_id, text in (
                ("n1", "alpha beta"),
                ("n2", "hello memory"),
                ("n3", "hello semantic memory"),
            ):
                save_embedding_record(
                    build_embedding_record("test_notes", source_id, text),
                    db_path,
                    dry_run=False,
                )

            results = semantic_search_records("hello memory", db_path, top_k=2)

            self.assertEqual(2, len(results))
            self.assertEqual(["n2", "n3"], [item["source_id"] for item in results])
            self.assertEqual(
                {"score", "source_table", "source_id", "memory_space", "content_preview"},
                set(results[0]),
            )

    def test_semantic_search_does_not_read_messages_table(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            save_embedding_record(
                build_embedding_record("test_notes", "n1", "hello memory"),
                db_path,
                dry_run=False,
            )

            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("CREATE VIEW messages AS SELECT text FROM missing_messages")

            results = semantic_search_records("hello", db_path, top_k=1)

            self.assertEqual(1, len(results))

    def test_cli_embed_write_can_write_test_database(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools" / "semantic_memory.py"),
                    "embed",
                    "--db",
                    str(db_path),
                    "--text",
                    "hello cli",
                    "--source-table",
                    "test_notes",
                    "--source-id",
                    "n1",
                    "--write",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("inserted", payload["save"]["status"])
            self.assertNotIn("embedding_json", json.dumps(payload))

    def test_cli_search_can_query_test_database(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            save_embedding_record(
                build_embedding_record("test_notes", "n1", "hello cli search"),
                db_path,
                dry_run=False,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "tools" / "semantic_memory.py"),
                    "search",
                    "--db",
                    str(db_path),
                    "--query",
                    "hello",
                    "--top-k",
                    "1",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(1, len(payload["results"]))
            self.assertIn("score", payload["results"][0])
            self.assertIn("content_preview", payload["results"][0])
            self.assertNotIn("embedding_json", json.dumps(payload))

    def test_cli_embed_write_rejects_real_database_without_traceback(self):
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "semantic_memory.py"),
                "embed",
                "--db",
                str(PROJECT_ROOT / "data" / "ayunos_chatgpt.db"),
                "--text",
                "must refuse",
                "--source-table",
                "test_notes",
                "--source-id",
                "n1",
                "--write",
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        payload = json.loads(result.stdout)
        self.assertEqual("refused", payload["status"])
        self.assertNotIn("Traceback", result.stderr)

    def test_does_not_call_api_or_llm(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "semantic_test.db"
            migrate_semantic_memory(db_path, write=True)
            save_embedding_record(
                build_embedding_record("test_notes", "n1", "local only"),
                db_path,
                dry_run=False,
            )

            with patch("socket.create_connection", side_effect=AssertionError("network not allowed")):
                results = semantic_search_records("local", db_path, top_k=1)

            self.assertEqual(1, len(results))

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
            PROJECT_ROOT / "tools" / "semantic_memory.py",
            PROJECT_ROOT / "tools" / "migrate_semantic_memory.py",
        ]

        for path in checked_paths:
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
