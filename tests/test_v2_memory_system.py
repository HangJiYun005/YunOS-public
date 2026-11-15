import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.read_only_retriever import retrieve
from tools.deepseek_importer import ensure_schema, import_deepseek_export
from tools import search_memory
import whale_chat


def create_v2_memory_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                update_time REAL,
                update_time_iso TEXT,
                source_platform TEXT,
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
                source_platform TEXT,
                memory_space TEXT DEFAULT 'general',
                metadata_json TEXT
            );
            """
        )
        rows = [
            ("whale-conv", "whale sea-glass memory", "deepseek", "whale", 30),
            ("general-conv", "general sea-glass note", "chatgpt", "general", 20),
        ]
        for conversation_id, title, platform, space, sort_time in rows:
            conn.execute(
                """
                INSERT INTO conversations
                (conversation_id, title, update_time, update_time_iso, source_platform, memory_space, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    title,
                    sort_time,
                    "2026-07-05T00:00:00+08:00",
                    platform,
                    space,
                    json.dumps({"memory_space": space}, ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                INSERT INTO messages
                (message_id, conversation_id, conversation_title, ordinal, role, create_time,
                 create_time_iso, text, source_platform, memory_space, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{conversation_id}-msg",
                    conversation_id,
                    title,
                    0,
                    "assistant",
                    sort_time,
                    "2026-07-05T00:00:00+08:00",
                    f"{title} contains sea-glass",
                    platform,
                    space,
                    json.dumps({"memory_space": space}, ensure_ascii=False),
                ),
            )
        conn.commit()


class V2MemorySystemTest(unittest.TestCase):
    def test_deepseek_import_adds_real_memory_space_column_as_whale(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            export_path = root / "deepseek.json"
            db_path = root / "memory.db"
            report_path = root / "report.md"
            export_path.write_text(
                json.dumps(
                    {
                        "source_platform": "deepseek",
                        "title": "DeepSeek whale import",
                        "conversation_id": "deepseek-whale",
                        "exported_at": "2026-07-05T01:24:24+08:00",
                        "messages": [
                            {
                                "index": 0,
                                "role": "user",
                                "content": "persistent whale phrase",
                                "created_at": "2026-07-05T01:24:24+08:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            import_deepseek_export(export_path, db_path, report_path)

            with sqlite3.connect(db_path) as conn:
                message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
                self.assertIn("memory_space", message_columns)
                self.assertEqual(
                    conn.execute("SELECT DISTINCT memory_space FROM messages").fetchall(),
                    [("whale",)],
                )

    def test_retrieve_keeps_whale_default_and_general_space_separate(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_v2_memory_db(db_path)

            default_rows = retrieve("sea-glass", db_path=db_path)
            general_rows = retrieve("sea-glass", memory_space="general", db_path=db_path)
            mixed_rows = retrieve("sea-glass", memory_space="whale", db_path=db_path) + retrieve(
                "sea-glass",
                memory_space="general",
                db_path=db_path,
            )

            self.assertEqual({row["memory_space"] for row in default_rows}, {"whale"})
            self.assertEqual({row["conversation_id"] for row in default_rows}, {"whale-conv"})
            self.assertEqual({row["memory_space"] for row in general_rows}, {"general"})
            self.assertEqual({row["conversation_id"] for row in general_rows}, {"general-conv"})
            self.assertEqual(
                {(row["conversation_id"], row["memory_space"]) for row in mixed_rows},
                {("whale-conv", "whale"), ("general-conv", "general")},
            )

    def test_search_memory_is_deprecated_wrapper_around_retriever(self):
        with patch("tools.search_memory.retrieve") as fake_retrieve:
            fake_retrieve.return_value = [
                {
                    "hit_source": "message",
                    "title": "wrapped",
                    "update_time_iso": "2026-07-05T00:00:00+08:00",
                    "role": "assistant",
                    "snippet": "wrapped result",
                    "conversation_id": "wrapped-conv",
                    "message_id": "wrapped-msg",
                    "source_platform": "deepseek",
                    "memory_space": "whale",
                }
            ]

            rows = search_memory.search(Path("ignored.db"), "wrapped", 3, memory_space="whale")

        fake_retrieve.assert_called_once_with("wrapped", top_k=3, memory_space="whale", db_path=Path("ignored.db"))
        self.assertEqual(rows[0]["source"], "message")
        self.assertEqual(rows[0]["memory_space"], "whale")

    def test_ensure_schema_adds_memory_space_default_general_to_existing_messages(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE messages (
                        message_id TEXT,
                        conversation_id TEXT,
                        ordinal INTEGER,
                        role TEXT,
                        source_platform TEXT,
                        text TEXT
                    );
                    INSERT INTO messages
                    (message_id, conversation_id, ordinal, role, source_platform, text)
                    VALUES ('legacy-1', 'legacy-conv', 0, 'assistant', 'chatgpt', 'legacy note');
                    """
                )
                ensure_schema(conn)
                message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
                self.assertIn("memory_space", message_columns)
                self.assertEqual(conn.execute("SELECT memory_space FROM messages").fetchone()[0], "general")


class V2SplashScreenTest(unittest.TestCase):
    def test_index_contains_whale_memory_splash_screen(self):
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="splash"', html)
        self.assertIn("YunOS", html)
        self.assertIn('data-i18n="splash.loading"', html)
        self.assertIn("正在加载 Whale Memory System…", html)
        self.assertIn('data-i18n="splash.initializing"', html)
        self.assertIn("正在初始化 memory_space=whale", html)


class V2WhaleChatPromptTest(unittest.TestCase):
    def test_prompt_sections_keep_whale_primary_and_general_optional(self):
        whale_rows = [
            {
                "hit_source": "message",
                "title": "whale continuity",
                "role": "assistant",
                "snippet": "whale remembers this",
                "conversation_id": "whale-conv",
                "message_id": "whale-msg",
                "source_platform": "deepseek",
                "memory_space": "whale",
            }
        ]
        general_rows = [
            {
                "hit_source": "message",
                "title": "general knowledge",
                "role": "assistant",
                "snippet": "general context only",
                "conversation_id": "general-conv",
                "message_id": "general-msg",
                "source_platform": "chatgpt",
                "memory_space": "general",
            }
        ]

        with patch.object(whale_chat, "load_persona", return_value="persona text"), patch.object(
            whale_chat,
            "retrieve",
            side_effect=[whale_rows, general_rows],
        ) as fake_retrieve:
            prompt = whale_chat.build_prompt("请分析 memory_space 架构", db_path=Path("memory.db"), top_k=2)

        self.assertIn("[System]", prompt)
        self.assertIn("persona text", prompt)
        self.assertIn("[Relevant Memory - whale]", prompt)
        self.assertIn("whale remembers this", prompt)
        self.assertIn("[Optional Context - general]", prompt)
        self.assertIn("general context only", prompt)
        self.assertIn("[User Input]", prompt)
        fake_retrieve.assert_any_call("请分析 memory_space 架构", top_k=2, memory_space="whale", db_path=Path("memory.db"))
        fake_retrieve.assert_any_call("请分析 memory_space 架构", top_k=2, memory_space="general", db_path=Path("memory.db"))

    def test_prompt_does_not_pull_general_for_plain_whale_chat(self):
        with patch.object(whale_chat, "load_persona", return_value="persona text"), patch.object(
            whale_chat,
            "retrieve",
            return_value=[],
        ) as fake_retrieve:
            prompt = whale_chat.build_prompt("鲸鱼，你还记得我吗", db_path=Path("memory.db"), top_k=2)

        self.assertNotIn("[Optional Context - general]", prompt)
        fake_retrieve.assert_called_once_with("鲸鱼，你还记得我吗", top_k=2, memory_space="whale", db_path=Path("memory.db"))


if __name__ == "__main__":
    unittest.main()
