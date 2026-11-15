import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import whale_chat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = PROJECT_ROOT / "tools" / "preview_whale_prompt.py"


def create_preview_db(db_path: Path, include_summary_table: bool = True) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                update_time INTEGER DEFAULT 0,
                update_time_iso TEXT,
                memory_space TEXT DEFAULT 'general',
                source_platform TEXT DEFAULT 'chatgpt'
            );
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                conversation_title TEXT,
                create_time INTEGER DEFAULT 0,
                create_time_iso TEXT,
                role TEXT,
                text TEXT,
                memory_space TEXT DEFAULT 'general',
                source_platform TEXT DEFAULT 'chatgpt'
            );
            INSERT INTO conversations (
                conversation_id, title, update_time, update_time_iso, memory_space, source_platform
            )
            VALUES
                ('whale-conv', 'whale topic alpha', 20, '2026-07-05T00:00:20', 'whale', 'deepseek'),
                ('general-conv', 'general topic', 10, '2026-07-05T00:00:10', 'general', 'chatgpt');
            INSERT INTO messages (
                message_id, conversation_id, conversation_title, create_time, create_time_iso,
                role, text, memory_space, source_platform
            )
            VALUES
                ('whale-msg-1', 'whale-conv', 'whale topic alpha', 31, '2026-07-05T00:00:31',
                 'user', 'alpha whale memory one', 'whale', 'deepseek'),
                ('whale-msg-2', 'whale-conv', 'whale topic alpha', 30, '2026-07-05T00:00:30',
                 'assistant', 'alpha whale memory two', 'whale', 'deepseek'),
                ('general-msg-1', 'general-conv', 'general topic alpha', 11, '2026-07-05T00:00:11',
                 'user', 'alpha general memory one', 'general', 'chatgpt');
            """
        )
        if include_summary_table:
            conn.executescript(
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
                );
                INSERT INTO memory_summaries (
                    memory_space, conversation_id, summary_type, summary_text,
                    source_message_count, source_conversation_count, method, created_at
                )
                VALUES
                    ('whale', NULL, 'memory_space_overview', 'overview alpha one', 10, 1,
                     'extractive_v1', '2026-07-05T00:00:05'),
                    ('whale', 'whale-conv', 'conversation_summary', 'conversation alpha two', 2, 1,
                     'extractive_v1', '2026-07-05T00:00:04');
                """
            )
        conn.commit()


def section_by_name(report: dict, name: str) -> dict:
    for section in report["sections"]:
        if section["name"] == name:
            return section
    raise AssertionError(f"missing section: {name}")


