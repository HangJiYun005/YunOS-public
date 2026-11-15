from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_migration_rehearsal import (
    create_consistent_backup,
    restore_from_backup,
)
from memory.ingestion_migrations import (
    HISTORICAL_INGESTION_TABLES,
    HISTORICAL_MIGRATION_DIGEST,
    HISTORICAL_MIGRATION_VERSION,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
    MigrationInterrupted,
    MigrationRehearsalError,
    apply_historical_migration_to_clone,
    assess_historical_migration_recovery,
    build_historical_migration_plan,
    inject_historical_migration_failure,
    inspect_schema,
    verify_historical_migrated_schema,
)
from tests.v83_historical_schema_fixtures import (
    create_v8301_database,
    migrate_v8302,
)
from tests.v83_migration_fixtures import create_legacy_database


class HistoricalSchemaRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83g0-recovery-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_v8302_without_v8301_is_blocked(self) -> None:
        database = create_legacy_database(self.root / "legacy.db")
        with self.assertRaisesRegex(
            MigrationRehearsalError, "historical_migration_requires_v8301",
        ):
            build_historical_migration_plan(inspect_schema(database))

    def test_v8301_digest_mismatch_is_blocked(self) -> None:
        database = create_v8301_database(self.root / "bad-v8301.db")
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("DROP TRIGGER schema_migrations_no_update")
            conn.execute(
                "UPDATE schema_migrations SET migration_digest=? "
                "WHERE migration_version=?",
                ("f" * 64, MIGRATION_VERSION),
            )
            conn.commit()
        with self.assertRaisesRegex(
            MigrationRehearsalError, "v8301_digest_mismatch",
        ):
            build_historical_migration_plan(inspect_schema(database))

    def test_unknown_same_name_table_is_fail_closed(self) -> None:
        database = create_v8301_database(self.root / "collision.db")
        with closing(sqlite3.connect(database)) as conn:
            conn.execute(
                "CREATE TABLE ingestion_historical_manifests(unknown TEXT)"
            )
            conn.commit()
        with self.assertRaisesRegex(
            MigrationRehearsalError,
            "historical_migration_marker_missing|v8301_schema_drift",
        ):
            build_historical_migration_plan(inspect_schema(database))

    def test_same_version_different_digest_is_recovery_required(self) -> None:
        database = create_v8301_database(self.root / "same-version.db")
        migrate_v8302(database)
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("DROP TRIGGER schema_migrations_no_update")
            conn.execute(
                "UPDATE schema_migrations SET migration_digest=? "
                "WHERE migration_version=?",
                ("f" * 64, HISTORICAL_MIGRATION_VERSION),
            )
            conn.commit()
        with self.assertRaisesRegex(
            MigrationRehearsalError, "historical_migration_digest_mismatch",
        ) as captured:
            build_historical_migration_plan(inspect_schema(database))
        self.assertTrue(captured.exception.recovery_required)

    def test_higher_unknown_version_is_blocked(self) -> None:
        database = create_v8301_database(self.root / "higher.db")
        migrate_v8302(database)
        with closing(sqlite3.connect(database)) as conn:
            row = conn.execute(
                "SELECT * FROM schema_migrations WHERE migration_version=?",
                (HISTORICAL_MIGRATION_VERSION,),
            ).fetchone()
            conn.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "migration_unknown_future_9999",
                    9999,
                    "unknown_future",
                    "f" * 64,
                    row[4],
                    "unknown",
                    row[7],
                    row[7],
                    "rehearsal_unknown_9999",
                    "unknown",
                ),
            )
            conn.execute("PRAGMA user_version=9999")
            conn.commit()
        with self.assertRaisesRegex(
            MigrationRehearsalError, "higher_unknown_schema_version",
        ):
            build_historical_migration_plan(inspect_schema(database))

    def test_precommit_failure_rolls_back_every_historical_object(self) -> None:
        checkpoints = (
            "historical_partial_tables_created",
            "historical_tables_created",
            "historical_indexes_created",
            "historical_triggers_created",
            "historical_marker_written",
            "historical_before_commit",
        )
        for index, checkpoint in enumerate(checkpoints):
            with self.subTest(checkpoint=checkpoint):
                database = create_v8301_database(
                    self.root / f"precommit-{index}.db"
                )
                before = inspect_schema(database)
                plan = build_historical_migration_plan(before)
                with self.assertRaises(MigrationRehearsalError):
                    apply_historical_migration_to_clone(
                        plan,
                        database,
                        rehearsal_id=f"rehearsal_g0_precommit_{index:03d}",
                        fault_injector=inject_historical_migration_failure(
                            checkpoint
                        ),
                    )
                after = inspect_schema(database)
                self.assertEqual(before.schema_fingerprint, after.schema_fingerprint)
                self.assertEqual(MIGRATION_VERSION, after.migration_version)
                self.assertEqual(MIGRATION_DIGEST, after.migration_digest)
                self.assertFalse(
                    set(HISTORICAL_INGESTION_TABLES).intersection(
                        after.object_names
                    )
                )

    def test_precommit_crash_rolls_back_and_allows_reviewed_retry(self) -> None:
        database = create_v8301_database(self.root / "precommit-crash.db")
        plan = build_historical_migration_plan(inspect_schema(database))
        with self.assertRaises(MigrationInterrupted):
            apply_historical_migration_to_clone(
                plan,
                database,
                rehearsal_id="rehearsal_g0_crash_001",
                fault_injector=inject_historical_migration_failure(
                    "historical_before_commit", crash=True,
                ),
            )
        recovery = assess_historical_migration_recovery(database, plan=plan)
        self.assertEqual("not_applied", recovery["status"])
        self.assertFalse(recovery["automatic_retry"])
        result = apply_historical_migration_to_clone(
            plan,
            database,
            rehearsal_id="rehearsal_g0_crash_retry_001",
        )
        self.assertEqual("completed", result.status)

    def test_postcommit_failure_reconciles_completed_marker(self) -> None:
        for index, checkpoint in enumerate((
            "historical_after_commit_report",
            "historical_integrity_checked",
            "historical_foreign_keys_checked",
        )):
            with self.subTest(checkpoint=checkpoint):
                database = create_v8301_database(
                    self.root / f"postcommit-{index}.db"
                )
                plan = build_historical_migration_plan(inspect_schema(database))
                with self.assertRaises(MigrationRehearsalError) as captured:
                    apply_historical_migration_to_clone(
                        plan,
                        database,
                        rehearsal_id=f"rehearsal_g0_postcommit_{index:03d}",
                        fault_injector=inject_historical_migration_failure(
                            checkpoint
                        ),
                    )
                self.assertTrue(captured.exception.recovery_required)
                recovery = assess_historical_migration_recovery(
                    database, plan=plan,
                )
                self.assertEqual("completed", recovery["status"])
                verification = verify_historical_migrated_schema(
                    database,
                    expected_fingerprint=plan.expected_target_schema_fingerprint,
                    require_empty=True,
                )
                self.assertTrue(verification["marker_chain_valid"])

    def test_schema_drift_is_recovery_required(self) -> None:
        database = create_v8301_database(self.root / "drift.db")
        plan = migrate_v8302(database)
        with closing(sqlite3.connect(database)) as conn:
            conn.execute(
                "CREATE INDEX idx_unified_unknown_drift "
                "ON ingestion_batches_v2(current_state)"
            )
            conn.commit()
        with self.assertRaisesRegex(
            MigrationRehearsalError, "historical_migrated_schema_marker_mismatch",
        ) as captured:
            verify_historical_migrated_schema(
                database,
                expected_fingerprint=plan.expected_target_schema_fingerprint,
            )
        self.assertTrue(captured.exception.recovery_required)
        recovery = assess_historical_migration_recovery(database, plan=plan)
        self.assertEqual("recovery_required", recovery["status"])

    def test_verified_backup_restores_v8301_clone(self) -> None:
        database = create_v8301_database(self.root / "restore.db")
        backup = self.root / "restore-backup.db"
        before = inspect_schema(database)
        backup_result = create_consistent_backup(database, backup)
        self.assertEqual("ok", backup_result.integrity_check)
        migrate_v8302(database)
        self.assertEqual(
            HISTORICAL_MIGRATION_VERSION,
            inspect_schema(database).migration_version,
        )
        result = restore_from_backup(database, backup)
        after = inspect_schema(database)
        self.assertEqual("restored", result["status"])
        self.assertEqual(before.schema_fingerprint, after.schema_fingerprint)
        self.assertEqual(MIGRATION_VERSION, after.migration_version)
        self.assertEqual(MIGRATION_DIGEST, after.migration_digest)

    def test_marker_records_expected_v8302_digest(self) -> None:
        database = create_v8301_database(self.root / "digest.db")
        migrate_v8302(database)
        with closing(sqlite3.connect(database)) as conn:
            marker = conn.execute(
                "SELECT migration_digest FROM schema_migrations "
                "WHERE migration_version=?",
                (HISTORICAL_MIGRATION_VERSION,),
            ).fetchone()[0]
        self.assertEqual(HISTORICAL_MIGRATION_DIGEST, marker)


if __name__ == "__main__":
    unittest.main()
