import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def write_connection(path: Path):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def make_db(path: Path, summaries: bool = True, coverage_columns: bool = False) -> None:
    with write_connection(path) as conn:
        schema = """
        CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, memory_space TEXT);
        CREATE TABLE messages (message_id TEXT, conversation_id TEXT, role TEXT, text TEXT, create_time_iso TEXT, memory_space TEXT);
        """
        if summaries:
            schema += """
        CREATE TABLE memory_summaries (
            id INTEGER PRIMARY KEY, memory_space TEXT, conversation_id TEXT, summary_type TEXT,
            summary_text TEXT, source_message_count INTEGER, created_at TEXT
            {coverage}
        );
        """.format(coverage=", source_content_hash TEXT, covered_at TEXT, last_message_id TEXT" if coverage_columns else "")
        conn.executescript(schema)


def add_conversation(conn, conversation_id="c1", space="general"):
    conn.execute("INSERT INTO conversations VALUES (?, ?)", (conversation_id, space))


def add_message(conn, message_id, conversation_id="c1", role="user", text="body", timestamp="2026-01-01T00:00:00Z", space="general"):
    conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", (message_id, conversation_id, role, text, timestamp, space))


class MemoryChangeDetectionTest(unittest.TestCase):
    def detect(self, path, **filters):
        from memory.change_detector import detect_changes
        return detect_changes(path, **filters)

    def snapshot(self, path):
        from memory.source_snapshot import snapshots_for_database
        return snapshots_for_database(path)[0]

    def test_missing_summary_and_new_conversation(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"; make_db(path)
            with write_connection(path) as conn:
                add_conversation(conn); add_message(conn, "m1")
                add_conversation(conn, "empty")
            items = self.detect(path)["items"]
        self.assertEqual({item["conversation_id"]: item["status"] for item in items}, {"c1": "summary_missing", "empty": "new_conversation"})

    def test_new_messages_source_changes_and_up_to_date(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"; make_db(path, coverage_columns=True)
            with write_connection(path) as conn:
                add_conversation(conn); add_message(conn, "m1")
            snapshot = self.snapshot(path)
            with write_connection(path) as conn:
                conn.execute("INSERT INTO memory_summaries VALUES (1, 'general', 'c1', 'conversation_summary', 'hidden', ?, '2026', ?, '2026', 'm1')", (1, snapshot["source_content_hash"]))
            self.assertEqual(self.detect(path)["items"][0]["status"], "up_to_date")
            with write_connection(path) as conn: add_message(conn, "m2")
            self.assertEqual(self.detect(path)["items"][0]["status"], "new_messages")
            with write_connection(path) as conn: conn.execute("DELETE FROM messages WHERE message_id='m2'"); conn.execute("UPDATE messages SET text='changed' WHERE message_id='m1'")
            self.assertEqual(self.detect(path)["items"][0]["status"], "source_changed")
            with write_connection(path) as conn: conn.execute("UPDATE messages SET role='assistant' WHERE message_id='m1'")
            self.assertEqual(self.detect(path)["items"][0]["status"], "source_changed")

    def test_order_stable_and_delete_changes_hash(self):
        with TemporaryDirectory() as temp:
            first, second = Path(temp) / "one.db", Path(temp) / "two.db"
            make_db(first, summaries=False); make_db(second, summaries=False)
            for path, rows in ((first, [("m2", "b"), ("m1", "a")]), (second, [("m1", "a"), ("m2", "b")])):
                with write_connection(path) as conn:
                    add_conversation(conn)
                    for message_id, text in rows: add_message(conn, message_id, text=text)
            self.assertEqual(self.snapshot(first)["source_content_hash"], self.snapshot(second)["source_content_hash"])
            with write_connection(first) as conn: conn.execute("DELETE FROM messages WHERE message_id='m2'")
            self.assertNotEqual(self.snapshot(first)["source_content_hash"], self.snapshot(second)["source_content_hash"])

    def test_coverage_unknown_and_orphaned_summary(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"; make_db(path)
            with write_connection(path) as conn:
                add_conversation(conn); add_message(conn, "m1")
                conn.execute("INSERT INTO memory_summaries VALUES (1, 'general', 'c1', 'conversation_summary', 'secret', 1, '2026')")
                conn.execute("INSERT INTO memory_summaries VALUES (2, 'whale', 'gone', 'conversation_summary', 'secret', 1, '2026')")
            statuses = {item["conversation_id"]: item["status"] for item in self.detect(path)["items"]}
        self.assertEqual(statuses, {"c1": "coverage_unknown", "gone": "orphaned_summary"})

    def test_general_whale_filters_cli_redacts_content_and_paths(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"; make_db(path)
            with write_connection(path) as conn:
                add_conversation(conn, "general", "general"); add_message(conn, "m1", "general", text="SECRET BODY")
                add_conversation(conn, "whale", "whale"); add_message(conn, "m2", "whale", text="ANOTHER SECRET", space="whale")
            self.assertEqual(self.detect(path, memory_space="whale")["items"][0]["conversation_id"], "whale")
            result = subprocess.run([sys.executable, "tools/check_memory_changes.py", "--db", str(path), "--memory-space", "general", "--json"], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["items"][0]["conversation_id"], "general")
        self.assertNotIn("SECRET BODY", result.stdout); self.assertNotIn(str(path.parent), result.stdout)

    def test_connections_are_closed(self):
        from memory import source_snapshot
        with TemporaryDirectory() as temp:
            path = Path(temp) / "test.db"; make_db(path)
            with write_connection(path) as conn: add_conversation(conn)
            original = source_snapshot.connect_readonly
            seen = []
            class TrackingConnection:
                def __init__(self, conn): self.conn = conn
                def __getattr__(self, name): return getattr(self.conn, name)
                def close(self): seen.append(True); self.conn.close()
            source_snapshot.connect_readonly = lambda db: TrackingConnection(original(db))
            try: source_snapshot.snapshots_for_database(path)
            finally: source_snapshot.connect_readonly = original
        self.assertEqual(seen, [True])
