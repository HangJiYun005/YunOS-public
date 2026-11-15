from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.memory_sidecar import initialize_memory_sidecar
from memory.source_snapshot import snapshots_for_database
from memory.summary_repository import _append_summary_record, graph_summary_sources


class GraphSummaryRepositoryTest(unittest.TestCase):
    def _source(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE conversations (
                    conversation_id TEXT PRIMARY KEY,
                    memory_space TEXT
                );
                CREATE TABLE messages (
                    message_id TEXT,
                    conversation_id TEXT,
                    role TEXT,
                    text TEXT,
                    created_at TEXT,
                    memory_space TEXT
                );
                CREATE TABLE memory_summaries (
                    id INTEGER PRIMARY KEY,
                    conversation_id TEXT,
                    memory_space TEXT,
                    summary_type TEXT,
                    summary_text TEXT,
                    created_at TEXT
                );
                """
            )
            conn.executemany("INSERT INTO conversations VALUES (?, ?)", [("c1", "whale"), ("c2", "general")])
            conn.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("m1", "c1", "user", "source one", "2026-01-01", "whale"),
                    ("m2", "c1", "assistant", "source two", "2026-01-02", "whale"),
                    ("m3", "c2", "user", "other source", "2026-01-03", "general"),
                ],
            )
            conn.executemany(
                "INSERT INTO memory_summaries VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, "c1", "whale", "conversation_summary", "legacy c1", "2026-01-01"),
                    (2, "c2", "general", "conversation_summary", "legacy c2", "2026-01-01"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _accepted(self, version: int, snapshot_hash: str) -> dict:
        text = f"accepted v{version}"
        return {
            "summary_id": str(version),
            "conversation_id": "c1",
            "memory_space": "whale",
            "version": version,
            "candidate_id": f"candidate-{version}",
            "candidate_schema_version": 1,
            "source_snapshot_hash": snapshot_hash,
            "source_message_count": 2,
            "source_last_message_at": "2026-01-02",
            "source_latest_message_id": "m2",
            "generator_identity": "test-generator",
            "generator_config_hash": hashlib.sha256(b"config").hexdigest(),
            "summary_text": text,
            "summary_text_hash": hashlib.sha256(text.encode()).hexdigest(),
            "reason": "summary_missing",
            "base_summary_id": None,
            "base_summary_text_hash": None,
            "acceptance_plan_id": f"plan-{version}",
            "acceptance_run_id": f"run-{version}",
            "accepted_at": "2026-01-04",
            "created_at": "2026-01-04",
        }

    def test_sidecar_versions_take_precedence_and_legacy_can_be_included(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, sidecar = root / "source.db", root / "sidecar.db"
            self._source(source)
            snapshot = snapshots_for_database(source, conversation_id="c1")[0]["source_content_hash"]
            initialize_memory_sidecar(sidecar, execute=True)
            conn = sqlite3.connect(sidecar)
            try:
                _append_summary_record(conn, self._accepted(1, snapshot))
                _append_summary_record(conn, self._accepted(2, snapshot))
                conn.commit()
            finally:
                conn.close()

            default = graph_summary_sources(sidecar, source, conversation_id="c1")
            inclusive = graph_summary_sources(sidecar, source, conversation_id="c1", include_legacy=True)
            self.assertEqual([1, 2], [item["summary_version"] for item in default])
            self.assertTrue(all(item["source_table"] == "summary_versions" for item in default))
            self.assertEqual(3, len(inclusive))
            self.assertEqual({"memory_summaries", "summary_versions"}, {item["source_table"] for item in inclusive})

    def test_legacy_fallback_binds_existing_source_snapshot(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, absent_sidecar = root / "source.db", root / "missing-sidecar.db"
            self._source(source)
            expected = snapshots_for_database(source, conversation_id="c1")[0]["source_content_hash"]
            records = graph_summary_sources(absent_sidecar, source, conversation_id="c1")
            self.assertEqual(1, len(records))
            self.assertEqual("memory_summaries", records[0]["source_table"])
            self.assertEqual(expected, records[0]["source_snapshot_hash"])
            self.assertFalse(absent_sidecar.exists())

    def test_filters_apply_before_graph_normalization_and_connections_release(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            source, absent_sidecar = root / "source.db", root / "missing-sidecar.db"
            self._source(source)
            records = graph_summary_sources(
                absent_sidecar,
                source,
                conversation_id="c2",
                memory_space="general",
            )
            self.assertEqual(["c2"], [item["conversation_id"] for item in records])
            source.unlink()
            self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
