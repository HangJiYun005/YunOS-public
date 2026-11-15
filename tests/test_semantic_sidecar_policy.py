from __future__ import annotations

import hashlib
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.db_path_policy import SemanticSidecarPathError, validate_semantic_sidecar_write_target
from memory.lifecycle_orchestrator import build_lifecycle_execution_plan, execute_lifecycle_plan
from memory.semantic_memory import REQUIRED_EMBEDDING_COLUMNS
from tools import init_semantic_sidecar


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SemanticSidecarPolicyTest(unittest.TestCase):
    def _paths(self):
        holder = TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(holder.name)
        semantic = root / "data" / "semantic"
        semantic.mkdir(parents=True)
        return holder, root / "data" / "ayunos_chatgpt.db", semantic / "ayunos_semantic.db", semantic / "other.db"

    def _policy(self, sidecar):
        return patch("memory.db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH", sidecar)

    def test_only_exact_default_sidecar_is_accepted(self):
        holder, real, sidecar, other = self._paths()
        try:
            with self._policy(sidecar):
                self.assertEqual(sidecar, validate_semantic_sidecar_write_target(sidecar, real_db_path=real))
                for rejected in (other, holder.name, real):
                    with self.assertRaises(SemanticSidecarPathError):
                        validate_semantic_sidecar_write_target(rejected, real_db_path=real)
        finally:
            holder.cleanup()

    def test_hardlink_and_reparse_targets_are_rejected(self):
        holder, real, sidecar, _ = self._paths()
        try:
            real.write_bytes(b"real")
            with self._policy(sidecar):
                try:
                    os.link(real, sidecar)
                except OSError as exc:
                    self.skipTest(f"hardlink creation is unavailable: {exc}")
                with self.assertRaises(SemanticSidecarPathError):
                    validate_semantic_sidecar_write_target(sidecar, real_db_path=real)
            with patch("memory.db_path_policy.is_reparse_point", side_effect=lambda value: Path(value) == sidecar.parent):
                with self._policy(sidecar), self.assertRaises(SemanticSidecarPathError):
                    validate_semantic_sidecar_write_target(sidecar, real_db_path=real)
        finally:
            holder.cleanup()

    def test_initializer_requires_all_confirmations_and_preserves_schema(self):
        holder, _, sidecar, _ = self._paths()
        try:
            with patch("tools.init_semantic_sidecar.PROJECT_ROOT", sidecar.parents[2]), self._policy(sidecar):
                self.assertEqual("dry_run", init_semantic_sidecar.init_semantic_sidecar()["status"])
                self.assertEqual("refused", init_semantic_sidecar.init_semantic_sidecar(True, True, False)["status"])
                self.assertEqual("created", init_semantic_sidecar.init_semantic_sidecar(True, True, True)["status"])
                self.assertEqual("already_ready", init_semantic_sidecar.init_semantic_sidecar(True, True, True)["status"])
                conn = sqlite3.connect(sidecar)
                try:
                    self.assertTrue(REQUIRED_EMBEDDING_COLUMNS.issubset({row[1] for row in conn.execute("PRAGMA table_info(memory_embeddings)")}))
                finally:
                    conn.close()
                sidecar.unlink()
                with sqlite3.connect(sidecar) as conn:
                    conn.execute("CREATE TABLE memory_embeddings (id INTEGER)")
                self.assertEqual("refused", init_semantic_sidecar.init_semantic_sidecar(True, True, True)["status"])
        finally:
            holder.cleanup()

    def test_orchestrator_sidecar_requires_confirmation_and_keeps_source_immutable(self):
        holder, real, sidecar, _ = self._paths()
        source = sidecar.parents[1] / "source.db"
        try:
            with sqlite3.connect(source) as conn:
                conn.execute("CREATE TABLE memory_summaries (id TEXT PRIMARY KEY, memory_space TEXT, summary_type TEXT, conversation_id TEXT, summary_text TEXT)")
                conn.execute("INSERT INTO memory_summaries VALUES ('s1', 'whale', 'conversation_summary', 'c1', 'sidecar summary')")
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            with self._policy(sidecar), patch("memory.lifecycle_orchestrator.DEFAULT_SEMANTIC_SIDECAR_PATH", sidecar), patch("tools.init_semantic_sidecar.PROJECT_ROOT", sidecar.parents[2]), patch("memory.sidecar_maintenance.BACKUP_ROOT", sidecar.parents[2] / "backups" / "semantic"):
                init_semantic_sidecar.init_semantic_sidecar(True, True, True)
                plan = build_lifecycle_execution_plan(source, sidecar, "whale", "deterministic_test")
                refused = execute_lifecycle_plan(source, sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id=plan["plan_id"])
                self.assertEqual("refused", refused["status"])
                done = execute_lifecycle_plan(source, sidecar, "whale", "deterministic_test", execute=True, confirm_plan_id=plan["plan_id"], allow_production_sidecar=True, confirm_production_sidecar_write=True)
                self.assertEqual("committed", done["status"])
                self.assertEqual({"up_to_date": 1}, done["after_status_counts"])
                again = build_lifecycle_execution_plan(source, sidecar, "whale", "deterministic_test")
                self.assertEqual(0, again["action_count"])
            self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
        finally:
            holder.cleanup()

    def test_sidecar_artifacts_are_ignored_and_no_forbidden_integrations_exist(self):
        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/semantic/*.db", ignored)
        text = (PROJECT_ROOT / "memory" / "lifecycle_orchestrator.py").read_text(encoding="utf-8")
        for forbidden in ("whale_chat", "FROM messages", "requests", "openai"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
