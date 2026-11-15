import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import whale_chat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PERSONA_PATH = PROJECT_ROOT / "config" / "persona_whale.md"


def create_summary_db(db_path: Path, include_summary_table: bool = True, summary_count: int = 2) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                update_time INTEGER DEFAULT 0,
                update_time_iso TEXT,
                memory_space TEXT DEFAULT 'general'
            );
            CREATE TABLE messages (
                message_id TEXT,
                conversation_id TEXT,
                conversation_title TEXT,
                create_time INTEGER DEFAULT 0,
                create_time_iso TEXT,
                role TEXT,
                text TEXT,
                memory_space TEXT DEFAULT 'general'
            );
            """
        )
        conn.execute(
            """
            INSERT INTO messages (
                message_id, conversation_id, conversation_title, create_time, create_time_iso,
                role, text, memory_space
            )
            VALUES ('raw-message', 'whale-conv', 'Whale raw', 1, '2026-07-05T00:00:00',
                    'user', 'ORIGINAL RAW MESSAGE FULL TEXT SHOULD NOT BE IN SUMMARY CONTEXT', 'whale')
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
                """
            )
            rows = [
                (
                    "whale",
                    None,
                    "memory_space_overview",
                    "Whale overview summary for high level continuity.",
                    688,
                    1,
                    "extractive_v1",
                    "2026-07-05T20:10:11+08:00",
                ),
                (
                    "whale",
                    "77026d03-3dbf-4036-ad04-0984cdb856f6",
                    "conversation_summary",
                    "Whale conversation summary about Mars and chair themes.",
                    200,
                    1,
                    "extractive_v1",
                    "2026-07-05T20:10:04+08:00",
                ),
                (
                    "general",
                    "general-conv",
                    "memory_space_overview",
                    "General summary must not appear in whale chat.",
                    20,
                    1,
                    "extractive_v1",
                    "2026-07-05T20:10:30+08:00",
                ),
            ]
            for index in range(summary_count):
                row = rows[index % len(rows)]
                conn.execute(
                    """
                    INSERT INTO memory_summaries (
                        memory_space, conversation_id, summary_type, summary_text,
                        source_message_count, source_conversation_count, method, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row[0],
                        row[1] if index < len(rows) else f"whale-extra-{index}",
                        row[2],
                        row[3] if index < len(rows) else f"Extra whale summary {index}.",
                        row[4],
                        row[5],
                        row[6],
                        row[7],
                    ),
                )
        conn.commit()


class WhaleChatSummaryContextTest(unittest.TestCase):
    def test_prompt_includes_high_level_whale_summary_before_raw_memory(self):
        whale_rows = [
            {
                "title": "raw whale",
                "role": "assistant",
                "snippet": "raw whale memory snippet",
                "conversation_id": "whale-conv",
                "message_id": "whale-msg",
                "source_platform": "deepseek",
                "memory_space": "whale",
            }
        ]
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_db(db_path)
            with patch.object(whale_chat, "load_persona", return_value="persona text"), patch.object(
                whale_chat, "retrieve", return_value=whale_rows
            ):
                prompt = whale_chat.build_prompt("鲸鱼你记得吗", db_path=db_path, top_k=2)

        self.assertIn("[High-level Memory Summaries - whale]", prompt)
        self.assertIn("Whale overview summary", prompt)
        self.assertIn("[Relevant Memory - whale]", prompt)
        self.assertLess(
            prompt.index("[High-level Memory Summaries - whale]"),
            prompt.index("[Relevant Memory - whale]"),
        )

    def test_missing_memory_summaries_table_does_not_crash(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_db(db_path, include_summary_table=False)
            with patch.object(whale_chat, "load_persona", return_value="persona text"), patch.object(
                whale_chat, "retrieve", return_value=[]
            ):
                prompt = whale_chat.build_prompt("鲸鱼", db_path=db_path, top_k=2)

        self.assertIn("[System]", prompt)
        self.assertNotIn("[High-level Memory Summaries - whale]", prompt)

    def test_empty_memory_summaries_does_not_crash(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_db(db_path, summary_count=0)

            context = whale_chat.load_whale_summary_context(db_path)

        self.assertEqual(context, "")

    def test_general_summary_is_not_injected(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_db(db_path, summary_count=3)

            context = whale_chat.load_whale_summary_context(db_path, limit=5)

        self.assertIn("Whale overview summary", context)
        self.assertNotIn("General summary must not appear", context)

    def test_summary_context_does_not_write_database(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_db(db_path)
            before_size = db_path.stat().st_size
            before_count = summary_count(db_path)

            context = whale_chat.load_whale_summary_context(db_path)

            after_size = db_path.stat().st_size
            after_count = summary_count(db_path)

        self.assertIn("Whale overview summary", context)
        self.assertEqual(before_size, after_size)
        self.assertEqual(before_count, after_count)

    def test_build_prompt_does_not_modify_persona_file(self):
        before = PERSONA_PATH.read_bytes()
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_db(db_path)
            with patch.object(whale_chat, "load_persona", return_value="persona text"), patch.object(
                whale_chat, "retrieve", return_value=[]
            ):
                whale_chat.build_prompt("鲸鱼", db_path=db_path, top_k=2)

        self.assertEqual(PERSONA_PATH.read_bytes(), before)

    def test_prompt_summary_context_does_not_include_raw_message_text(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_db(db_path)
            with patch.object(whale_chat, "load_persona", return_value="persona text"), patch.object(
                whale_chat, "retrieve", return_value=[]
            ):
                prompt = whale_chat.build_prompt("鲸鱼", db_path=db_path, top_k=2)

        self.assertNotIn("ORIGINAL RAW MESSAGE FULL TEXT SHOULD NOT BE IN SUMMARY CONTEXT", prompt)

    def test_summary_count_is_limited(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_summary_db(db_path, summary_count=8)

            context = whale_chat.load_whale_summary_context(db_path, limit=3)

        self.assertEqual(context.count("summary_type:"), 3)


def summary_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM memory_summaries").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
