from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.change_detector import detect_changes
from memory.memory_sidecar import initialize_memory_sidecar
from memory.summary_acceptance import execute_acceptance, prepare_acceptance
from memory.summary_repository import _append_summary_record


def make_source(path: Path, legacy: bool = False) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript("CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, memory_space TEXT); CREATE TABLE messages (message_id TEXT, conversation_id TEXT, role TEXT, text TEXT, create_time_iso TEXT, memory_space TEXT); CREATE TABLE memory_summaries (id TEXT, conversation_id TEXT, memory_space TEXT, summary_type TEXT, summary_text TEXT);")
        conn.execute("INSERT INTO conversations VALUES ('c1','whale')")
        conn.executemany("INSERT INTO messages VALUES (?, 'c1', ?, ?, ?, 'whale')", [("m1", "user", "First source body.", "2026-01-01T00:00:00Z"), ("m2", "assistant", "Second source body.", "2026-01-01T00:01:00Z")])
        if legacy:
            conn.execute("INSERT INTO memory_summaries VALUES ('legacy','c1','whale','conversation_summary','legacy body')")
        conn.commit()
    finally:
        conn.close()


class MemorySidecarChangeDetectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.source = Path(self.tmp.name) / "source.db"
        self.sidecar = Path(self.tmp.name) / "sidecar.db"
        make_source(self.source)
        initialize_memory_sidecar(self.sidecar, execute=True)

    def tearDown(self):
        self.tmp.cleanup()

    def item(self):
        return detect_changes(self.source, conversation_id="c1", memory_sidecar_path=self.sidecar)["items"][0]

    def accept(self):
        plan = prepare_acceptance(self.source, self.sidecar, "c1")
        return execute_acceptance(self.source, self.sidecar, "c1", confirm_candidate_id=plan["candidate"]["candidate_id"], confirm_plan_id=plan["acceptance_plan"]["acceptance_plan_id"], backup_dir=Path(self.tmp.name) / "backups")

    def test_e2e_accepted_sidecar_tracks_source(self):
        self.assertEqual("summary_missing", self.item()["status"])
        self.assertEqual("accepted", self.accept()["status"])
        current = self.item()
        self.assertEqual(("up_to_date", "memory_sidecar", 1), (current["status"], current["summary_source"], current["summary_version"]))
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("INSERT INTO messages VALUES ('m3','c1','user','New source body.','2026-01-01T00:02:00Z','whale')")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual("new_messages", self.item()["status"])
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("DELETE FROM messages WHERE message_id='m3'")
            conn.execute("UPDATE messages SET text='Changed body.' WHERE message_id='m1'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual("source_changed", self.item()["status"])

    def test_sidecar_is_preferred_and_legacy_without_coverage_remains_unknown(self):
        self.accept()
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("INSERT INTO memory_summaries VALUES ('legacy','c1','whale','conversation_summary','legacy')")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual("memory_sidecar", self.item()["summary_source"])
        legacy = Path(self.tmp.name) / "legacy.db"
        make_source(legacy, legacy=True)
        item = detect_changes(legacy, conversation_id="c1", memory_sidecar_path=Path(self.tmp.name) / "absent.db")["items"][0]
        self.assertEqual(("coverage_unknown", "legacy", None), (item["status"], item["summary_source"], item["summary_version"]))

    def test_latest_sidecar_version_is_selected(self):
        accepted = self.accept()
        conn = sqlite3.connect(self.sidecar)
        try:
            row = conn.execute("SELECT * FROM summary_versions WHERE summary_id=?", (accepted["summary_id"],)).fetchone()
            names = [entry[1] for entry in conn.execute("PRAGMA table_info(summary_versions)")]
            record = dict(zip(names, row))
            record.update({"summary_id": "second", "candidate_id": "second-candidate", "version": 2, "acceptance_run_id": "run2"})
            _append_summary_record(conn, record)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(2, self.item()["summary_version"])
