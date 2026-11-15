from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_migration_rehearsal import persist_synthetic_integration
from memory.ingestion_migrations import (
    INGESTION_TABLES,
    MARKER_TABLES,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
    REQUIRED_TABLES,
    apply_migration_to_clone,
    build_migration_plan,
    inspect_schema,
    verify_migrated_schema,
)
from tests.v83_migration_fixtures import create_legacy_database, migrate_database


class SchemaMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83f-schema-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_empty_legacy_database_migrates_additively(self) -> None:
        database = create_legacy_database(self.root / "empty.db", with_rows=False)
        before = inspect_schema(database)
        plan = build_migration_plan(before)
        result = apply_migration_to_clone(
            plan, database, rehearsal_id="rehearsal_empty_001",
        )
        after = inspect_schema(database)
        self.assertEqual("completed", result.status)
        self.assertEqual(plan.expected_target_schema_fingerprint, after.schema_fingerprint)
        self.assertTrue(set(REQUIRED_TABLES).issubset(after.object_names))
        self.assertTrue(set(before.object_names).issubset(after.object_names))
        self.assertEqual(0, sum(after.row_counts.values()))

    def test_all_tables_indexes_triggers_and_foreign_keys_exist(self) -> None:
        database = create_legacy_database(self.root / "objects.db")
        plan = migrate_database(database)
        snapshot = inspect_schema(database)
        self.assertTrue(set(MARKER_TABLES + INGESTION_TABLES).issubset(snapshot.object_names))
        self.assertEqual(plan.expected_target_schema_fingerprint, snapshot.schema_fingerprint)
        with closing(sqlite3.connect(database)) as conn:
            index_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_schema "
                "WHERE type='index' AND name LIKE 'ingestion_%'"
            ).fetchone()[0]
            trigger_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_schema "
                "WHERE type='trigger' AND name LIKE '%_no_%'"
            ).fetchone()[0]
            foreign_keys = sum(
                len(list(conn.execute(f'PRAGMA foreign_key_list("{table}")')))
                for table in INGESTION_TABLES
            )
        self.assertEqual(plan.index_count, index_count)
        self.assertEqual(plan.trigger_count, trigger_count)
        self.assertGreaterEqual(foreign_keys, 12)

    def test_marker_binds_version_digest_and_fingerprints(self) -> None:
        database = create_legacy_database(self.root / "marker.db")
        before = inspect_schema(database)
        plan = build_migration_plan(before)
        result = apply_migration_to_clone(
            plan, database, rehearsal_id="rehearsal_marker_001",
        )
        with closing(sqlite3.connect(database)) as conn:
            row = conn.execute(
                "SELECT * FROM schema_migrations WHERE migration_id=?",
                (result.migration_id,),
            ).fetchone()
            meta = conn.execute(
                "SELECT meta_value,value_digest FROM yunos_schema_meta "
                "WHERE meta_key='ingestion_schema'"
            ).fetchone()
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(MIGRATION_VERSION, row[1])
        self.assertEqual(MIGRATION_DIGEST, row[3])
        self.assertEqual(before.schema_fingerprint, row[6])
        self.assertEqual(result.after_schema_fingerprint, row[7])
        self.assertIsNotNone(meta)
        self.assertEqual(MIGRATION_VERSION, user_version)

    def test_new_ingestion_tables_are_empty_after_migration(self) -> None:
        database = create_legacy_database(self.root / "empty-new.db")
        plan = migrate_database(database)
        verification = verify_migrated_schema(
            database, expected_fingerprint=plan.expected_target_schema_fingerprint,
        )
        self.assertEqual("ok", verification["integrity_check"])
        self.assertEqual(0, verification["foreign_key_violation_count"])
        self.assertTrue(all(
            count == 0 for count in verification["new_table_row_counts"].values()
        ))

    def test_append_only_and_unique_constraints_fail_closed(self) -> None:
        database = create_legacy_database(self.root / "constraints.db")
        migrate_database(database)
        self.assertTrue(persist_synthetic_integration(database)["passed"])
        with closing(sqlite3.connect(database)) as conn:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                conn.execute(
                    "UPDATE ingestion_batch_events SET reason_code='changed'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append_only"):
                conn.execute("DELETE FROM ingestion_execution_events")
            row = conn.execute(
                "SELECT * FROM ingestion_source_bindings LIMIT 1"
            ).fetchone()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ingestion_source_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row),
                )

    def test_json_and_storage_key_checks_reject_invalid_values(self) -> None:
        database = create_legacy_database(self.root / "checks.db")
        migrate_database(database)
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ingestion_conflicts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "conflict_invalid_json", "missing-batch", "test", "low",
                        "a" * 64, "{", "stop", 1, "open", "fixture", "now",
                    ),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ingestion_asset_entities VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "b" * 64, "image/png", 1, "verified",
                        "C:\\private\\asset.png", "c" * 64, "missing-batch",
                        "fixture", "now",
                    ),
                )


if __name__ == "__main__":
    unittest.main()
