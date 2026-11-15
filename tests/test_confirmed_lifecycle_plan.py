from __future__ import annotations

import copy
import hashlib
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.lifecycle_executor import execute_memory_lifecycle
from memory.lifecycle_orchestrator import build_lifecycle_execution_plan, execute_lifecycle_plan
from memory.lifecycle_planner import calculate_lifecycle_plan_id
from tools.migrate_semantic_memory import migrate_semantic_memory


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "data" / "test"


class ConfirmedLifecyclePlanTest(unittest.TestCase):
    def setUp(self):
        self.source_holder = TemporaryDirectory()
        self.holder = TemporaryDirectory(dir=TEST_ROOT)
        self.source = Path(self.source_holder.name) / "source.db"
        root = Path(self.holder.name)
        self.sidecar = root / "sidecar.db"
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("CREATE TABLE memory_summaries (id TEXT PRIMARY KEY, memory_space TEXT, summary_type TEXT, conversation_id TEXT, summary_text TEXT)")
            conn.executemany("INSERT INTO memory_summaries VALUES (?, 'whale', 'conversation_summary', 'c1', ?)", [("s2", "two"), ("s1", "one")])
            conn.commit()
        finally:
            conn.close()
        migrate_semantic_memory(self.sidecar, write=True)
        self.policy = patch("memory.lifecycle_orchestrator.DEFAULT_SEMANTIC_SIDECAR_PATH", self.sidecar)
        self.path_policy = patch("memory.db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH", self.sidecar)
        self.policy.start(); self.path_policy.start()
        self.backup_root = patch("memory.sidecar_maintenance.BACKUP_ROOT", root / "backups")
        self.backup_root.start()

    def tearDown(self):
        self.backup_root.stop(); self.path_policy.stop(); self.policy.stop(); self.holder.cleanup(); self.source_holder.cleanup()

    def plan(self):
        return build_lifecycle_execution_plan(self.source, self.sidecar, "whale", "deterministic_test")

    def executor(self, plan, **changes):
        values = {
            "confirmed_plan_schema_version": plan["plan_schema_version"],
            "confirmed_plan_id": plan["plan_id"],
            "confirmed_plan_items": plan["items"],
            "confirmed_provider_identity": plan["provider_identity"],
            "confirmed_target_kind": plan["target_kind"],
        }
        values.update(changes)
        return execute_memory_lifecycle(self.source, self.sidecar, "whale", "deterministic_test", dry_run=False, allow_production_sidecar=True, confirm_production_sidecar_write=True, **values)

    def test_complete_envelope_enters_execution_and_boolean_only_is_refused(self):
        plan = self.plan()
        self.assertTrue(self.executor(plan)["executed"])
        refused = execute_memory_lifecycle(self.source, self.sidecar, "whale", "deterministic_test", dry_run=False, allow_production_sidecar=True)
        self.assertEqual("refused", refused["status"])

    def test_each_confirmed_field_is_bound_to_plan_id(self):
        plan = self.plan()
        cases = []
        for field, value in (("action", "rebuild_embedding"), ("content_hash", "0" * 64)):
            items = copy.deepcopy(plan["items"]); items[0][field] = value
            cases.append({"confirmed_plan_items": items})
        for field, value in (("fingerprint", "tampered"), ("protocol", "tampered"), ("dim", 1), ("normalized", False)):
            identity = dict(plan["provider_identity"]); identity[field] = value
            cases.append({"confirmed_provider_identity": identity})
        cases.append({"confirmed_target_kind": "test_semantic_db"})
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual("stale_plan" if "confirmed_target_kind" not in changes else "refused", self.executor(plan, **changes)["status"])

    def test_plan_order_is_canonical(self):
        plan = self.plan()
        reversed_id = calculate_lifecycle_plan_id(plan["target_kind"], plan["provider_identity"], list(reversed(plan["items"])))
        self.assertEqual(plan["plan_id"], reversed_id)

    def test_action_changes_plan_id(self):
        plan = self.plan()
        items = copy.deepcopy(plan["items"])
        items[0]["action"] = "rebuild_embedding"
        changed = calculate_lifecycle_plan_id(plan["target_kind"], plan["provider_identity"], items)
        self.assertNotEqual(plan["plan_id"], changed)

    def test_missing_action_and_legacy_field_are_explicitly_refused(self):
        plan = self.plan()
        missing = copy.deepcopy(plan["items"])
        missing[0].pop("action")
        legacy = copy.deepcopy(plan["items"])
        legacy[0]["recommended_action"] = legacy[0].pop("action")
        self.assertEqual("refused", self.executor(plan, confirmed_plan_items=missing)["status"])
        self.assertEqual("refused", self.executor(plan, confirmed_plan_items=legacy)["status"])

    def test_executor_rechecks_content_hash_before_maintenance(self):
        plan = self.plan()
        changed = {"id": "s1", "memory_space": "whale", "summary_type": "conversation_summary", "conversation_id": "c1", "summary_text": "changed"}
        with patch("memory.sidecar_maintenance._load_summary", return_value=changed), patch("memory.sidecar_maintenance._maintain_sidecar_transaction") as maintenance:
            result = self.executor(plan)
        self.assertEqual("stale_plan", result["status"])
        maintenance.assert_not_called()

    def test_orchestrator_rejects_old_plan_and_source_hash_is_unchanged(self):
        plan = self.plan(); before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("UPDATE memory_summaries SET summary_text='changed' WHERE id='s1'")
            conn.commit()
        finally:
            conn.close()
        result = execute_lifecycle_plan(self.source, self.sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id=plan["plan_id"], allow_production_sidecar=True, confirm_production_sidecar_write=True)
        self.assertEqual("stale_plan", result["status"])
        self.assertNotEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())
        conn = sqlite3.connect(self.sidecar)
        try:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
