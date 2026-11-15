from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.lifecycle_orchestrator import build_lifecycle_execution_plan, execute_lifecycle_plan
from memory.sidecar_maintenance import _maintain_sidecar_transaction
from tests.test_strict_sidecar_transactions import record
from tools.init_semantic_sidecar import CREATE_MEMORY_EMBEDDINGS_SQL


ROOT = Path(__file__).resolve().parents[1]


class LifecycleAuditConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.holder = TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.holder.name)
        self.source = root / "source.db"
        self.sidecar = root / "sidecar.db"
        self.backups = root / "backups"
        with sqlite3.connect(self.source) as conn:
            conn.execute("CREATE TABLE memory_summaries (id TEXT PRIMARY KEY, memory_space TEXT, summary_type TEXT, conversation_id TEXT, summary_text TEXT)")
            conn.execute("INSERT INTO memory_summaries VALUES ('s1', 'whale', 'conversation_summary', 'c1', 'one')")
        with sqlite3.connect(self.sidecar) as conn:
            conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
        self.patches = [
            patch("memory.db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH", self.sidecar),
            patch("memory.lifecycle_orchestrator.DEFAULT_SEMANTIC_SIDECAR_PATH", self.sidecar),
            patch("memory.sidecar_maintenance.BACKUP_ROOT", self.backups),
        ]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.holder.cleanup()

    def execute(self):
        plan = build_lifecycle_execution_plan(self.source, self.sidecar, "whale", "deterministic_test")
        return plan, execute_lifecycle_plan(self.source, self.sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id=plan["plan_id"], allow_production_sidecar=True, confirm_production_sidecar_write=True)

    def test_committed_status_and_plan_id_are_consistent_at_all_layers(self):
        plan, result = self.execute()
        self.assertEqual("committed", result["status"])
        self.assertTrue(result["executed"])
        self.assertTrue(result["transaction_committed"])
        self.assertEqual(plan["plan_id"], result["plan_id"])
        self.assertEqual(plan["plan_id"], result["maintenance"]["plan_id"])
        self.assertEqual(plan["plan_id"], result["audit"]["plan_id"])
        self.assertEqual("production_semantic_sidecar", result["target_scope"])
        encoded = json.dumps(result["audit"])
        for forbidden in ("summary_text", "embedding_json", str(self.sidecar)):
            self.assertNotIn(forbidden, encoded)

    def test_rolled_back_and_busy_statuses_propagate_without_executed(self):
        plan = build_lifecycle_execution_plan(self.source, self.sidecar, "whale", "deterministic_test")
        for status in ("rolled_back", "maintenance_busy"):
            maintenance = self._maintenance_result(plan, status)
            with patch("memory.lifecycle_executor.execute_confirmed_sidecar_maintenance", return_value=maintenance):
                result = execute_lifecycle_plan(self.source, self.sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id=plan["plan_id"], allow_production_sidecar=True, confirm_production_sidecar_write=True)
            self.assertEqual(status, result["status"])
            self.assertFalse(result["executed"])

    def test_refused_stale_and_no_action_create_no_backup(self):
        plan = build_lifecycle_execution_plan(self.source, self.sidecar, "whale", "deterministic_test")
        refused = execute_lifecycle_plan(self.source, self.sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id=plan["plan_id"])
        stale = execute_lifecycle_plan(self.source, self.sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id="wrong", allow_production_sidecar=True, confirm_production_sidecar_write=True)
        self.assertEqual(("refused", False), (refused["status"], refused["backup_created"]))
        self.assertEqual(("stale_plan", False), (stale["status"], stale["backup_created"]))
        self.execute()
        empty = build_lifecycle_execution_plan(self.source, self.sidecar, "whale", "deterministic_test")
        backup_count = len(list(self.backups.glob("*.db")))
        before_hash = hashlib.sha256(self.sidecar.read_bytes()).hexdigest()
        no_action = execute_lifecycle_plan(self.source, self.sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id=empty["plan_id"], allow_production_sidecar=True, confirm_production_sidecar_write=True)
        self.assertEqual(("no_action", False), (no_action["status"], no_action["backup_created"]))
        self.assertEqual(empty["plan_id"], no_action["plan_id"])
        self.assertEqual(backup_count, len(list(self.backups.glob("*.db"))))
        self.assertEqual(before_hash, hashlib.sha256(self.sidecar.read_bytes()).hexdigest())

    def test_backup_names_are_unique_valid_and_restore_prestate(self):
        first = _maintain_sidecar_transaction([record("seed")], self.sidecar, "p" * 64)
        before_count = 1
        second = _maintain_sidecar_transaction([record("next")], self.sidecar, "p" * 64)
        self.assertNotEqual(first["backup_name"], second["backup_name"])
        backup = self.backups / second["backup_name"]
        self.assertEqual(second["backup_sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())
        restored = Path(self.holder.name) / "restored.db"
        source_conn = sqlite3.connect(backup); restored_conn = sqlite3.connect(restored)
        try: source_conn.backup(restored_conn)
        finally: restored_conn.close(); source_conn.close()
        with sqlite3.connect(restored) as conn:
            self.assertEqual(before_count, conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0])

    def _maintenance_result(self, plan, status):
        return {
            "run_id": "run", "plan_id": plan["plan_id"], "plan_schema_version": plan["plan_schema_version"],
            "target_kind": plan["target_kind"], "target_scope": "production_semantic_sidecar",
            "started_at": "start", "completed_at": "end", "status": status, "executed": False,
            "transaction_committed": False, "transaction_rolled_back": status == "rolled_back",
            "backup_created": True, "backup_name": "backup.db", "backup_sha256": "hash",
            "planned_count": 1, "inserted_count": 0, "duplicate_count": 0, "failed_count": int(status == "rolled_back"),
            "readonly_source": True,
        }


if __name__ == "__main__":
    unittest.main()
