from __future__ import annotations

import hashlib
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.memory_sidecar import initialize_memory_sidecar
from memory.summary_acceptance import execute_acceptance, prepare_acceptance


def source_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, memory_space TEXT);"
            "CREATE TABLE messages (message_id TEXT, conversation_id TEXT, role TEXT, text TEXT, create_time_iso TEXT, memory_space TEXT);"
            "CREATE TABLE memory_summaries (id TEXT, conversation_id TEXT, memory_space TEXT, summary_type TEXT, summary_text TEXT);"
        )
        conn.execute("INSERT INTO conversations VALUES ('c1','whale')")
        conn.execute("INSERT INTO messages VALUES ('m1','c1','user','safe body','2026','whale')")
        conn.commit()
    finally:
        conn.close()


class SummaryAcceptanceHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "source.db"
        self.sidecar = root / "sidecar.db"
        self.backups = root / "backups"
        source_fixture(self.source)
        initialize_memory_sidecar(self.sidecar, execute=True)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        return prepare_acceptance(self.source, self.sidecar, "c1")

    def execute(self, plan, *, plan_id=None):
        return execute_acceptance(
            self.source,
            self.sidecar,
            "c1",
            confirm_candidate_id=plan["candidate"]["candidate_id"],
            confirm_plan_id=plan_id or plan["acceptance_plan"]["acceptance_plan_id"],
            backup_dir=self.backups,
        )

    def count(self) -> int:
        conn = sqlite3.connect(self.sidecar)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0])
        finally:
            conn.close()

    def test_hardlink_symlink_and_reparse_aliases_are_refused(self):
        root = Path(self.tmp.name)
        production_source = root / "production-source.db"
        production_semantic = root / "production-semantic.db"
        production_memory = root / "production-memory.db"
        source_fixture(production_source)
        sqlite3.connect(production_semantic).close()
        initialize_memory_sidecar(production_memory, execute=True)
        aliases = {
            "source": root / "source-hardlink.db",
            "semantic": root / "semantic-hardlink.db",
            "memory": root / "memory-hardlink.db",
        }
        os.link(production_source, aliases["source"])
        os.link(production_semantic, aliases["semantic"])
        os.link(production_memory, aliases["memory"])
        policy = (
            patch("memory.db_path_policy.DEFAULT_REAL_DB_PATH", production_source),
            patch("memory.db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH", production_semantic),
            patch("memory.summary_acceptance.PRODUCTION_MEMORY_SIDECAR", production_memory),
            patch("memory.memory_sidecar.PRODUCTION_MEMORY_SIDECAR", production_memory),
        )
        for item in policy:
            item.start()
            self.addCleanup(item.stop)

        self.assertEqual("refused", prepare_acceptance(aliases["source"], self.sidecar, "c1")["status"])
        self.assertEqual("refused", prepare_acceptance(aliases["semantic"], self.sidecar, "c1")["status"])
        self.assertEqual("refused", prepare_acceptance(aliases["memory"], self.sidecar, "c1")["status"])
        self.assertEqual("refused", prepare_acceptance(self.source, aliases["semantic"], "c1")["status"])
        self.assertEqual("refused", prepare_acceptance(self.source, aliases["memory"], "c1")["status"])
        alias_execute = execute_acceptance(
            self.source, aliases["memory"], "c1",
            confirm_candidate_id="candidate", confirm_plan_id="plan",
            real_readonly=True, allow_production_memory_sidecar=True,
            confirm_production_summary_write=True,
        )
        self.assertEqual("refused", alias_execute["status"])
        self.assertEqual("refused", initialize_memory_sidecar(aliases["memory"], execute=True)["status"])
        dotted_production = production_memory.parent / "unused" / ".." / production_memory.name
        self.assertEqual("refused", initialize_memory_sidecar(dotted_production, execute=True)["status"])

        symlink = root / "memory-symlink.db"
        try:
            os.symlink(production_memory, symlink)
        except OSError:
            symlink = None
        if symlink is not None:
            self.assertEqual("refused", prepare_acceptance(self.source, symlink, "c1")["status"])

        linked_parent = root / "linked-parent"
        linked_parent.mkdir()
        with patch("memory.db_path_policy.is_reparse_point", side_effect=lambda path: Path(path) == linked_parent):
            self.assertEqual("refused", prepare_acceptance(self.source, linked_parent / "sidecar.db", "c1")["status"])

    @unittest.skipUnless(os.name == "nt", "Windows mandatory source guard")
    def test_open_writable_source_returns_source_busy_without_backup(self):
        plan = self.plan()
        writer = sqlite3.connect(self.source)
        try:
            result = self.execute(plan)
        finally:
            writer.close()
        self.assertEqual("source_busy", result["status"])
        self.assertFalse(result["backup_created"])
        self.assertEqual(0, self.count())
        self.assertFalse(self.backups.exists())

    def test_sidecar_lock_returns_busy_without_backup(self):
        plan = self.plan()
        lock = sqlite3.connect(self.sidecar)
        lock.execute("BEGIN IMMEDIATE")
        try:
            result = self.execute(plan)
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual("sidecar_busy", result["status"])
        self.assertFalse(result["backup_created"])
        self.assertEqual(0, self.count())
        self.assertFalse(self.backups.exists())

    def test_base_change_is_stale_and_creates_no_backup(self):
        plan = self.plan()
        conn = sqlite3.connect(self.sidecar)
        try:
            conn.execute(
                "INSERT INTO summary_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "base", "c1", "whale", 1, "other-candidate", 1, "hash", 0, None, None,
                    "generator", "config", "base text", hashlib.sha256(b"base text").hexdigest(),
                    "summary_missing", None, None, "other-plan", "other-run", "2026", "2026",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        result = self.execute(plan)
        self.assertEqual("stale_plan", result["status"])
        self.assertFalse(result["backup_created"])
        self.assertEqual(1, self.count())
        self.assertFalse(self.backups.exists())

    def test_wrong_plan_is_stale_before_idempotent_success(self):
        plan = self.plan()
        accepted = self.execute(plan)
        self.assertEqual("accepted", accepted["status"])
        backup_count = len(list(self.backups.glob("*.db")))
        wrong = self.execute(plan, plan_id="wrong-plan")
        self.assertEqual("stale_plan", wrong["status"])
        self.assertFalse(wrong["backup_created"])
        self.assertEqual(backup_count, len(list(self.backups.glob("*.db"))))
        repeated = self.execute(plan)
        self.assertEqual("already_accepted", repeated["status"])
        self.assertEqual(backup_count, len(list(self.backups.glob("*.db"))))

    def test_insert_failure_rolls_back_and_closes_connections(self):
        plan = self.plan()
        with patch("memory.summary_acceptance._append_summary_record", side_effect=ValueError("simulated")):
            result = self.execute(plan)
        self.assertEqual("rolled_back", result["status"])
        self.assertTrue(result["transaction_rolled_back"])
        self.assertTrue(result["backup_created"])
        self.assertEqual(0, self.count())
        self.assertEqual(1, len(list(self.backups.glob("*.db"))))


if __name__ == "__main__":
    unittest.main()
