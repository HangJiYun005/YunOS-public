from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.db_path_policy import (
    KnowledgeSidecarPathError,
    validate_knowledge_test_db_target,
)
from memory.knowledge_sidecar import (
    PRODUCTION_KNOWLEDGE_SIDECAR,
    TestKnowledgeGraphRepository,
    initialize_test_knowledge_sidecar,
    production_knowledge_status,
)
from memory.memory_graph import build_memory_graph
from tests.test_memory_graph_identity import summary_record


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class KnowledgeSidecarTest(unittest.TestCase):
    def _build(self):
        return build_memory_graph([
            summary_record(source_table="memory_summaries", version=None)
        ], [])

    def test_initialization_and_repository_are_system_temp_only(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "knowledge.db"
            result = initialize_test_knowledge_sidecar(db)
            self.assertEqual("initialized", result["status"])
            self.assertEqual(db.resolve(), validate_knowledge_test_db_target(db))
            repository = TestKnowledgeGraphRepository(db)
            appended = repository.append_build(self._build(), generated_at="2026-01-01T00:00:00Z")
            self.assertEqual("appended", appended["status"])

            conn = sqlite3.connect(db)
            try:
                self.assertEqual(3, conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0])
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0])
                node_columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_nodes)")}
                edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_edges)")}
                self.assertNotIn("summary_text", node_columns)
                self.assertNotIn("embedding_json", edge_columns)
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("UPDATE graph_nodes SET memory_space = 'general'")
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute("DELETE FROM graph_edges")
            finally:
                conn.close()

            db.unlink()
            self.assertFalse(db.exists())

    def test_duplicate_append_rolls_back_and_releases_connection(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "knowledge.db"
            initialize_test_knowledge_sidecar(db)
            repository = TestKnowledgeGraphRepository(db)
            build = self._build()
            repository.append_build(build, generated_at="2026-01-01T00:00:00Z")
            with self.assertRaises(sqlite3.IntegrityError):
                repository.append_build(build, generated_at="2026-01-01T00:00:01Z")
            db.unlink()
            self.assertFalse(db.exists())

    def test_versioned_build_reuses_immutable_node_and_edge_identities(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "knowledge.db"
            initialize_test_knowledge_sidecar(db)
            repository = TestKnowledgeGraphRepository(db)
            record = summary_record(source_table="memory_summaries", version=None)
            repository.append_build(
                build_memory_graph([record], [], min_score=0.8),
                generated_at="2026-01-01T00:00:00Z",
            )
            repository.append_build(
                build_memory_graph([record], [], min_score=0.9),
                generated_at="2026-01-01T00:00:01Z",
            )
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(3, conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0])
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0])
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM graph_metadata").fetchone()[0])
            finally:
                conn.close()

    def test_conversation_payload_reuses_identity_across_memory_space_builds(self):
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "knowledge.db"
            initialize_test_knowledge_sidecar(db)
            repository = TestKnowledgeGraphRepository(db)
            whale = summary_record(memory_space="whale")
            general = summary_record(memory_space="general")
            repository.append_build(
                build_memory_graph([whale], []),
                generated_at="2026-01-01T00:00:00Z",
            )
            repository.append_build(
                build_memory_graph([general], []),
                generated_at="2026-01-01T00:00:01Z",
            )
            conn = sqlite3.connect(db)
            try:
                rows = conn.execute(
                    "SELECT node_id, memory_space FROM graph_nodes WHERE node_type = 'conversation'"
                ).fetchall()
                self.assertEqual(1, len(rows))
                self.assertIsNone(rows[0][1])
            finally:
                conn.close()

    def test_project_test_and_production_paths_are_rejected_for_initialization(self):
        with self.assertRaises(KnowledgeSidecarPathError):
            validate_knowledge_test_db_target(PROJECT_ROOT / "data" / "test" / "knowledge.db")
        with self.assertRaises(KnowledgeSidecarPathError):
            initialize_test_knowledge_sidecar(PRODUCTION_KNOWLEDGE_SIDECAR)

    def test_reparse_and_samefile_alias_checks_cannot_be_bypassed(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            linked_parent = root / "linked-parent"
            linked_parent.mkdir()
            with patch(
                "memory.db_path_policy.is_reparse_point",
                side_effect=lambda value: Path(value).name == "linked-parent",
            ):
                with self.assertRaises(KnowledgeSidecarPathError):
                    validate_knowledge_test_db_target(linked_parent / "knowledge.db")

            alias = root / "alias.db"
            alias.touch()
            with patch("memory.db_path_policy.os.path.samefile", return_value=True):
                with self.assertRaises(KnowledgeSidecarPathError):
                    validate_knowledge_test_db_target(alias)

    def test_symlink_target_is_rejected_when_supported(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            destination = root / "destination.db"
            link = root / "linked.db"
            destination.touch()
            try:
                os.symlink(destination, link)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(KnowledgeSidecarPathError):
                validate_knowledge_test_db_target(link)

    def test_hardlink_to_production_database_is_rejected_when_supported(self):
        real_db = PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db")
        if not real_db.is_file():
            self.skipTest("production database is unavailable")
        with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            hardlink = Path(tmp) / "production-alias.db"
            try:
                os.link(real_db, hardlink)
            except OSError as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")
            with self.assertRaises(KnowledgeSidecarPathError):
                validate_knowledge_test_db_target(hardlink)

    def test_production_status_check_does_not_create_sidecar(self):
        before = PRODUCTION_KNOWLEDGE_SIDECAR.exists()
        status = production_knowledge_status()
        after = PRODUCTION_KNOWLEDGE_SIDECAR.exists()
        self.assertEqual(before, after)
        self.assertTrue(status["readonly"])
        if not before:
            self.assertEqual("absent", status["status"])


if __name__ == "__main__":
    unittest.main()
