import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = PROJECT_ROOT / "tools" / "optimize_whale_context.py"


def create_optimizer_db(db_path: Path) -> None:
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
            INSERT INTO conversations (
                conversation_id, title, update_time, update_time_iso, memory_space, source_platform
            )
            VALUES
                ('whale-conv', 'alpha whale', 20, '2026-07-05T00:00:20', 'whale', 'deepseek'),
                ('general-conv', 'alpha general', 10, '2026-07-05T00:00:10', 'general', 'chatgpt');
            INSERT INTO messages (
                message_id, conversation_id, conversation_title, create_time, create_time_iso,
                role, text, memory_space, source_platform
            )
            VALUES
                ('whale-msg-1', 'whale-conv', 'alpha whale', 31, '2026-07-05T00:00:31',
                 'user', 'alpha whale memory one', 'whale', 'deepseek'),
                ('whale-msg-2', 'whale-conv', 'alpha whale', 30, '2026-07-05T00:00:30',
                 'assistant', 'alpha whale memory two', 'whale', 'deepseek'),
                ('general-msg-1', 'general-conv', 'alpha general', 11, '2026-07-05T00:00:11',
                 'user', 'alpha general memory one', 'general', 'chatgpt');
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


def database_counts(db_path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(db_path) as conn:
        return (
            int(conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM memory_summaries").fetchone()[0]),
        )


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
    )


class WhaleContextOptimizerTest(unittest.TestCase):
    def test_cli_json_runs_and_recommends_config_without_prompt(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            result = run_cli("alpha", "--db", str(db_path), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertGreaterEqual(len(data["candidates"]), 16)
        self.assertEqual(len(data["top_configs"]), 5)
        self.assertIn("recommended_config", data)
        self.assertIn("reason", data)
        self.assertNotIn("prompt", data)
        self.assertNotIn("[System]", result.stdout)

    def test_cli_text_runs_with_top_configs_and_recommendation(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            result = run_cli("alpha", "--db", str(db_path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Top 5 Configurations", result.stdout)
        self.assertIn("recommended_config", result.stdout)
        self.assertNotIn("[User Input]", result.stdout)

    def test_optimizer_does_not_write_database(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)
            before = database_counts(db_path)

            result = run_cli("alpha", "--db", str(db_path), "--json")

            after = database_counts(db_path)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(after, before)

    def test_optimizer_does_not_call_llm_or_api(self):
        import tools.optimize_whale_context as optimizer

        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)
            with patch.object(optimizer.whale_chat, "call_deepseek", side_effect=AssertionError("API called")):
                result = optimizer.optimize_context("alpha", db_path=db_path)

        self.assertIn("recommended_config", result)

    def test_default_does_not_test_general_context(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            result = run_cli("alpha", "--db", str(db_path), "--json")

        data = json.loads(result.stdout)
        self.assertEqual({candidate["include_general"] for candidate in data["candidates"]}, {False})
        self.assertTrue(all(candidate["general_context_count"] == 0 for candidate in data["candidates"]))

    def test_test_general_adds_include_general_true_candidates(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            result = run_cli("alpha", "--db", str(db_path), "--test-general", "--json")

        data = json.loads(result.stdout)
        self.assertIn(True, {candidate["include_general"] for candidate in data["candidates"]})
        self.assertGreater(len(data["candidates"]), 16)

    def test_candidate_fields_include_tokens_ordering_and_counts(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = Path(tmp) / "memory.db"
            create_optimizer_db(db_path)

            result = run_cli("alpha", "--db", str(db_path), "--json")

        candidate = json.loads(result.stdout)["candidates"][0]
        expected = {
            "summary_limit",
            "raw_memory_top_k",
            "include_general",
            "total_chars",
            "estimated_total_tokens",
            "summary_count",
            "raw_memory_count",
            "general_context_count",
            "summary_before_raw_memory",
            "enabled_sections",
            "score",
        }
        self.assertTrue(expected.issubset(candidate))
        self.assertGreater(candidate["estimated_total_tokens"], 0)
        self.assertEqual(candidate["summary_before_raw_memory"], True)


if __name__ == "__main__":
    unittest.main()
