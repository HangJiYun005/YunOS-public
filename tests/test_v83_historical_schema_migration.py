from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_migration_rehearsal import (
    capture_legacy_compatibility,
    run_compatibility_suite,
)
from memory.ingestion_migrations import (
    HISTORICAL_INGESTION_TABLES,
    HISTORICAL_MIGRATION_DDL,
    HISTORICAL_MIGRATION_DIGEST,
    HISTORICAL_MIGRATION_INDEX_DDL,
    HISTORICAL_MIGRATION_TRIGGER_DDL,
    HISTORICAL_MIGRATION_VERSION,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
    apply_historical_migration_to_clone,
    build_historical_migration_plan,
    inspect_schema,
    verify_historical_migrated_schema,
)
from tests.v83_historical_schema_fixtures import (
    create_v8301_database,
    insert_unified_chains,
    migrate_v8302,
)


class UnifiedV8302MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83g0-unified-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_v8302_applies_after_v8301(self) -> None:
        database = create_v8301_database(self.root / "ordered.db")
        before = inspect_schema(database)
        plan = build_historical_migration_plan(before)
        result = apply_historical_migration_to_clone(
            plan,
            database,
            rehearsal_id="rehearsal_g0_unified_order_001",
        )
        self.assertEqual("completed", result.status)
        self.assertEqual(HISTORICAL_MIGRATION_VERSION, result.migration_version)
        self.assertEqual(before.schema_fingerprint, result.v8301_schema_fingerprint)

    def test_v8302_has_expected_unified_objects(self) -> None:
        database = create_v8301_database(self.root / "objects.db")
        plan = migrate_v8302(database)
        snapshot = inspect_schema(database)
        self.assertTrue(set(HISTORICAL_INGESTION_TABLES).issubset(
            snapshot.object_names
        ))
        self.assertEqual(13, plan.table_count)
        self.assertEqual(13, plan.index_count)
        self.assertEqual(31, plan.trigger_count)
        self.assertEqual(13, len(HISTORICAL_MIGRATION_INDEX_DDL))
        self.assertEqual(31, len(HISTORICAL_MIGRATION_TRIGGER_DDL))

    def test_forbidden_historical_authority_tables_do_not_exist(self) -> None:
        database = create_v8301_database(self.root / "forbidden.db")
        migrate_v8302(database)
        forbidden = {
            "ingestion_historical_batches",
            "ingestion_historical_batch_events",
            "ingestion_historical_conflicts",
            "ingestion_historical_decisions",
            "ingestion_historical_approvals",
            "ingestion_historical_executions",
            "ingestion_historical_execution_events",
            "ingestion_historical_verifications",
        }
        self.assertTrue(forbidden.isdisjoint(inspect_schema(database).object_names))

    def test_marker_chains_v8301_and_v8302(self) -> None:
        database = create_v8301_database(self.root / "marker.db")
        plan = migrate_v8302(database)
        verification = verify_historical_migrated_schema(
            database,
            expected_fingerprint=plan.expected_target_schema_fingerprint,
            require_empty=True,
        )
        self.assertEqual(
            [MIGRATION_VERSION, HISTORICAL_MIGRATION_VERSION],
            verification["migration_versions"],
        )
        self.assertTrue(verification["marker_chain_valid"])
        self.assertTrue(verification["schema_meta_valid"])

    def test_new_tables_start_empty_and_database_is_sound(self) -> None:
        database = create_v8301_database(self.root / "empty.db")
        migrate_v8302(database)
        verification = verify_historical_migrated_schema(
            database,
            require_empty=True,
        )
        self.assertEqual("ok", verification["integrity_check"])
        self.assertEqual(0, verification["foreign_key_violation_count"])
        self.assertTrue(all(
            count == 0
            for count in verification["historical_table_row_counts"].values()
        ))

    def test_exact_replay_is_already_applied(self) -> None:
        database = create_v8301_database(self.root / "replay.db")
        plan = build_historical_migration_plan(inspect_schema(database))
        first = apply_historical_migration_to_clone(
            plan,
            database,
            rehearsal_id="rehearsal_g0_unified_replay_001",
        )
        second = apply_historical_migration_to_clone(
            plan,
            database,
            rehearsal_id="rehearsal_g0_unified_replay_002",
        )
        self.assertEqual("completed", first.status)
        self.assertEqual("already_applied", second.status)

    def test_replanned_completed_schema_is_idempotent(self) -> None:
        database = create_v8301_database(self.root / "replan.db")
        migrate_v8302(database)
        plan = build_historical_migration_plan(inspect_schema(database))
        result = apply_historical_migration_to_clone(
            plan,
            database,
            rehearsal_id="rehearsal_g0_unified_replan_001",
        )
        self.assertEqual("already_applied", result.status)
        self.assertEqual(
            plan.source_schema_fingerprint,
            plan.expected_target_schema_fingerprint,
        )

    def test_v8302_is_strictly_additive(self) -> None:
        for statement in HISTORICAL_MIGRATION_DDL:
            with self.subTest(statement=statement[:50]):
                self.assertTrue(statement.startswith((
                    "CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX",
                    "CREATE TRIGGER",
                )))
                self.assertFalse(statement.startswith((
                    "DROP ", "ALTER ", "DELETE ", "UPDATE ",
                )))

    def test_v8301_marker_and_schema_sql_are_unchanged(self) -> None:
        database = create_v8301_database(self.root / "frozen.db")
        before = inspect_schema(database)
        old_objects = {
            name: (kind, table, sql)
            for kind, name, table, sql in before.object_sql
        }
        with closing(sqlite3.connect(database)) as conn:
            marker_before = conn.execute(
                "SELECT * FROM schema_migrations WHERE migration_version=?",
                (MIGRATION_VERSION,),
            ).fetchone()
        migrate_v8302(database)
        after = inspect_schema(database)
        new_objects = {
            name: (kind, table, sql)
            for kind, name, table, sql in after.object_sql
        }
        with closing(sqlite3.connect(database)) as conn:
            marker_after = conn.execute(
                "SELECT * FROM schema_migrations WHERE migration_version=?",
                (MIGRATION_VERSION,),
            ).fetchone()
        self.assertEqual(marker_before, marker_after)
        self.assertEqual(MIGRATION_DIGEST, marker_after[3])
        for name, value in old_objects.items():
            self.assertEqual(value, new_objects[name], name)

    def test_legacy_read_contracts_are_unchanged(self) -> None:
        database = create_v8301_database(self.root / "compat.db")
        before = capture_legacy_compatibility(database)
        migrate_v8302(database)
        after = capture_legacy_compatibility(database)
        result = run_compatibility_suite(before, after)
        self.assertTrue(result["passed"], result)

    def test_source_and_historical_batches_share_every_v2_table(self) -> None:
        database = create_v8301_database(self.root / "chains.db")
        migrate_v8302(database)
        before = capture_legacy_compatibility(database)
        chain = insert_unified_chains(database)
        after = capture_legacy_compatibility(database)
        compatibility = run_compatibility_suite(before, after)
        self.assertTrue(all(
            passed
            for name, passed in compatibility["checks"].items()
            if name != "new_ingestion_tables_empty"
        ), compatibility)
        self.assertFalse(compatibility["checks"]["new_ingestion_tables_empty"])
        self.assertEqual("full", chain["full_batch"].batch_mode)
        self.assertEqual(
            "historical_backfill",
            chain["historical_batch"].batch_mode,
        )
        self.assertEqual(2, chain["counts"]["ingestion_manifest_subjects"])
        for table in (
            "ingestion_batches_v2",
            "ingestion_batch_events_v2",
            "ingestion_approvals_v2",
            "ingestion_executions_v2",
            "ingestion_verifications_v2",
            "ingestion_leases_v2",
        ):
            self.assertEqual(2, chain["counts"][table], table)
        self.assertIsNone(chain["attachment_binding"].entity_digest)

    def test_migration_digest_binds_frozen_v8301(self) -> None:
        database = create_v8301_database(self.root / "digest.db")
        plan = build_historical_migration_plan(inspect_schema(database))
        self.assertEqual(HISTORICAL_MIGRATION_DIGEST, plan.migration_digest)
        self.assertNotEqual(
            "aa0021d65c9a4c5d78c8bdc471a9d4fa8be1ce94cb91c3c0e153e4782ada35d5",
            plan.migration_digest,
        )
        self.assertEqual(MIGRATION_VERSION, plan.required_migration_version)
        self.assertEqual(MIGRATION_DIGEST, plan.required_migration_digest)


if __name__ == "__main__":
    unittest.main()
