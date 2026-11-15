from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory.db_path_policy import TestDatabasePathError, validate_test_db_write_target
from memory.semantic_memory import build_embedding_record, save_embedding_record
from tools.migrate_semantic_memory import migrate_semantic_memory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_ROOT = PROJECT_ROOT / "data" / "test"
REAL_DB_PATH = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"


class TestDatabasePathPolicyTest(unittest.TestCase):
    def test_project_test_database_is_allowed(self):
        path = TEST_DB_ROOT / "valid.db"
        self.assertEqual(path.resolve(), validate_test_db_write_target(path))

    def test_safe_nested_project_test_database_is_allowed(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            path = Path(tmp) / "nested" / "valid.db"
            path.parent.mkdir()
            self.assertEqual(path.resolve(), validate_test_db_write_target(path))
            self.assertTrue(path.parent.exists())

    def test_external_test_named_paths_are_rejected(self):
        with TemporaryDirectory() as tmp:
            external = Path(tmp) / "test" / "production.db"
            with self.assertRaises(TestDatabasePathError):
                validate_test_db_write_target(external)

    def test_project_data_testing_database_is_rejected(self):
        with self.assertRaises(TestDatabasePathError):
            validate_test_db_write_target(PROJECT_ROOT / "data" / "testing.db")

    def test_real_database_and_external_relative_path_are_rejected(self):
        with self.assertRaises(TestDatabasePathError):
            validate_test_db_write_target(REAL_DB_PATH)
        with self.assertRaises(TestDatabasePathError):
            validate_test_db_write_target(Path("data") / "testing.db")

    def test_target_symlink_is_rejected(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp, TemporaryDirectory() as outside:
            target = Path(tmp) / "linked.db"
            try:
                os.symlink(Path(outside) / "outside.db", target)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(TestDatabasePathError):
                validate_test_db_write_target(target)

    def test_parent_symlink_is_rejected(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp, TemporaryDirectory() as outside:
            linked_parent = Path(tmp) / "linked-parent"
            try:
                os.symlink(outside, linked_parent, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink creation is unavailable: {exc}")
            with self.assertRaises(TestDatabasePathError):
                validate_test_db_write_target(linked_parent / "child.db")

    def test_reparse_point_detection_is_enforced_with_mock(self):
        path = TEST_DB_ROOT / "mock-reparse" / "target.db"
        with patch("memory.db_path_policy.is_reparse_point", side_effect=lambda value: Path(value).name == "mock-reparse"):
            with self.assertRaises(TestDatabasePathError):
                validate_test_db_write_target(path)

    def test_hardlink_to_real_database_is_rejected(self):
        if not REAL_DB_PATH.exists():
            self.skipTest("real database is unavailable")
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            hardlink = Path(tmp) / "real-alias.db"
            try:
                os.link(REAL_DB_PATH, hardlink)
            except OSError as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")
            with self.assertRaises(TestDatabasePathError):
                validate_test_db_write_target(hardlink)

    def test_save_and_migration_use_the_shared_policy(self):
        external_db = Path.cwd().parent / "test" / "production.db"
        record = build_embedding_record("test_notes", "n1", "boundary check")
        with self.assertRaises(TestDatabasePathError):
            migrate_semantic_memory(external_db, write=True)
        with self.assertRaises(TestDatabasePathError):
            save_embedding_record(record, external_db, dry_run=False)

    def test_migration_and_write_succeed_for_controlled_test_database(self):
        with TemporaryDirectory(dir=TEST_DB_ROOT) as tmp:
            db_path = Path(tmp) / "valid.db"
            migrated = migrate_semantic_memory(db_path, write=True)
            saved = save_embedding_record(
                build_embedding_record("test_notes", "n1", "controlled test database"),
                db_path,
                dry_run=False,
            )
            self.assertEqual("created", migrated["status"])
            self.assertEqual("inserted", saved["status"])


if __name__ == "__main__":
    unittest.main()
