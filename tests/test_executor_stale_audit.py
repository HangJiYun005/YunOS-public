from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.lifecycle_executor import execute_memory_lifecycle
from memory.lifecycle_orchestrator import build_lifecycle_execution_plan, execute_lifecycle_plan
from tools.init_semantic_sidecar import CREATE_MEMORY_EMBEDDINGS_SQL
from tools import run_memory_lifecycle


class ExecutorStaleAuditTest(unittest.TestCase):
    def setUp(self):
        self.holder = TemporaryDirectory()
        root = Path(self.holder.name)
        self.source = root / "source.db"
        self.sidecar = root / "sidecar.db"
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("CREATE TABLE memory_summaries (id TEXT PRIMARY KEY, memory_space TEXT, summary_type TEXT, conversation_id TEXT, summary_text TEXT)")
            conn.execute("INSERT INTO memory_summaries VALUES ('s1', 'whale', 'conversation_summary', 'c1', 'one')")
            conn.commit()
        finally:
            conn.close()
        conn = sqlite3.connect(self.sidecar)
        try:
            conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
            conn.commit()
        finally:
            conn.close()
        self.patches = [
            patch("memory.db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH", self.sidecar),
            patch("memory.lifecycle_orchestrator.DEFAULT_SEMANTIC_SIDECAR_PATH", self.sidecar),
            patch("memory.sidecar_maintenance.BACKUP_ROOT", root / "backups"),
        ]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.holder.cleanup()

    def plan(self):
        return build_lifecycle_execution_plan(self.source, self.sidecar, "whale", "deterministic_test")

    def executor(self, plan, **changes):
        values = {
            "confirmed_plan_schema_version": plan["plan_schema_version"],
            "confirmed_plan_id": plan["plan_id"],
            "confirmed_plan_items": plan["items"],
            "confirmed_provider_identity": plan["provider_identity"],
            "confirmed_target_kind": plan["target_kind"],
            "confirm_production_sidecar_write": True,
        }
        values.update(changes)
        return execute_memory_lifecycle(self.source, self.sidecar, "whale", "deterministic_test", dry_run=False, allow_production_sidecar=True, **values)

    def assert_audit(self, result, status, plan_id):
        audit = result["audit"]
        self.assertEqual(status, result["status"])
        self.assertFalse(result["executed"])
        self.assertEqual(plan_id, audit["plan_id"])
        self.assertEqual("production_semantic_sidecar", audit["target_kind"])
        self.assertEqual("production_semantic_sidecar", audit["target_scope"])
        for field in ("run_id", "plan_schema_version", "started_at", "completed_at", "transaction_committed", "transaction_rolled_back", "backup_created", "backup_name", "backup_sha256", "planned_count", "inserted_count", "duplicate_count", "failed_count", "before_status_counts", "after_status_counts", "readonly_source"):
            self.assertIn(field, audit)
        self.assertFalse(audit["transaction_committed"])
        self.assertFalse(audit["transaction_rolled_back"])
        self.assertFalse(audit["backup_created"])
        encoded = json.dumps(audit)
        for forbidden in ("summary_text", "embedding_json", str(self.sidecar)):
            self.assertNotIn(forbidden, encoded)

    def test_missing_confirmation_and_executor_hash_stale_use_complete_audit(self):
        plan = self.plan()
        refused = self.executor(plan, confirmed_plan_items=None)
        self.assert_audit(refused, "refused", plan["plan_id"])
        changed = {"id": "s1", "memory_space": "whale", "summary_type": "conversation_summary", "conversation_id": "c1", "summary_text": "changed"}
        with patch("memory.sidecar_maintenance._load_summary", return_value=changed):
            stale = self.executor(plan)
        self.assert_audit(stale, "stale_plan", plan["plan_id"])

    def test_executor_stale_propagates_through_orchestrator(self):
        plan = self.plan()
        changed = {"id": "s1", "memory_space": "whale", "summary_type": "conversation_summary", "conversation_id": "c1", "summary_text": "changed"}
        with patch("memory.sidecar_maintenance._load_summary", return_value=changed):
            result = execute_lifecycle_plan(self.source, self.sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id=plan["plan_id"], allow_production_sidecar=True, confirm_production_sidecar_write=True)
        self.assert_audit(result, "stale_plan", plan["plan_id"])
        self.assertEqual(result["plan_id"], result["audit"]["plan_id"])

    def test_cli_json_preserves_canonical_status_plan_and_scope(self):
        plan = self.plan()
        result = execute_lifecycle_plan(self.source, self.sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id="wrong", allow_production_sidecar=True, confirm_production_sidecar_write=True)
        output = io.StringIO()
        argv = ["run_memory_lifecycle.py", "--source-db", str(self.source), "--semantic-db", str(self.sidecar), "--execute", "--confirm-plan-id", "wrong", "--allow-production-sidecar", "--confirm-production-sidecar-write", "--json"]
        with patch.object(sys, "argv", argv), patch("tools.run_memory_lifecycle.execute_lifecycle_plan", return_value=result), contextlib.redirect_stdout(output):
            code = run_memory_lifecycle.main()
        payload = json.loads(output.getvalue())
        self.assertEqual(2, code)
        self.assertEqual(("stale_plan", plan["plan_id"], "production_semantic_sidecar"), (payload["status"], payload["plan_id"], payload["target_scope"]))


if __name__ == "__main__":
    unittest.main()
