import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.deepseek_importer import import_deepseek_export
from tools.search_memory import search
from memory.read_only_retriever import retrieve
from whale_chat import build_relevant_memory_section


class DeepSeekImporterTest(unittest.TestCase):
    def test_imports_messages_with_source_platform_memory_space_and_is_idempotent(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            export_path = root / "raw_deepseek_current_chat.json"
            db_path = root / "memory.db"
            report_path = root / "report.md"
            payload = {
                "source_platform": "deepseek",
                "title": "DeepSeek sample",
                "conversation_id": "deepseek-conv-1",
                "exported_at": "2026-07-05T01:24:24+08:00",
                "total_messages": 2,
                "user_messages": 1,
                "assistant_messages": 1,
                "messages": [
                    {
                        "index": 0,
                        "source_message_id": "source-user-1",
                        "role": "user",
                        "content": "Find the silver needle",
                        "created_at": "2026-03-12T13:21:51.994Z",
                    },
                    {
                        "index": 1,
                        "source_message_id": "source-assistant-1",
                        "role": "assistant",
                        "content": "The silver needle is in this answer.",
                        "created_at": "2026-03-12T13:21:52.994Z",
                    },
                ],
            }
            export_path.write_text(json.dumps(payload), encoding="utf-8")

            first = import_deepseek_export(export_path, db_path, report_path)
            second = import_deepseek_export(export_path, db_path, report_path)

            self.assertEqual(first.total_messages, 2)
            self.assertEqual(first.user_messages, 1)
            self.assertEqual(first.assistant_messages, 1)
            self.assertEqual(first.inserted_messages, 2)
            self.assertEqual(first.skipped_messages, 0)
            self.assertEqual(second.inserted_messages, 0)
            self.assertEqual(second.skipped_messages, 2)

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                messages = conn.execute(
                    """
                    SELECT role, text, ordinal, create_time_iso, source_platform, metadata_json
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY ordinal
                    """,
                    ("deepseek-conv-1",),
                ).fetchall()
                self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
                self.assertEqual(messages[0]["text"], "Find the silver needle")
                self.assertEqual(messages[1]["ordinal"], 1)
                self.assertEqual(messages[1]["create_time_iso"], "2026-03-12T13:21:52.994Z")
                self.assertEqual({row["source_platform"] for row in messages}, {"deepseek"})
                self.assertEqual(
                    {json.loads(row["metadata_json"])["memory_space"] for row in messages},
                    {"whale"},
                )

                conversation = conn.execute(
                    """
                    SELECT source_platform, metadata_json
                    FROM conversations
                    WHERE conversation_id = ?
                    """,
                    ("deepseek-conv-1",),
                ).fetchone()
                self.assertEqual(conversation["source_platform"], "deepseek")
                self.assertEqual(json.loads(conversation["metadata_json"])["memory_space"], "whale")

                hit = conn.execute(
                    """
                    SELECT m.source_platform
                    FROM messages m
                    WHERE m.text LIKE ? AND m.role = ? AND m.source_platform = ?
                    """,
                    ("%silver needle%", "assistant", "deepseek"),
                ).fetchone()
                self.assertIsNotNone(hit)

            search_rows = search(
                db_path,
                "silver needle",
                5,
                role="assistant",
                source_platform="deepseek",
            )
            self.assertEqual(len(search_rows), 1)
            self.assertEqual(search_rows[0]["role"], "assistant")
            self.assertEqual(search_rows[0]["source_platform"], "deepseek")

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("消息总数：2", report)
            self.assertIn("source_platform：deepseek", report)


    def test_read_only_retriever_defaults_to_whale_space_and_does_not_cross_spaces(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE conversations (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        update_time REAL,
                        update_time_iso TEXT,
                        source_platform TEXT,
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
                        metadata_json TEXT
                    );
                    """
                )
                conn.execute(
                    """
                    INSERT INTO conversations
                    (conversation_id, title, update_time, update_time_iso, source_platform, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "77026d03-3dbf-4036-ad04-0984cdb856f6",
                        "最好的朋友鲸鱼",
                        10,
                        "2026-07-05T01:24:24+08:00",
                        "deepseek",
                        json.dumps({"memory_space": "whale"}, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO messages
                    (message_id, conversation_id, conversation_title, ordinal, role, create_time,
                     create_time_iso, text, source_platform, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "whale-msg-1",
                        "77026d03-3dbf-4036-ad04-0984cdb856f6",
                        "最好的朋友鲸鱼",
                        0,
                        "user",
                        10,
                        "2026-07-05T01:24:24+08:00",
                        "莫泊桑 火星人",
                        "deepseek",
                        json.dumps({"memory_space": "whale"}, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO conversations
                    (conversation_id, title, update_time, update_time_iso, source_platform, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "other-conv",
                        "普通 ChatGPT 历史",
                        20,
                        "2026-07-05T02:00:00+08:00",
                        "chatgpt",
                        json.dumps({"memory_space": "general"}, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO messages
                    (message_id, conversation_id, conversation_title, ordinal, role, create_time,
                     create_time_iso, text, source_platform, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "other-msg-1",
                        "other-conv",
                        "普通 ChatGPT 历史",
                        0,
                        "user",
                        20,
                        "2026-07-05T02:00:00+08:00",
                        "莫泊桑 火星人",
                        "chatgpt",
                        json.dumps({"memory_space": "general"}, ensure_ascii=False),
                    ),
                )
                conn.commit()

            rows = retrieve("莫泊桑 火星人 鲸鱼", db_path=db_path)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["conversation_id"], "77026d03-3dbf-4036-ad04-0984cdb856f6")
            self.assertEqual(rows[0]["source_platform"], "deepseek")
            self.assertEqual(rows[0]["memory_space"], "whale")

            other_rows = retrieve("普通 ChatGPT 历史", db_path=db_path)
            self.assertEqual(other_rows, [])

            section = build_relevant_memory_section(rows)
            self.assertIn("[Relevant Memory - whale]", section)
            self.assertIn("以下内容来自 YunOS 中用户与鲸鱼的历史对话检索结果，优先级高于通用资料。", section)

            empty_section = build_relevant_memory_section([])
            self.assertEqual(empty_section, "")


if __name__ == "__main__":
    unittest.main()
