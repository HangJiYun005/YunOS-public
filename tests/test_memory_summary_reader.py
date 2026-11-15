import json
import sqlite3
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_reader_db(db_path: Path, include_table: bool = True, rows: int = 3) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                memory_space TEXT DEFAULT 'general'
            );
            CREATE TABLE messages (
                message_id TEXT,
                conversation_id TEXT,
                role TEXT,
                text TEXT,
                memory_space TEXT DEFAULT 'general'
            );
            """
        )
        if include_table:
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
            base_rows = [
                (
                    "whale",
                    "whale-conv",
                    "conversation_summary",
                    "Whale conversation summary",
                    2,
                    1,
                    "2026-07-05T20:10:04+08:00",
                ),
                (
                    "whale",
                    None,
                    "memory_space_overview",
                    "Whale overview summary",
                    688,
                    1,
                    "2026-07-05T20:10:11+08:00",
                ),
                (
                    "general",
                    "general-conv",
                    "conversation_summary",
                    "General summary",
                    2,
                    1,
                    "2026-07-04T20:10:04+08:00",
                ),
            ]
            generated = []
            for index in range(rows):
                row = base_rows[index % len(base_rows)]
                generated.append(
                    (
                        row[0],
                        row[1] if index < len(base_rows) else f"{row[1] or 'overview'}-{index}",
                        row[2],
                        f"{row[3]} {index}",
                        json.dumps(["summary", str(index)]),
                        row[4],
                        row[5],
                        "extractive_v1",
                        None,
                        row[6],
                        None,
                        "{}",
                    )
                )
            conn.executemany(
                """
                INSERT INTO memory_summaries (
                    memory_space, conversation_id, summary_type, summary_text, keywords_json,
                    source_message_count, source_conversation_count, method, model_name,
                    created_at, updated_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                generated,
            )
        conn.commit()


def table_exists(db_path: Path, table_name: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None


class MemorySummaryReaderTest(unittest.TestCase):
    def test_list_summaries_missing_table_returns_empty(self):
        from memory.summary_reader import list_summaries

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path, include_table=False)

            rows = list_summaries(db_path)

        self.assertEqual(rows, [])

    def test_get_summary_count_missing_table_returns_zero(self):
        from memory.summary_reader import get_summary_count

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path, include_table=False)

            count = get_summary_count(db_path)

        self.assertEqual(count, 0)

    def test_list_summaries_reads_test_database_summary(self):
        from memory.summary_reader import list_summaries

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path)

            rows = list_summaries(db_path, memory_space="whale")

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["memory_space"] for row in rows}, {"whale"})
        self.assertIn("summary_text", rows[0])
        self.assertNotIn("text", rows[0])

    def test_filters_memory_space_summary_type_and_conversation_id(self):
        from memory.summary_reader import list_summaries

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path)

            whale_rows = list_summaries(db_path, memory_space="whale")
            overview_rows = list_summaries(db_path, summary_type="memory_space_overview")
            conversation_rows = list_summaries(db_path, conversation_id="whale-conv")

        self.assertEqual(len(whale_rows), 2)
        self.assertEqual(len(overview_rows), 1)
        self.assertEqual(overview_rows[0]["summary_type"], "memory_space_overview")
        self.assertEqual(len(conversation_rows), 1)
        self.assertEqual(conversation_rows[0]["conversation_id"], "whale-conv")

    def test_limit_is_clamped_to_100(self):
        from memory.summary_reader import list_summaries

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path, rows=120)

            rows = list_summaries(db_path, limit=500)

        self.assertEqual(len(rows), 100)

    def test_readonly_reader_does_not_create_table_or_write(self):
        from memory.summary_reader import list_summaries

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path, include_table=False)
            before_size = db_path.stat().st_size

            rows = list_summaries(db_path)
            after_size = db_path.stat().st_size

        self.assertEqual(rows, [])
        self.assertEqual(before_size, after_size)

    def test_cli_reads_test_database_summary(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path)

            result = subprocess.run(
                [
                    sys.executable,
                    "tools/view_memory_summaries.py",
                    "--db",
                    str(db_path),
                    "--memory-space",
                    "whale",
                    "--json",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 2)
        self.assertEqual({item["memory_space"] for item in payload["items"]}, {"whale"})

    def test_cli_real_database_requires_real_readonly_flag(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "ayunos_chatgpt.db"
            create_reader_db(db_path)

            result = subprocess.run(
                [
                    sys.executable,
                    "tools/view_memory_summaries.py",
                    "--db",
                    str(db_path),
                    "--memory-space",
                    "whale",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--real-readonly", result.stderr)

    def test_api_memory_summaries_returns_json_without_path(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/memory-summaries?memory_space=whale&limit=20")

        self.assertEqual(data["count"], 2)
        self.assertTrue(data["readonly"])
        self.assertNotIn(str(db_path), json.dumps(data))
        self.assertEqual({item["memory_space"] for item in data["items"]}, {"whale"})

    def test_api_memory_summaries_missing_table_returns_empty_items(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path, include_table=False)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/memory-summaries?memory_space=whale")

        self.assertEqual(data["items"], [])
        self.assertEqual(data["count"], 0)
        self.assertFalse(table_exists(db_path, "memory_summaries"))

    def test_api_memory_summaries_invalid_memory_space_returns_json_400(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_reader_db(db_path)

            with local_server(db_path) as base_url:
                with self.assertRaises(HTTPError) as ctx:
                    fetch_json(f"{base_url}/api/memory-summaries?memory_space=all")
                body = json.loads(ctx.exception.read().decode("utf-8"))

        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(body["error"], "Invalid memory space")

    def test_web_app_has_summary_entry_without_summary_post(self):
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        app = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn(
            '{ key: "summaries", labelKey: "nav.memorySummaries", fallback: "记忆摘要", view: "summaries" }',
            app,
        )
        self.assertIn("memory-summaries", app)
        self.assertNotIn('/api/memory-summaries", { method: "POST"', app)
        self.assertNotIn("summary POST", app)


class local_server:
    def __init__(self, db_path: Path):
        from tools.local_web import App, AyunOSHandler

        self.handler = AyunOSHandler
        self.handler.app = App(db_path.resolve(), real=False, readonly=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def fetch_json(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
