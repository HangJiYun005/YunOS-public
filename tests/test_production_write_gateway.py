from __future__ import annotations

import copy
import hashlib
import inspect
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory import sidecar_maintenance
from memory.db_path_policy import SemanticSidecarPathError
from memory.lifecycle_orchestrator import build_lifecycle_execution_plan
from memory.semantic_memory import build_embedding_record, save_embedding_record
from memory.sidecar_maintenance import execute_confirmed_sidecar_maintenance
from tools.init_semantic_sidecar import CREATE_MEMORY_EMBEDDINGS_SQL


ROOT = Path(__file__).resolve().parents[1]


class ProductionWriteGatewayTest(unittest.TestCase):
    def setUp(self):
        self.holder = TemporaryDirectory(dir=ROOT / "data" / "test")
        root = Path(self.holder.name)
        self.source = root / "source.db"
        self.sidecar = root / "sidecar.db"
        self.test_db = root / "test.db"
        self.backups = root / "backups"
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("CREATE TABLE memory_summaries (id TEXT PRIMARY KEY, memory_space TEXT, summary_type TEXT, conversation_id TEXT, summary_text TEXT)")
            conn.execute("INSERT INTO memory_summaries VALUES ('s1', 'whale', 'conversation_summary', 'c1', 'gateway summary')")
            conn.commit()
        finally:
            conn.close()
        for path in (self.sidecar, self.test_db):
            conn = sqlite3.connect(path)
            try:
                conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
                conn.commit()
            finally:
                conn.close()
        self.patches = [
            patch("memory.db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH", self.sidecar),
            patch("memory.lifecycle_orchestrator.DEFAULT_SEMANTIC_SIDECAR_PATH", self.sidecar),
            patch("memory.sidecar_maintenance.BACKUP_ROOT", self.backups),
        ]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.holder.cleanup()

    def plan(self):
        return build_lifecycle_execution_plan(self.source, self.sidecar, "whale", "deterministic_test")

    def execute(self, plan, **changes):
        values = {
            "confirmed_plan_schema_version": plan["plan_schema_version"],
            "confirmed_plan_id": plan["plan_id"],
            "confirmed_plan_items": plan["items"],
            "confirmed_provider_identity": plan["provider_identity"],
            "confirmed_target_kind": plan["target_kind"],
            "allow_production_sidecar": True,
            "confirm_production_sidecar_write": True,
        }
        values.update(changes)
        return execute_confirmed_sidecar_maintenance(self.source, self.sidecar, "whale", "deterministic_test", **values)

    def test_semantic_record_writer_always_rejects_production_sidecar(self):
        record = build_embedding_record("memory_summaries", "s1", "gateway summary")
        for allow in (False, True):
            with self.subTest(allow=allow), self.assertRaises(SemanticSidecarPathError):
                save_embedding_record(record, self.sidecar, dry_run=False, allow_production_sidecar=allow)

    def test_test_database_record_write_remains_supported(self):
        record = build_embedding_record("memory_summaries", "s1", "test")
        self.assertEqual("inserted", save_embedding_record(record, self.test_db, dry_run=False)["status"])

    def test_raw_records_api_is_not_public_or_accepted(self):
        self.assertFalse(hasattr(sidecar_maintenance, "maintain_sidecar"))
        self.assertNotIn("records", inspect.signature(execute_confirmed_sidecar_maintenance).parameters)
        with self.assertRaises(TypeError):
            execute_confirmed_sidecar_maintenance(self.source, self.sidecar, records=[])

    def test_missing_or_tampered_envelope_is_rejected_without_artifacts(self):
        plan = self.plan()
        before_hash = hashlib.sha256(self.sidecar.read_bytes()).hexdigest()
        cases = [
            {"confirmed_plan_id": None},
            {"confirmed_plan_id": "fake"},
            {"confirmed_target_kind": "test_semantic_db"},
            {"confirmed_provider_identity": {**plan["provider_identity"], "fingerprint": "fake"}},
            {"confirmed_plan_items": [{**plan["items"][0], "content_hash": "0" * 64}]},
            {"confirm_production_sidecar_write": False},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.execute(plan, **changes)
                self.assertIn(result["status"], {"refused", "stale_plan"})
                self.assertFalse(result["backup_created"])
        self.assertEqual(before_hash, hashlib.sha256(self.sidecar.read_bytes()).hexdigest())
        self.assertFalse(self.backups.exists())

    def test_source_change_is_stale_and_valid_envelope_commits(self):
        plan = self.plan()
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("UPDATE memory_summaries SET summary_text='changed' WHERE id='s1'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual("stale_plan", self.execute(plan)["status"])
        current = self.plan()
        self.assertEqual("committed", self.execute(current)["status"])

    def test_real_chat_database_hash_is_unchanged(self):
        real = ROOT / "data" / ("ayunos_" + "chatgpt.db")
        if not real.exists(): self.skipTest("real database unavailable")
        before = hashlib.sha256(real.read_bytes()).hexdigest()
        self.execute(self.plan())
        self.assertEqual(before, hashlib.sha256(real.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
