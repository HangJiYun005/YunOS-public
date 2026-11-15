from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.lifecycle_planner import EMBEDDING_MISSING, plan_memory_lifecycle
from memory.memory_sidecar import initialize_memory_sidecar
from memory.refresh_workflow import run_memory_refresh
from memory.semantic_memory import SemanticRetrievalError, build_embedding_record, save_embedding_record
from tools.init_semantic_sidecar import CREATE_MEMORY_EMBEDDINGS_SQL


ROOT = Path(__file__).resolve().parents[1]
def source_fixture(path: Path, *, include_legacy: bool = False) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                memory_space TEXT
            );
            CREATE TABLE messages (
                message_id TEXT,
                conversation_id TEXT,
                role TEXT,
                text TEXT,
                create_time_iso TEXT,
                memory_space TEXT
            );
            CREATE TABLE memory_summaries (
                id TEXT,
                conversation_id TEXT,
                memory_space TEXT,
                summary_type TEXT,
                summary_text TEXT,
                source_content_hash TEXT,
                created_at TEXT
            );
            """
        )
        conn.execute("INSERT INTO conversations VALUES ('c1', 'whale')")
        conn.executemany(
            "INSERT INTO messages VALUES (?, 'c1', ?, ?, ?, 'whale')",
            [
                ("m1", "user", "controlled memory refresh whale", "2026-01-01T00:00:00Z"),
                ("m2", "assistant", "semantic sidecar recovery", "2026-01-01T00:01:00Z"),
            ],
        )
        if include_legacy:
            conn.execute(
                "INSERT INTO memory_summaries VALUES "
                "('collision', 'c1', 'whale', 'conversation_summary', 'legacy fallback', NULL, '2025')"
            )
        conn.commit()
    finally:
        conn.close()


class MemoryRefreshWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.holder = TemporaryDirectory()
        self.source_root = Path(self.holder.name)
        self.source = self.source_root / "source.db"
        self.memory = self.source_root / "memory.db"
        self.semantic = self.source_root / "semantic.db"
        self.backups = self.source_root / "backups"
        source_fixture(self.source)
        initialize_memory_sidecar(self.memory, execute=True)
        conn = sqlite3.connect(self.semantic)
        try:
            conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
            conn.commit()
        finally:
            conn.close()
        self.policy_patches = [
            patch("memory.lifecycle_orchestrator.validate_test_db_write_target", side_effect=self._test_semantic_target),
            patch("memory.lifecycle_executor.validate_test_db_write_target", side_effect=self._test_semantic_target),
            patch("memory.semantic_memory.validate_test_db_write_target", side_effect=self._test_semantic_target),
        ]
        for policy in self.policy_patches:
            policy.start()
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        for policy in reversed(self.policy_patches):
            policy.stop()
        root = self.source_root
        self.holder.cleanup()
        self.assertFalse(root.exists(), "system temporary databases must be immediately deletable")

    def _test_semantic_target(self, candidate, *args, **kwargs):
        resolved = Path(candidate).expanduser().resolve()
        if resolved != self.semantic.resolve():
            raise ValueError("unexpected test semantic target")
        return resolved

    def phase_run(self, phase: str, **kwargs):
        return run_memory_refresh(
            self.source,
            self.memory,
            self.semantic,
            "c1",
            memory_space="whale",
            provider_name="deterministic_test",
            phase=phase,
            backup_dir=self.backups,
            **kwargs,
        )

    def accept_v1(self):
        prepared = self.phase_run("prepare-summary")
        accepted = self.phase_run(
            "execute-summary",
            confirm_candidate_id=prepared["candidate_id"],
            confirm_acceptance_plan_id=prepared["acceptance_plan_id"],
        )
        return prepared, accepted

    def prepare_semantic(self):
        return self.phase_run("prepare-semantic")

    def complete(self):
        prepared, accepted = self.accept_v1()
        semantic = self.prepare_semantic()
        executed = self.phase_run(
            "execute-semantic",
            confirm_semantic_plan_id=semantic["semantic_plan_id"],
        )
        return prepared, accepted, semantic, executed

    def summary_count(self) -> int:
        conn = sqlite3.connect(self.memory)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM summary_versions").fetchone()[0])
        finally:
            conn.close()

    def embedding_count(self) -> int:
        conn = sqlite3.connect(self.semantic)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings WHERE source_table = 'summary_versions'"
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def file_hash(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def embedding_metadata(self) -> str:
        conn = sqlite3.connect(self.semantic)
        try:
            return str(
                conn.execute(
                    "SELECT metadata_json FROM memory_embeddings "
                    "WHERE source_table = 'summary_versions'"
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def set_embedding_metadata(self, value: str) -> None:
        conn = sqlite3.connect(self.semantic)
        try:
            conn.execute(
                "UPDATE memory_embeddings SET metadata_json = ? "
                "WHERE source_table = 'summary_versions'",
                (value,),
            )
            conn.commit()
        finally:
            conn.close()

    def completion_views(self):
        return {
            "status": self.phase_run("status"),
            "prepare-semantic": self.phase_run("prepare-semantic"),
            "verify": self.phase_run("verify"),
        }

    def test_end_to_end_reaches_both_up_to_date_states_and_retrieval(self):
        prepared, accepted, semantic, executed = self.complete()
        self.assertEqual("summary_acceptance_required", prepared["status"])
        self.assertEqual(("summary_accepted_index_pending", 1), (accepted["status"], accepted["accepted_summary_version"]))
        self.assertEqual("semantic_plan_ready", semantic["status"])
        self.assertEqual("completed", executed["status"])
        self.assertTrue(all(executed["verification"].values()))
        self.assertEqual("completed", self.phase_run("status")["status"])
        self.assertEqual(self.source_hash, hashlib.sha256(self.source.read_bytes()).hexdigest())
        metadata = json.loads(self.embedding_metadata())
        self.assertTrue({
            "summary_source", "summary_id", "conversation_id", "summary_version",
            "summary_text_hash", "source_snapshot_hash", "memory_space",
            "semantic_target_kind", "summary_type",
        }.issubset(metadata))
        self.assertEqual("memory_sidecar", metadata["summary_source"])
        self.assertEqual("summary_embedding", metadata["semantic_target_kind"])
        self.assertEqual("conversation_summary", metadata["summary_type"])

    def test_all_public_completion_views_share_full_success_verification(self):
        _, _, _, executed = self.complete()
        views = {"execute-semantic": executed, **self.completion_views()}
        for name, result in views.items():
            with self.subTest(name=name):
                self.assertEqual(("completed", "none"), (result["status"], result["next_action"]))
                self.assertTrue(all(result["verification"].values()))
                self.assertTrue(result["summary_committed"])
                self.assertTrue(result["semantic_committed"])

    def test_metadata_missing_damaged_or_misbound_blocks_every_completion_view_without_writes(self):
        self.complete()
        original = json.loads(self.embedding_metadata())
        bad_metadata = {
            "missing": "{}",
            "damaged": "{not-json",
            "wrong-version-hash": json.dumps(
                {**original, "summary_version": 999, "summary_text_hash": "0" * 64},
                sort_keys=True,
            ),
        }
        for case, value in bad_metadata.items():
            with self.subTest(case=case):
                self.set_embedding_metadata(value)
                before = {
                    "source": self.file_hash(self.source),
                    "memory": self.file_hash(self.memory),
                    "semantic": self.file_hash(self.semantic),
                    "backups": sorted(path.name for path in self.backups.glob("*.db")),
                }
                views = self.completion_views()
                for name, result in views.items():
                    with self.subTest(view=name):
                        self.assertEqual(("manual_review", "repair_semantic_metadata"), (result["status"], result["next_action"]))
                        self.assertTrue(result["verification"]["semantic_lifecycle_up_to_date"])
                        self.assertFalse(result["verification"]["semantic_binding_matches"])
                        self.assertFalse(result["verification"]["semantic_retrieval_hit"])
                        self.assertTrue(result["summary_committed"])
                        self.assertFalse(result["semantic_committed"])
                        encoded = json.dumps(result, ensure_ascii=False)
                        self.assertNotIn("controlled memory refresh whale", encoded)
                        self.assertNotIn("semantic sidecar recovery", encoded)
                        self.assertNotIn(str(self.source_root), encoded)
                        self.assertNotIn("embedding_json", encoded)
                after = {
                    "source": self.file_hash(self.source),
                    "memory": self.file_hash(self.memory),
                    "semantic": self.file_hash(self.semantic),
                    "backups": sorted(path.name for path in self.backups.glob("*.db")),
                }
                self.assertEqual(before, after)
                self.assertFalse(list(self.source_root.glob("*.log")))

    def test_retrieval_miss_blocks_status_prepare_and_verify_without_writes(self):
        self.complete()
        before = {
            "source": self.file_hash(self.source),
            "memory": self.file_hash(self.memory),
            "semantic": self.file_hash(self.semantic),
            "backups": sorted(path.name for path in self.backups.glob("*.db")),
        }
        with patch("memory.refresh_workflow.semantic_summary_search_preview", return_value={"results": []}):
            views = self.completion_views()
        for name, result in views.items():
            with self.subTest(name=name):
                self.assertEqual(("manual_review", "inspect_semantic_retrieval"), (result["status"], result["next_action"]))
                self.assertTrue(result["verification"]["semantic_binding_matches"])
                self.assertFalse(result["verification"]["semantic_retrieval_hit"])
                self.assertFalse(result["semantic_committed"])
        after = {
            "source": self.file_hash(self.source),
            "memory": self.file_hash(self.memory),
            "semantic": self.file_hash(self.semantic),
            "backups": sorted(path.name for path in self.backups.glob("*.db")),
        }
        self.assertEqual(before, after)
        self.assertFalse(list(self.source_root.glob("*.log")))

    def test_retrieval_error_is_a_consistent_manual_review_state(self):
        self.complete()
        with patch(
            "memory.refresh_workflow.semantic_summary_search_preview",
            side_effect=SemanticRetrievalError("fixture retrieval failure"),
        ):
            views = self.completion_views()
        for name, result in views.items():
            with self.subTest(name=name):
                self.assertEqual(
                    ("manual_review", "inspect_semantic_retrieval"),
                    (result["status"], result["next_action"]),
                )
                self.assertFalse(result["semantic_committed"])

    def test_incompatible_provider_identity_never_completes(self):
        self.complete()
        conn = sqlite3.connect(self.semantic)
        try:
            conn.execute(
                "UPDATE memory_embeddings SET model_fingerprint = 'incompatible' "
                "WHERE source_table = 'summary_versions'"
            )
            conn.commit()
        finally:
            conn.close()
        status = self.phase_run("status")
        prepared = self.phase_run("prepare-semantic")
        verified = self.phase_run("verify")
        self.assertEqual(("summary_accepted_index_pending", "prepare_semantic_maintenance"), (status["status"], status["next_action"]))
        self.assertEqual(("semantic_plan_ready", "execute_semantic_maintenance"), (prepared["status"], prepared["next_action"]))
        self.assertEqual((status["status"], status["next_action"]), (verified["status"], verified["next_action"]))
        self.assertNotEqual("completed", prepared["status"])

    def test_full_rerun_is_idempotent_and_does_not_duplicate_versions_or_embeddings(self):
        prepared, _, _, _ = self.complete()
        repeated = self.phase_run(
            "execute-summary",
            confirm_candidate_id=prepared["candidate_id"],
            confirm_acceptance_plan_id=prepared["acceptance_plan_id"],
        )
        self.assertEqual("summary_accepted_index_pending", repeated["status"])
        self.assertEqual("completed", self.prepare_semantic()["status"])
        self.assertEqual((1, 1), (self.summary_count(), self.embedding_count()))

    def test_accepted_but_unindexed_state_recovers_after_failure_without_new_summary(self):
        self.accept_v1()
        pending = self.phase_run("status")
        self.assertEqual("summary_accepted_index_pending", pending["status"])
        plan = self.prepare_semantic()
        with patch("memory.lifecycle_executor.save_embedding_record", side_effect=RuntimeError("fixture failure")):
            failed = self.phase_run("execute-semantic", confirm_semantic_plan_id=plan["semantic_plan_id"])
        self.assertEqual(("failed", "retry_semantic_maintenance"), (failed["status"], failed["next_action"]))
        self.assertEqual((1, 0), (self.summary_count(), self.embedding_count()))
        retried = self.phase_run("execute-semantic", confirm_semantic_plan_id=plan["semantic_plan_id"])
        self.assertEqual("completed", retried["status"])
        self.assertEqual((1, 1), (self.summary_count(), self.embedding_count()))

    def test_new_accepted_version_makes_old_semantic_plan_stale(self):
        self.accept_v1()
        old_plan = self.prepare_semantic()
        conn = sqlite3.connect(self.source)
        try:
            conn.execute(
                "INSERT INTO messages VALUES "
                "('m3', 'c1', 'user', 'new source message', '2026-01-01T00:02:00Z', 'whale')"
            )
            conn.commit()
        finally:
            conn.close()
        prepared_v2 = self.phase_run("prepare-summary")
        accepted_v2 = self.phase_run(
            "execute-summary",
            confirm_candidate_id=prepared_v2["candidate_id"],
            confirm_acceptance_plan_id=prepared_v2["acceptance_plan_id"],
        )
        self.assertEqual(2, accepted_v2["accepted_summary_version"])
        status = self.phase_run("status")
        verified = self.phase_run("verify")
        self.assertEqual(("summary_accepted_index_pending", "prepare_semantic_maintenance"), (status["status"], status["next_action"]))
        self.assertEqual((status["status"], status["next_action"]), (verified["status"], verified["next_action"]))
        stale = self.phase_run("execute-semantic", confirm_semantic_plan_id=old_plan["semantic_plan_id"])
        self.assertEqual("stale_plan", stale["status"])
        new_plan = self.prepare_semantic()
        self.assertNotEqual(old_plan["semantic_plan_id"], new_plan["semantic_plan_id"])
        self.assertEqual(2, self.summary_count())

    def test_confirmation_envelopes_are_independent_and_missing_values_refuse(self):
        prepared = self.phase_run("prepare-summary")
        missing = self.phase_run("execute-summary")
        wrong_summary_envelope = self.phase_run(
            "execute-summary",
            confirm_candidate_id=prepared["candidate_id"],
            confirm_acceptance_plan_id="0" * 64,
        )
        self.assertEqual("refused", missing["status"])
        self.assertEqual("stale_plan", wrong_summary_envelope["status"])
        self.accept_v1()
        semantic = self.prepare_semantic()
        missing_semantic = self.phase_run("execute-semantic")
        acceptance_as_semantic = self.phase_run(
            "execute-semantic",
            confirm_semantic_plan_id=prepared["acceptance_plan_id"],
        )
        self.assertEqual("refused", missing_semantic["status"])
        self.assertEqual("stale_plan", acceptance_as_semantic["status"])
        self.assertEqual(0, self.embedding_count())
        self.assertNotEqual(prepared["acceptance_plan_id"], semantic["semantic_plan_id"])

    def test_legacy_fallback_and_cross_source_id_collision_are_unambiguous(self):
        legacy_source = self.source_root / "legacy.db"
        empty_memory = self.source_root / "empty-memory.db"
        source_fixture(legacy_source, include_legacy=True)
        initialize_memory_sidecar(empty_memory, execute=True)
        legacy = plan_memory_lifecycle(
            legacy_source, self.semantic, "whale", "deterministic_test",
            empty_memory, "c1",
        )
        self.assertEqual(("legacy", "memory_summaries"), (legacy["items"][0]["summary_source"], legacy["items"][0]["source_table"]))

        self.accept_v1()
        conn = sqlite3.connect(self.memory)
        try:
            conn.execute("UPDATE summary_versions SET summary_id = 'collision'")
            accepted_text = conn.execute("SELECT summary_text FROM summary_versions").fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        legacy_collision = build_embedding_record(
            "memory_summaries", "collision", accepted_text,
            memory_space="whale", provider_name="deterministic_test",
        )
        save_embedding_record(legacy_collision, self.semantic, dry_run=False)
        unified = plan_memory_lifecycle(
            self.source, self.semantic, "whale", "deterministic_test",
            self.memory, "c1",
        )
        self.assertEqual(("memory_sidecar", "summary_versions", EMBEDDING_MISSING), (
            unified["items"][0]["summary_source"],
            unified["items"][0]["source_table"],
            unified["items"][0]["status"],
        ))

    def test_default_output_is_text_free_path_free_and_explicit_preview_is_opt_in(self):
        prepared = self.phase_run("prepare-summary")
        encoded = json.dumps(prepared, ensure_ascii=False)
        self.assertNotIn("controlled memory refresh whale", encoded)
        self.assertNotIn("semantic sidecar recovery", encoded)
        self.assertNotIn(str(self.source_root), encoded)
        self.assertNotIn("embedding_json", encoded)
        preview = self.phase_run("prepare-summary", show_candidate_text=True)
        self.assertIn("candidate_preview", preview)
        self.assertTrue(preview["candidate_preview"])

    def test_cli_defaults_to_readonly_text_free_output(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "run_memory_refresh.py"),
                "--phase", "prepare-summary",
                "--source-db", str(self.source),
                "--memory-sidecar", str(self.memory),
                "--semantic-db", str(self.semantic),
                "--conversation-id", "c1",
                "--memory-space", "whale",
                "--provider", "deterministic_test",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("summary_acceptance_required", payload["status"])
        self.assertNotIn("candidate_preview", payload)
        self.assertNotIn("controlled memory refresh whale", result.stdout)
        self.assertNotIn(str(self.source_root), result.stdout)


if __name__ == "__main__":
    unittest.main()
