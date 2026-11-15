from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.memory_sidecar import initialize_memory_sidecar
from memory.summary_acceptance import execute_acceptance, prepare_acceptance


class SummaryAcceptanceGatewayTest(unittest.TestCase):
    def source_fixture(self):
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup); source, sidecar = Path(tmp.name) / "source.db", Path(tmp.name) / "sidecar.db"
        conn = sqlite3.connect(source)
        try:
            conn.executescript("CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, memory_space TEXT); CREATE TABLE messages (message_id TEXT, conversation_id TEXT, role TEXT, text TEXT, create_time_iso TEXT, memory_space TEXT); CREATE TABLE memory_summaries (id TEXT, conversation_id TEXT, memory_space TEXT, summary_type TEXT, summary_text TEXT);")
            conn.execute("INSERT INTO conversations VALUES ('c1','whale')"); conn.execute("INSERT INTO messages VALUES ('m1','c1','user','safe body','2026','whale')"); conn.commit()
        finally: conn.close()
        return source, sidecar

    def test_uninitialized_sidecar_and_tampered_confirmations_are_refused(self):
        source, sidecar = self.source_fixture(); plan = prepare_acceptance(source, sidecar, "c1")
        self.assertEqual("sidecar_not_initialized", execute_acceptance(source, sidecar, "c1", confirm_candidate_id="x", confirm_plan_id="y")["status"])
        initialize_memory_sidecar(sidecar, execute=True); plan = prepare_acceptance(source, sidecar, "c1")
        self.assertEqual("stale_plan", execute_acceptance(source, sidecar, "c1", confirm_candidate_id="x", confirm_plan_id=plan["acceptance_plan"]["acceptance_plan_id"])["status"])
        self.assertEqual("stale_plan", execute_acceptance(source, sidecar, "c1", confirm_candidate_id=plan["candidate"]["candidate_id"], confirm_plan_id="x")["status"])

    def test_raw_production_write_helper_is_not_public(self):
        import memory.summary_repository as repository
        self.assertFalse(hasattr(repository, "write_summary_record")); self.assertFalse(hasattr(repository, "save_summary"))
