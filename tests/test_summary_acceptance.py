from __future__ import annotations

import hashlib
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.memory_sidecar import initialize_memory_sidecar
from memory.summary_acceptance import execute_acceptance, prepare_acceptance


def fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript("CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, memory_space TEXT); CREATE TABLE messages (message_id TEXT, conversation_id TEXT, role TEXT, text TEXT, create_time_iso TEXT, memory_space TEXT); CREATE TABLE memory_summaries (id TEXT, conversation_id TEXT, memory_space TEXT, summary_type TEXT, summary_text TEXT);")
        conn.execute("INSERT INTO conversations VALUES ('c1', 'whale')"); conn.executemany("INSERT INTO messages VALUES (?, 'c1', ?, ?, ?, 'whale')", [("m1", "user", "Discuss controlled acceptance.", "2026-01-01T00:00:00Z"), ("m2", "assistant", "Use a read-only source.", "2026-01-01T00:01:00Z")]); conn.commit()
    finally: conn.close()


class SummaryAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.source = Path(self.tmp.name) / "source.db"; self.sidecar = Path(self.tmp.name) / "sidecar.db"; self.backups = Path(self.tmp.name) / "backups"; fixture(self.source)
    def tearDown(self): self.tmp.cleanup()
    def prepare(self): return prepare_acceptance(self.source, self.sidecar, "c1")
    def accept(self, plan): return execute_acceptance(self.source, self.sidecar, "c1", confirm_candidate_id=plan["candidate"]["candidate_id"], confirm_plan_id=plan["acceptance_plan"]["acceptance_plan_id"], backup_dir=self.backups)

    def test_prepare_is_deterministic_text_free_and_does_not_write(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest(); first, second = self.prepare(), self.prepare()
        self.assertEqual("sidecar_not_initialized", first["target_status"]); self.assertEqual(first["acceptance_plan"]["acceptance_plan_id"], second["acceptance_plan"]["acceptance_plan_id"]); self.assertNotIn("candidate_text", first["candidate"]); self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest()); self.assertFalse(self.sidecar.exists())

    def test_execute_requires_confirmation_then_accepts_once_with_backup(self):
        initialize_memory_sidecar(self.sidecar, execute=True); plan = self.prepare()
        refused = execute_acceptance(self.source, self.sidecar, "c1", confirm_candidate_id=None, confirm_plan_id=None, backup_dir=self.backups)
        self.assertEqual("refused", refused["status"]); self.assertFalse(refused["backup_created"])
        accepted = self.accept(plan); self.assertEqual("accepted", accepted["status"]); self.assertEqual(1, accepted["summary_version"]); self.assertTrue(accepted["backup_created"]); self.assertNotIn("Discuss controlled", str(accepted)); self.assertNotIn(str(self.tmp.name), str(accepted))
        again = self.accept(plan); self.assertEqual("already_accepted", again["status"]); self.assertEqual(1, len(list(self.backups.glob("*.db"))))

    def test_changed_source_and_plan_are_rejected_without_backup(self):
        initialize_memory_sidecar(self.sidecar, execute=True); plan = self.prepare()
        conn = sqlite3.connect(self.source); conn.execute("UPDATE messages SET text='changed' WHERE message_id='m1'"); conn.commit(); conn.close()
        result = self.accept(plan); self.assertEqual("stale_plan", result["status"]); self.assertFalse(result["backup_created"]); self.assertFalse(self.backups.exists())