class WhalePromptPreviewTest(unittest.TestCase):
    def test_build_whale_prompt_context_returns_sections_with_system(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            report = whale_chat.build_whale_prompt_context("alpha", db_path=db_path)

        self.assertIn("sections", report)
        self.assertEqual(section_by_name(report, "System")["enabled"], True)

    def test_summary_section_is_before_raw_memory_section(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            report = whale_chat.build_whale_prompt_context("alpha", db_path=db_path)

        names = [section["name"] for section in report["sections"]]
        self.assertIn("High-level Memory Summaries - whale", names)
        self.assertLess(
            names.index("High-level Memory Summaries - whale"),
            names.index("Relevant Memory - whale"),
        )
        self.assertEqual(report["ordering"]["summary_before_raw_memory"], True)

    def test_optional_general_is_disabled_by_default_and_enabled_only_when_requested(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            default_report = whale_chat.build_whale_prompt_context("alpha", db_path=db_path)
            general_report = whale_chat.build_whale_prompt_context(
                "alpha",
                db_path=db_path,
                include_general=True,
            )

        self.assertEqual(section_by_name(default_report, "Optional Context - general")["enabled"], False)
        self.assertEqual(default_report["totals"]["general_context_count"], 0)
        self.assertEqual(section_by_name(general_report, "Optional Context - general")["enabled"], True)
        self.assertEqual(general_report["totals"]["general_context_count"], 1)

    def test_estimated_tokens_are_positive_for_enabled_sections(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            report = whale_chat.build_whale_prompt_context("alpha", db_path=db_path)

        enabled_sections = [section for section in report["sections"] if section["enabled"]]
        self.assertGreater(report["totals"]["estimated_total_tokens"], 0)
        self.assertTrue(all(section["estimated_tokens"] > 0 for section in enabled_sections))

    def test_summary_limit_and_raw_memory_top_k_are_applied(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            report = whale_chat.build_whale_prompt_context(
                "alpha",
                db_path=db_path,
                summary_limit=1,
                raw_memory_top_k=1,
            )

        self.assertEqual(report["totals"]["summary_count"], 1)
        self.assertEqual(report["totals"]["raw_memory_count"], 1)
        self.assertEqual(section_by_name(report, "High-level Memory Summaries - whale")["item_count"], 1)
        self.assertEqual(section_by_name(report, "Relevant Memory - whale")["item_count"], 1)

    def test_raw_memory_retrieval_falls_back_to_significant_terms(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            report = whale_chat.build_whale_prompt_context(
                "did we discuss alpha before",
                db_path=db_path,
                raw_memory_top_k=2,
            )

        self.assertEqual(report["totals"]["raw_memory_count"], 2)
        self.assertEqual(section_by_name(report, "Relevant Memory - whale")["enabled"], True)

    def test_missing_memory_summaries_table_does_not_crash(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path, include_summary_table=False)

            report = whale_chat.build_whale_prompt_context("alpha", db_path=db_path)

        self.assertEqual(report["totals"]["summary_count"], 0)
        self.assertEqual(section_by_name(report, "High-level Memory Summaries - whale")["enabled"], False)

    def test_preview_builder_does_not_write_database(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)
            before = database_counts(db_path)

            whale_chat.build_whale_prompt_context("alpha", db_path=db_path, include_general=True)

            after = database_counts(db_path)

        self.assertEqual(after, before)

    def test_cli_json_outputs_json(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            result = run_cli("alpha", "--db", str(db_path), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["totals"]["summary_count"], 2)
        self.assertIn("sections", data)
        self.assertNotIn("prompt", data)

    def test_cli_hides_full_prompt_by_default_and_show_full_prompt_reveals_it(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)

            default_result = run_cli("alpha", "--db", str(db_path))
            full_result = run_cli("alpha", "--db", str(db_path), "--show-full-prompt")

        self.assertEqual(default_result.returncode, 0, default_result.stderr)
        self.assertEqual(full_result.returncode, 0, full_result.stderr)
        self.assertNotIn("Full Prompt:", default_result.stdout)
        self.assertIn("Full Prompt:", full_result.stdout)
        self.assertIn("[User Input]\nalpha", full_result.stdout)

    def test_cli_real_database_requires_real_readonly_flag(self):
        real_db_path = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"

        result = run_cli("alpha", "--db", str(real_db_path))

        self.assertEqual(result.returncode, 2)
        self.assertIn("--real-readonly", result.stderr)
        self.assertNotIn(str(real_db_path), result.stderr)

    def test_preview_does_not_call_api(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_preview_db(db_path)
            with patch.object(whale_chat, "call_deepseek", side_effect=AssertionError("API called")):
                report = whale_chat.build_whale_prompt_context("alpha", db_path=db_path)

        self.assertGreater(report["totals"]["raw_memory_count"], 0)


def database_counts(db_path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(db_path) as conn:
        conversation_count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        summary_count = (
            conn.execute("SELECT COUNT(*) FROM memory_summaries").fetchone()[0]
            if table_exists(conn, "memory_summaries")
            else 0
        )
    return int(conversation_count), int(message_count), int(summary_count)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
    )


if __name__ == "__main__":
    unittest.main()
