from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.summary_candidate import generate_summary_candidate, preview_summary_candidates, public_preview


def fixture(path: Path, old_summary: bool = False, coverage: bool = True) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript("""
        CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, title TEXT, memory_space TEXT);
        CREATE TABLE messages (message_id TEXT, conversation_id TEXT, ordinal INTEGER, role TEXT, create_time_iso TEXT, text TEXT, memory_space TEXT);
        CREATE TABLE memory_summaries (id TEXT, conversation_id TEXT, memory_space TEXT, summary_type TEXT, summary_text TEXT, source_content_hash TEXT, source_message_count INTEGER, created_at TEXT);
        """)
        conn.executemany("INSERT INTO conversations VALUES (?, ?, ?)", [("missing", "Missing", "whale"), ("unknown", "Unknown", "general"), ("system", "System", "whale"), ("empty", "Empty", "whale")])
        rows = [("m1", "missing", 1, "user", "2026-01-01T00:00:00Z", "Plan safe summary review.", "whale"), ("m2", "missing", 2, "assistant", "2026-01-01T00:01:00Z", "Conclusion: keep it readonly.", "whale"), ("u1", "unknown", 1, "user", "2026-01-02T00:00:00Z", "Refresh this controlled record.", "general"), ("s1", "system", 1, "system", "2026-01-03T00:00:00Z", "internal instruction", "whale")]
        conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        if old_summary:
            columns = ("old", "unknown", "general", "conversation_summary", "old controlled summary", None if not coverage else "wrong", None if not coverage else 1, "2026-01-01T00:00:00Z")
            conn.execute("INSERT INTO memory_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", columns)
        conn.commit()
    finally:
        conn.close()


class SummaryCandidateTest(unittest.TestCase):
    def test_missing_candidate_is_deterministic_and_source_bound(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "fixture.db"; fixture(db)
            first = generate_summary_candidate(db, "missing", "whale", "summary_missing")
            second = generate_summary_candidate(db, "missing", "whale", "summary_missing")
            self.assertEqual("candidate_ready", first["status"])
            self.assertEqual(first["candidate"]["candidate_id"], second["candidate"]["candidate_id"])
            self.assertEqual(first["candidate"]["candidate_text"], second["candidate"]["candidate_text"])
            self.assertNotIn("m1", first["candidate"]["candidate_text"])
            conn = sqlite3.connect(db)
            try:
                conn.execute("UPDATE messages SET text = 'Changed source' WHERE message_id = 'm1'"); conn.commit()
            finally:
                conn.close()
            changed = generate_summary_candidate(db, "missing", "whale", "summary_missing")
            self.assertNotEqual(first["candidate"]["candidate_id"], changed["candidate"]["candidate_id"])

    def test_role_and_config_changes_change_candidate_id(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "fixture.db"; fixture(db)
            base = generate_summary_candidate(db, "missing", "whale", "summary_missing")["candidate"]
            config = generate_summary_candidate(db, "missing", "whale", "summary_missing", {"max_messages": 100, "max_text_chars": 1200, "method": "extractive_v1"})["candidate"]
            self.assertNotEqual(base["candidate_id"], config["candidate_id"])
            conn = sqlite3.connect(db)
            try:
                conn.execute("UPDATE messages SET role = 'assistant' WHERE message_id = 'm1'"); conn.commit()
            finally:
                conn.close()
            role_changed = generate_summary_candidate(db, "missing", "whale", "summary_missing")["candidate"]
            self.assertNotEqual(base["candidate_id"], role_changed["candidate_id"])

    def test_coverage_unknown_has_difference_and_public_text_gate(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "fixture.db"; fixture(db, old_summary=True, coverage=False)
            result = generate_summary_candidate(db, "unknown", "general", "coverage_unknown")
            self.assertEqual("candidate_ready", result["status"])
            self.assertIn("difference", result)
            hidden = public_preview({"items": [result]}, False)
            self.assertNotIn("candidate_text", hidden["items"][0]["candidate"])
            self.assertNotIn("old_summary_text", hidden["items"][0])
            shown = public_preview({"items": [result]}, True)
            self.assertIn("candidate_text", shown["items"][0]["candidate"])
            self.assertIn("old_summary_text", shown["items"][0])

    def test_filters_limits_and_safe_states_are_readonly(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "fixture.db"; fixture(db, old_summary=True, coverage=False)
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            payload = preview_summary_candidates(db, limit=500, memory_space="whale")
            self.assertEqual(50, payload["limit"])
            self.assertTrue(all(item.get("candidate", {}).get("memory_space") == "whale" or item.get("memory_space") == "whale" for item in payload["items"]))
            self.assertEqual(before, hashlib.sha256(db.read_bytes()).hexdigest())
            empty = generate_summary_candidate(db, "empty", "whale", "summary_missing")
            system = generate_summary_candidate(db, "system", "whale", "summary_missing")
            self.assertEqual("empty_source", empty["status"])
            self.assertEqual("unsupported_content", system["status"])
            renamed = db.with_name("renamed.db")
            db.rename(renamed)
            renamed.rename(db)
            db.unlink()

    def test_connections_close_and_no_external_api(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "fixture.db"; fixture(db)
            with patch("memory.summary_candidate.connect_readonly", wraps=__import__("memory.summary_candidate", fromlist=["connect_readonly"]).connect_readonly) as reader:
                generate_summary_candidate(db, "missing", "whale", "summary_missing")
            self.assertGreaterEqual(reader.call_count, 1)
            source = Path(__file__).resolve().parents[1] / "memory" / "summary_candidate.py"
            self.assertNotIn("requests", source.read_text(encoding="utf-8"))
            self.assertNotIn("openai", source.read_text(encoding="utf-8"))
