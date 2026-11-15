import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def create_summary_fixture(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                update_time REAL,
                update_time_iso TEXT,
                memory_space TEXT DEFAULT 'general',
                metadata_json TEXT
            );
            CREATE TABLE messages (
                message_id TEXT,
                conversation_id TEXT,
                conversation_title TEXT,
                ordinal INTEGER,
                role TEXT,
                create_time REAL,
                create_time_iso TEXT,
                text TEXT,
                memory_space TEXT DEFAULT 'general',
                metadata_json TEXT
            );
            """
        )
        conversations = [
            ("whale-conv", "Whale repair planning", "whale", 20),
            ("general-conv", "General study notes", "general", 10),
        ]
        for conversation_id, title, memory_space, update_time in conversations:
            conn.execute(
                """
                INSERT INTO conversations
                (conversation_id, title, update_time, update_time_iso, memory_space, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    title,
                    update_time,
                    "2026-07-05T00:00:00+08:00",
                    memory_space,
                    json.dumps({"memory_space": memory_space}),
                ),
            )
        message_rows = [
            (
                "whale-1",
                "whale-conv",
                "Whale repair planning",
                0,
                "user",
                "Need a careful backup-first plan for memory summary boundaries.",
                "whale",
            ),
            (
                "whale-2",
                "whale-conv",
                "Whale repair planning",
                1,
                "assistant",
                "Conclusion: keep real database readonly and preserve memory_space filters.",
                "whale",
            ),
            (
                "general-1",
                "general-conv",
                "General study notes",
                0,
                "user",
                "General notes mention summary boundaries but should stay separate.",
                "general",
            ),
            (
                "general-2",
                "general-conv",
                "General study notes",
                1,
                "assistant",
                "Conclusion: general scope remains independent.",
                "general",
            ),
        ]
        for ordinal, row in enumerate(message_rows):
            message_id, conversation_id, title, local_ordinal, role, text, memory_space = row
            conn.execute(
                """
                INSERT INTO messages
                (message_id, conversation_id, conversation_title, ordinal, role, create_time,
                 create_time_iso, text, memory_space, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    title,
                    local_ordinal,
                    role,
                    100 + ordinal,
                    "2026-07-05T00:00:00+08:00",
                    text,
                    memory_space,
                    json.dumps({"memory_space": memory_space}),
                ),
            )
        conn.commit()


def table_count(db_path: Path, table_name: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


class MemorySummarizerTest(unittest.TestCase):
    def sample_summary(self, memory_space: str = "whale") -> dict:
        return {
            "memory_space": memory_space,
            "conversation_id": "whale-conv" if memory_space == "whale" else "general-conv",
            "summary_type": "conversation_summary",
            "summary_text": "controlled test write",
            "keywords_json": json.dumps(["controlled", "test"]),
            "source_message_count": 2,
            "source_conversation_count": 1,
            "created_at": "2026-07-05T00:00:00+08:00",
            "metadata_json": json.dumps({"test": True}),
        }

    def test_build_conversation_summary_returns_dict(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            with patch.object(summarizer, "DEFAULT_DB_PATH", db_path):
                summary = summarizer.build_conversation_summary("whale-conv", memory_space="whale")

        self.assertIsInstance(summary, dict)
        self.assertEqual(summary["summary_type"], "conversation_summary")
        self.assertEqual(summary["conversation_id"], "whale-conv")
        self.assertEqual(summary["memory_space"], "whale")
        self.assertEqual(summary["source_message_count"], 2)
        self.assertIn("Whale repair planning", summary["summary_text"])

    def test_dry_run_does_not_write_database(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            summary = {
                "memory_space": "whale",
                "conversation_id": "whale-conv",
                "summary_type": "conversation_summary",
                "summary_text": "dry run only",
                "created_at": "2026-07-05T00:00:00+08:00",
            }

            result = summarizer.save_summary(summary, db_path, dry_run=True)

            with sqlite3.connect(db_path) as conn:
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_summaries'"
                ).fetchone()

        self.assertTrue(result["dry_run"])
        self.assertFalse(result["written"])
        self.assertIsNone(table)

    def test_migration_creates_memory_summaries_in_test_database(self):
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            result = migrate(db_path)
            second = migrate(db_path)
            with sqlite3.connect(db_path) as conn:
                columns = [row[1] for row in conn.execute("PRAGMA table_info(memory_summaries)")]

        self.assertTrue(result["migrated"])
        self.assertFalse(second["migrated"])
        self.assertIn("summary_text", columns)
        self.assertIn("memory_space", columns)

    def test_save_summary_rejects_real_database_path(self):
        from memory import summarizer

        real_path = Path(__file__).resolve().parents[1] / "data" / "ayunos_chatgpt.db"
        result = summarizer.save_summary(self.sample_summary(), real_path, dry_run=False)

        self.assertFalse(result["written"])
        self.assertEqual(
            result["error"],
            "Refusing to write to the real database without --allow-real-write and --confirm-real-write.",
        )

    def test_real_database_path_rejects_allow_without_confirm(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "ayunos_chatgpt.db"
            create_summary_fixture(db_path)

            result = summarizer.save_summary(
                self.sample_summary(),
                db_path,
                dry_run=False,
                allow_real_write=True,
            )

        self.assertFalse(result["written"])
        self.assertIn("without --allow-real-write and --confirm-real-write", result["error"])

    def test_real_database_path_rejects_confirm_without_allow(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "ayunos_chatgpt.db"
            create_summary_fixture(db_path)

            result = summarizer.save_summary(
                self.sample_summary(),
                db_path,
                dry_run=False,
                confirm_real_write=True,
            )

        self.assertFalse(result["written"])
        self.assertIn("without --allow-real-write and --confirm-real-write", result["error"])

    def test_real_database_migration_creates_backup(self):
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "ayunos_chatgpt.db"
            backup_dir = root / "backups"
            create_summary_fixture(db_path)

            result = migrate(
                db_path,
                allow_real_write=True,
                confirm_real_write=True,
                backup_dir=backup_dir,
            )
            backup_path = Path(result["backup_path"])
            backup_exists = backup_path.is_file()
            backup_size = backup_path.stat().st_size if backup_exists else 0

        self.assertTrue(result["is_real_db"])
        self.assertTrue(result["table_exists_after"])
        self.assertTrue(backup_exists)
        self.assertGreater(backup_size, 0)

    def test_real_database_migration_only_creates_summary_table(self):
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "ayunos_chatgpt.db"
            backup_dir = root / "backups"
            create_summary_fixture(db_path)
            before_messages = table_count(db_path, "messages")
            before_conversations = table_count(db_path, "conversations")

            migrate(db_path, allow_real_write=True, confirm_real_write=True, backup_dir=backup_dir)
            after_messages = table_count(db_path, "messages")
            after_conversations = table_count(db_path, "conversations")

        self.assertEqual(after_messages, before_messages)
        self.assertEqual(after_conversations, before_conversations)

    def test_save_summary_real_database_requires_allow_and_confirm(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "ayunos_chatgpt.db"
            create_summary_fixture(db_path)
            result = summarizer.save_summary(self.sample_summary(), db_path, dry_run=False)

        self.assertFalse(result["written"])
        self.assertIn("without --allow-real-write and --confirm-real-write", result["error"])

    def test_save_summary_real_database_allows_whale_after_migration(self):
        from memory import summarizer
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "ayunos_chatgpt.db"
            backup_dir = root / "backups"
            create_summary_fixture(db_path)
            migrate(db_path, allow_real_write=True, confirm_real_write=True, backup_dir=backup_dir)
            with patch.object(summarizer, "BACKUP_DIR", backup_dir):
                result = summarizer.save_summary(
                    self.sample_summary("whale"),
                    db_path,
                    dry_run=False,
                    allow_real_write=True,
                    confirm_real_write=True,
                )
            count = table_count(db_path, "memory_summaries")

        self.assertTrue(result["written"])
        self.assertEqual(count, 1)
        self.assertIsNotNone(result["backup_path"])

    def test_save_summary_real_database_rejects_general(self):
        from memory import summarizer
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "ayunos_chatgpt.db"
            backup_dir = root / "backups"
            create_summary_fixture(db_path)
            migrate(db_path, allow_real_write=True, confirm_real_write=True, backup_dir=backup_dir)
            with patch.object(summarizer, "BACKUP_DIR", backup_dir):
                result = summarizer.save_summary(
                    self.sample_summary("general"),
                    db_path,
                    dry_run=False,
                    allow_real_write=True,
                    confirm_real_write=True,
                )
            count = table_count(db_path, "memory_summaries")

        self.assertFalse(result["written"])
        self.assertEqual(result["error"], "v3-C only allows real database writes for memory_space=whale.")
        self.assertEqual(count, 0)

    def test_save_summary_real_database_rejects_duplicate_summary(self):
        from memory import summarizer
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "ayunos_chatgpt.db"
            backup_dir = root / "backups"
            create_summary_fixture(db_path)
            migrate(db_path, allow_real_write=True, confirm_real_write=True, backup_dir=backup_dir)
            with patch.object(summarizer, "BACKUP_DIR", backup_dir):
                first = summarizer.save_summary(
                    self.sample_summary("whale"),
                    db_path,
                    dry_run=False,
                    allow_real_write=True,
                    confirm_real_write=True,
                )
                second = summarizer.save_summary(
                    self.sample_summary("whale"),
                    db_path,
                    dry_run=False,
                    allow_real_write=True,
                    confirm_real_write=True,
                )
            count = table_count(db_path, "memory_summaries")

        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        self.assertEqual(second["error"], "Summary already exists. Use --replace-summary to replace it.")
        self.assertEqual(count, 1)

    def test_save_summary_writes_test_database_after_migration(self):
        from memory import summarizer
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            migrate(db_path)
            result = summarizer.save_summary(self.sample_summary(), db_path, dry_run=False)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, memory_space, conversation_id, summary_text
                    FROM memory_summaries
                    """
                ).fetchone()

        self.assertTrue(result["written"])
        self.assertEqual(result["inserted_id"], row[0])
        self.assertEqual(row[1:], ("whale", "whale-conv", "controlled test write"))

    def test_save_summary_rejects_missing_schema_without_creating_table(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            result = summarizer.save_summary(self.sample_summary(), db_path, dry_run=False)
            with sqlite3.connect(db_path) as conn:
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_summaries'"
                ).fetchone()

        self.assertFalse(result["written"])
        self.assertEqual(result["error"], "memory_summaries schema is missing; run migration first")
        self.assertIsNone(table)

    def test_dry_run_does_not_write_real_database(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "ayunos_chatgpt.db"
            create_summary_fixture(db_path)

            result = summarizer.save_summary(
                self.sample_summary("whale"),
                db_path,
                dry_run=True,
                allow_real_write=True,
                confirm_real_write=True,
            )
            with sqlite3.connect(db_path) as conn:
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_summaries'"
                ).fetchone()

        self.assertFalse(result["written"])
        self.assertIsNone(table)

    def test_unknown_memory_space_does_not_cross_domain(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            with patch.object(summarizer, "DEFAULT_DB_PATH", db_path):
                summary = summarizer.build_memory_space_overview(memory_space="unknown")

        self.assertEqual(summary["memory_space"], "unknown")
        self.assertEqual(summary["source_conversation_count"], 0)
        self.assertIn("内容不足", summary["summary_text"])

    def test_whale_summary_only_reads_whale(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            with patch.object(summarizer, "DEFAULT_DB_PATH", db_path):
                summary = summarizer.build_memory_space_overview(memory_space="whale")

        self.assertEqual(summary["source_conversation_count"], 1)
        self.assertEqual(summary["source_message_count"], 2)
        self.assertIn("Whale repair planning", summary["summary_text"])
        self.assertNotIn("General study notes", summary["summary_text"])

    def test_general_summary_only_reads_general(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            with patch.object(summarizer, "DEFAULT_DB_PATH", db_path):
                summary = summarizer.build_memory_space_overview(memory_space="general")

        self.assertEqual(summary["source_conversation_count"], 1)
        self.assertEqual(summary["source_message_count"], 2)
        self.assertIn("General study notes", summary["summary_text"])
        self.assertNotIn("Whale repair planning", summary["summary_text"])

    def test_save_summary_dry_run_does_not_create_or_modify_table(self):
        from memory import summarizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            before_mtime = db_path.stat().st_mtime_ns
            result = summarizer.save_summary(
                {
                    "memory_space": "general",
                    "summary_type": "memory_space_overview",
                    "summary_text": "dry run only",
                    "created_at": "2026-07-05T00:00:00+08:00",
                },
                db_path,
                dry_run=True,
            )
            after_mtime = db_path.stat().st_mtime_ns
            with sqlite3.connect(db_path) as conn:
                tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='memory_summaries'"
                ).fetchone()[0]

        self.assertEqual(before_mtime, after_mtime)
        self.assertEqual(tables, 0)
        self.assertFalse(result["written"])

    def test_cli_dry_run_exits_successfully(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/summarize_memory.py",
                    "--db",
                    str(db_path),
                    "--memory-space",
                    "whale",
                    "--conversation-id",
                    "whale-conv",
                    "--dry-run",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"dry_run": true', result.stdout)
        self.assertIn('"written": false', result.stdout)

    def test_cli_write_requires_explicit_db(self):
        result = subprocess.run(
            [
                sys.executable,
                "tools/summarize_memory.py",
                "--memory-space",
                "whale",
                "--conversation-id",
                "whale-conv",
                "--write",
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--write requires explicit --db", result.stderr)

    def test_cli_write_to_migrated_test_database_succeeds(self):
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_fixture(db_path)
            migrate(db_path)
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/summarize_memory.py",
                    "--db",
                    str(db_path),
                    "--memory-space",
                    "whale",
                    "--conversation-id",
                    "whale-conv",
                    "--write",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
            )
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM memory_summaries").fetchone()[0]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(count, 1)
        self.assertIn('"written": true', result.stdout)

    def test_cli_write_to_real_database_is_rejected(self):
        real_path = Path(__file__).resolve().parents[1] / "data" / "ayunos_chatgpt.db"
        result = subprocess.run(
            [
                sys.executable,
                "tools/summarize_memory.py",
                "--db",
                str(real_path),
                "--memory-space",
                "whale",
                "--conversation-id",
                "whale-conv",
                "--write",
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without --allow-real-write and --confirm-real-write", result.stdout)

    def test_cli_real_database_write_with_gates_succeeds_on_temp_mock(self):
        from tools.migrate_memory_summaries import migrate

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            db_path = root / "ayunos_chatgpt.db"
            backup_dir = root / "backups"
            create_summary_fixture(db_path)
            migrate(db_path, allow_real_write=True, confirm_real_write=True, backup_dir=backup_dir)
            env = dict(**{k: v for k, v in __import__("os").environ.items()})
            env["AYUNOS_V3C_BACKUP_DIR"] = str(backup_dir)
            result = subprocess.run(
                [
                    sys.executable,
                    "tools/summarize_memory.py",
                    "--db",
                    str(db_path),
                    "--memory-space",
                    "whale",
                    "--conversation-id",
                    "whale-conv",
                    "--write",
                    "--allow-real-write",
                    "--confirm-real-write",
                ],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            count = table_count(db_path, "memory_summaries")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(count, 1)
        self.assertIn('"written": true', result.stdout)

    def test_summarizer_tools_do_not_reference_api_credentials(self):
        root = Path(__file__).resolve().parents[1]
        combined = "\n".join(
            (root / rel_path).read_text(encoding="utf-8")
            for rel_path in (
                "memory/summarizer.py",
                "tools/summarize_memory.py",
                "tools/migrate_memory_summaries.py",
            )
        )

        self.assertNotIn("DEEPSEEK_API_KEY", combined)
        self.assertNotIn("requests.", combined)
        self.assertNotIn("api_key", combined.lower())


if __name__ == "__main__":
    unittest.main()
