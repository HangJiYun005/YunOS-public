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


def create_health_db(db_path: Path, include_table: bool = True) -> None:
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
            conn.executemany(
                """
                INSERT INTO memory_summaries (
                    memory_space, conversation_id, summary_type, summary_text, keywords_json,
                    source_message_count, source_conversation_count, method, model_name,
                    created_at, updated_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "whale",
                        "whale-conv",
                        "conversation_summary",
                        "A useful whale summary with enough detail to pass the short-text check.",
                        json.dumps(["whale", "summary"]),
                        12,
                        1,
                        "extractive_v1",
                        None,
                        "2026-07-05T20:10:04+08:00",
                        None,
                        "{}",
                    ),
                    (
                        "whale",
                        None,
                        "memory_space_overview",
                        "Whale overview summary with enough detail for read-only health checks.",
                        json.dumps(["overview"]),
                        688,
                        1,
                        "extractive_v1",
                        None,
                        "2026-07-05T20:10:11+08:00",
                        None,
                        "{}",
                    ),
                    (
                        "general",
                        "general-conv",
                        "conversation_summary",
                        "General summary with enough detail for a separate memory space.",
                        json.dumps(["general"]),
                        5,
                        1,
                        "extractive_v1",
                        None,
                        "2026-07-04T20:10:04+08:00",
                        None,
                        "{}",
                    ),
                ],
            )
        conn.commit()


def table_exists(db_path: Path, table_name: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table_name,),
            ).fetchone()
            is not None
        )


class MemorySummaryHealthTest(unittest.TestCase):
    def test_empty_summary_text_warns(self):
        from memory.summary_health import diagnose_summary

        result = diagnose_summary(
            {
                "id": 1,
                "memory_space": "whale",
                "conversation_id": "conv",
                "summary_type": "conversation_summary",
                "summary_text": "",
                "keywords_json": json.dumps(["whale"]),
                "source_message_count": 1,
                "source_conversation_count": 1,
                "method": "extractive_v1",
            }
        )

        self.assertFalse(result["has_summary_text"])
        self.assertLessEqual(result["health_score"], 40)
        self.assertTrue(any("summary_text" in warning for warning in result["warnings"]))

    def test_short_summary_text_deducts_score(self):
        from memory.summary_health import diagnose_summary

        result = diagnose_summary(
            {
                "id": 2,
                "memory_space": "whale",
                "conversation_id": "conv",
                "summary_type": "conversation_summary",
                "summary_text": "Too short",
                "keywords_json": json.dumps(["whale"]),
                "source_message_count": 1,
                "source_conversation_count": 1,
                "method": "extractive_v1",
            }
        )

        self.assertEqual(result["summary_length"], 9)
        self.assertLess(result["health_score"], 100)
        self.assertTrue(any("too short" in warning for warning in result["warnings"]))

    def test_conversation_summary_requires_conversation_id(self):
        from memory.summary_health import diagnose_summary

        result = diagnose_summary(
            {
                "id": 3,
                "memory_space": "whale",
                "conversation_id": None,
                "summary_type": "conversation_summary",
                "summary_text": "Enough content for this diagnostic test summary.",
                "keywords_json": json.dumps(["whale"]),
                "source_message_count": 1,
                "source_conversation_count": 1,
                "method": "extractive_v1",
            }
        )

        self.assertFalse(result["has_required_conversation_id"])
        self.assertTrue(any("conversation_id" in warning for warning in result["warnings"]))

    def test_memory_space_overview_allows_empty_conversation_id(self):
        from memory.summary_health import diagnose_summary

        result = diagnose_summary(
            {
                "id": 4,
                "memory_space": "whale",
                "conversation_id": None,
                "summary_type": "memory_space_overview",
                "summary_text": "Enough overview content for this diagnostic test summary.",
                "keywords_json": json.dumps(["overview"]),
                "source_message_count": 10,
                "source_conversation_count": 1,
                "method": "extractive_v1",
            }
        )

        self.assertTrue(result["has_required_conversation_id"])
        self.assertFalse(any("conversation_id" in warning for warning in result["warnings"]))

    def test_diagnose_summaries_counts_groups_and_average_score(self):
        from memory.summary_health import diagnose_summaries

        result = diagnose_summaries(
            [
                {
                    "id": 1,
                    "memory_space": "whale",
                    "conversation_id": "conv",
                    "summary_type": "conversation_summary",
                    "summary_text": "Enough content for this diagnostic test summary.",
                    "keywords_json": json.dumps(["whale"]),
                    "source_message_count": 2,
                    "source_conversation_count": 1,
                    "method": "extractive_v1",
                },
                {
                    "id": 2,
                    "memory_space": "general",
                    "conversation_id": None,
                    "summary_type": "memory_space_overview",
                    "summary_text": "",
                    "keywords_json": "",
                    "source_message_count": 0,
                    "source_conversation_count": 0,
                    "method": "",
                },
            ]
        )

        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["by_memory_space"], {"whale": 1, "general": 1})
        self.assertEqual(result["by_summary_type"], {"conversation_summary": 1, "memory_space_overview": 1})
        self.assertEqual(result["total_source_message_count"], 2)
        self.assertGreater(result["warning_count"], 0)
        self.assertGreater(result["average_health_score"], 0)
        self.assertLess(result["average_health_score"], 100)

    def test_cli_reads_test_database_without_writing(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_health_db(db_path)
            before_size = db_path.stat().st_size

            result = subprocess.run(
                [
                    sys.executable,
                    "tools/summary_health.py",
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
            after_size = db_path.stat().st_size

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["overall"]["total_count"], 2)
        self.assertTrue(payload["readonly"])
        self.assertEqual(before_size, after_size)

    def test_cli_real_database_requires_real_readonly(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "ayunos_chatgpt.db"
            create_health_db(db_path)

            result = subprocess.run(
                [
                    sys.executable,
                    "tools/summary_health.py",
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

    def test_api_memory_summary_health_returns_json(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_health_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/memory-summary-health?memory_space=whale")

        self.assertTrue(data["readonly"])
        self.assertEqual(data["overall"]["total_count"], 2)
        self.assertIn("health_score", data["items"][0])

    def test_api_memory_summary_health_does_not_expose_local_path(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_health_db(db_path)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/memory-summary-health?memory_space=whale")

        self.assertNotIn(str(db_path), json.dumps(data))

    def test_api_memory_summary_health_missing_table_returns_empty_diagnostic(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_health_db(db_path, include_table=False)

            with local_server(db_path) as base_url:
                data = fetch_json(f"{base_url}/api/memory-summary-health?memory_space=whale")

        self.assertEqual(data["items"], [])
        self.assertEqual(data["overall"]["total_count"], 0)
        self.assertFalse(table_exists(db_path, "memory_summaries"))

    def test_api_memory_summary_health_invalid_memory_space_returns_400(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_health_db(db_path)

            with local_server(db_path) as base_url:
                with self.assertRaises(HTTPError) as ctx:
                    fetch_json(f"{base_url}/api/memory-summary-health?memory_space=all")
                body = json.loads(ctx.exception.read().decode("utf-8"))

        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(body["error"], "Invalid memory space")

    def test_local_web_adds_no_memory_summary_health_post(self):
        source = (PROJECT_ROOT / "tools" / "local_web.py").read_text(encoding="utf-8")

        self.assertNotIn('/api/memory-summary-health", { method: "POST"', source)
        self.assertNotIn("api_memory_summary_health_post", source)


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
