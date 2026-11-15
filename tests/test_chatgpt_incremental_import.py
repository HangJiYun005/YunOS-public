from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS = PROJECT_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from chatgpt_incremental_import import execute, prepare_plan, ro_connect  # noqa: E402


SCHEMA = """
CREATE TABLE conversations (
 conversation_id TEXT PRIMARY KEY, title TEXT, create_time REAL, create_time_iso TEXT,
 update_time REAL, update_time_iso TEXT, source_file TEXT, default_model_slug TEXT,
 is_archived TEXT, is_starred TEXT, message_count INTEGER, user_message_count INTEGER,
 assistant_message_count INTEGER, char_count INTEGER, tags TEXT, importance_score INTEGER,
 source_platform TEXT, metadata_json TEXT, memory_space TEXT DEFAULT 'general'
);
CREATE TABLE messages (
 message_id TEXT, node_id TEXT, conversation_id TEXT, conversation_title TEXT,
 ordinal INTEGER, parent_id TEXT, children_json TEXT, role TEXT, author_name TEXT,
 create_time REAL, create_time_iso TEXT, update_time REAL, update_time_iso TEXT,
 content_type TEXT, text TEXT, status TEXT, weight REAL, metadata_json TEXT,
 source_platform TEXT, memory_space TEXT DEFAULT 'general'
);
CREATE TABLE conversation_marks (conversation_id TEXT PRIMARY KEY, status TEXT, note TEXT, updated_at TEXT);
CREATE TABLE memory_summaries (id INTEGER PRIMARY KEY, summary_text TEXT);
CREATE VIRTUAL TABLE messages_fts USING fts5(conversation_title, role, text, content='messages', content_rowid='rowid');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
 INSERT INTO messages_fts(rowid, conversation_title, role, text)
 VALUES (new.rowid, new.conversation_title, new.role, new.text);
END;
"""


def conversation(conv_id: str, created: float, message_id: str, message_created: float | None = None):
    return {
        "id": conv_id,
        "title": conv_id,
        "create_time": created,
        "update_time": created + 1,
        "mapping": {
            message_id: {
                "parent": None,
                "children": [],
                "message": {
                    "id": message_id,
                    "author": {"role": "user"},
                    "create_time": created if message_created is None else message_created,
                    "update_time": None,
                    "content": {"content_type": "text", "parts": [conv_id]},
                    "metadata": {},
                },
            }
        },
    }


class IncrementalImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "real.db"
        self.zip = self.root / "export.zip"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO conversations (conversation_id,title,create_time,update_time,message_count) VALUES ('old','old',100,110,1)"
            )
            conn.execute(
                "INSERT INTO messages (message_id,node_id,conversation_id,conversation_title,ordinal,text) VALUES ('m-old','m-old','old','old',0,'old')"
            )
            conn.execute("INSERT INTO conversation_marks VALUES ('old','keep','note','now')")
            conn.execute("INSERT INTO memory_summaries VALUES (1,'keep summary')")
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def write_zip(self, rows):
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.writestr("conversations-000.json", json.dumps(rows))

    def test_dry_run_filters_existing_and_old_date_new_ids(self):
        self.write_zip([
            conversation("old", 100, "m-old"),
            conversation("old-date-new-id", 100, "m-old-date"),
            conversation("new", 101.000001, "m-new"),
        ])
        plan = prepare_plan(self.zip, self.db)
        self.assertEqual(plan["zip"]["conversations"], 3)
        self.assertEqual(plan["overlap_conversations"], 1)
        self.assertEqual(plan["overlap_messages"], 1)
        self.assertEqual(plan["old_date_new_id_conversations"], 1)
        self.assertEqual(plan["candidate_conversations_count"], 1)
        self.assertEqual(plan["candidate_messages_count"], 1)
        self.assertTrue(plan["safe_to_import"])

    def test_execute_uses_backup_transaction_and_preserves_extensions(self):
        self.write_zip([conversation("new", 101, "m-new")])
        plan = prepare_plan(self.zip, self.db)
        result = execute(plan, self.db, self.root / "backups")
        self.assertTrue(result["transaction_committed"])
        self.assertTrue(Path(result["backup_path"]).is_file())
        self.assertTrue(result["verification"]["all_checks_passed"])
        self.assertTrue(result["verification"]["checks"]["fts_message_count_matches"])
        with closing(ro_connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT note FROM conversation_marks").fetchone()[0], "note")
            self.assertEqual(conn.execute("SELECT summary_text FROM memory_summaries").fetchone()[0], "keep summary")

    def test_message_date_overlap_stops_execute(self):
        self.write_zip([conversation("new", 101, "m-new", message_created=99)])
        plan = prepare_plan(self.zip, self.db)
        self.assertTrue(plan["date_overlap"])
        self.assertFalse(plan["safe_to_import"])
        with self.assertRaisesRegex(RuntimeError, "date overlap"):
            execute(plan, self.db, self.root / "backups")
        self.assertFalse((self.root / "backups").exists())

    def test_message_id_is_deduplicated_globally_across_new_conversations(self):
        self.write_zip([
            conversation("new-a", 101, "shared-message"),
            conversation("new-b", 102, "shared-message"),
        ])
        plan = prepare_plan(self.zip, self.db)
        self.assertEqual(plan["zip_internal_duplicate_message_occurrences"], 1)
        self.assertEqual(plan["candidate_conversations_count"], 2)
        self.assertEqual(plan["candidate_messages_count"], 1)
        self.assertEqual(plan["skipped_candidate_duplicate_messages"], 1)
        result = execute(plan, self.db, self.root / "backups")
        self.assertTrue(result["verification"]["checks"]["message_ids_unique"])


if __name__ == "__main__":
    unittest.main()
