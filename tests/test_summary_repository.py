from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.memory_sidecar import initialize_memory_sidecar
from memory.summary_repository import _append_summary_record, candidate_is_accepted, current_summary, latest_accepted_summary, list_summary_versions, next_version


class SummaryRepositoryTest(unittest.TestCase):
    def record(self, candidate_id="candidate-1", version=1):
        text = "accepted summary"
        return {"summary_id": f"summary-{version}", "conversation_id": "c1", "memory_space": "whale", "version": version, "candidate_id": candidate_id, "candidate_schema_version": 1, "source_snapshot_hash": "source", "source_message_count": 2, "source_last_message_at": "2026", "source_latest_message_id": "m2", "generator_identity": "generator", "generator_config_hash": "config", "summary_text": text, "summary_text_hash": hashlib.sha256(text.encode()).hexdigest(), "reason": "summary_missing", "base_summary_id": None, "base_summary_text_hash": None, "acceptance_plan_id": "plan", "acceptance_run_id": "run", "accepted_at": "2026", "created_at": "2026"}

    def test_versions_are_append_only_and_latest_wins(self):
        with TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "sidecar.db"; initialize_memory_sidecar(sidecar, execute=True)
            conn = sqlite3.connect(sidecar)
            try:
                _append_summary_record(conn, self.record()); _append_summary_record(conn, self.record("candidate-2", 2)); conn.commit()
            finally: conn.close()
            self.assertEqual(3, next_version(sidecar, "c1"))
            self.assertEqual(2, latest_accepted_summary(sidecar, "c1")["version"])
            self.assertTrue(candidate_is_accepted(sidecar, "candidate-1"))
            self.assertEqual([1, 2], [item["version"] for item in list_summary_versions(sidecar, "c1")])

    def test_current_summary_falls_back_to_legacy_without_copying(self):
        with TemporaryDirectory() as tmp:
            sidecar, source = Path(tmp) / "sidecar.db", Path(tmp) / "source.db"; initialize_memory_sidecar(sidecar, execute=True)
            conn = sqlite3.connect(source)
            try:
                conn.execute("CREATE TABLE memory_summaries (id TEXT, conversation_id TEXT, memory_space TEXT, summary_type TEXT, summary_text TEXT)")
                conn.execute("INSERT INTO memory_summaries VALUES ('legacy', 'c1', 'whale', 'conversation_summary', 'legacy text')"); conn.commit()
            finally: conn.close()
            self.assertEqual("legacy", current_summary(sidecar, source, "c1", "whale")["summary_source"])
