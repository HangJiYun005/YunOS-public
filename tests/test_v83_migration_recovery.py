from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_migration_rehearsal import (
    assess_recovery_state,
    create_consistent_backup,
    restore_from_backup,
)
from memory.ingestion_migrations import (
    MIGRATION_VERSION,
    MigrationInterrupted,
    MigrationRehearsalError,
    apply_migration_to_clone,
    build_migration_plan,
    downgrade_schema,
    inject_migration_failure,
    inspect_schema,
)
from tests.v83_migration_fixtures import create_legacy_database


class MigrationRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83f-recovery-")
        self.root = Path(self.temp.name)
        self.source = create_legacy_database(self.root / "source.db")
        self.source_fingerprint = inspect_schema(self.source).schema_fingerprint

    def tearDown(self) -> None:
        self.temp.cleanup()

    def clone(self, name: str) -> tuple[Path, object]:
        target = self.root / f"{name}.db"
        create_consistent_backup(self.source, target)
        return target, build_migration_plan(inspect_schema(target))

    def test_every_precommit_fault_rolls_back_all_ddl_and_marker(self) -> None:
        checkpoints = (
            "marker_created",
            "partial_tables_created",
            "tables_created",
            "indexes_created",
            "triggers_created",
            "before_commit",
        )
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                clone, plan = self.clone(checkpoint)
                with self.assertRaises(MigrationRehearsalError):
                    apply_migration_to_clone(
                        plan,
                        clone,
                        rehearsal_id="rehearsal_fault_001",
                        fault_injector=inject_migration_failure(checkpoint),
                    )
                snapshot = inspect_schema(clone)
                self.assertEqual(self.source_fingerprint, snapshot.schema_fingerprint)
                self.assertEqual("legacy", snapshot.marker_state)

    def test_process_crash_signal_before_commit_rolls_back(self) -> None:
        clone, plan = self.clone("crash")
        with self.assertRaises(MigrationInterrupted):
            apply_migration_to_clone(
                plan,
                clone,
                rehearsal_id="rehearsal_crash_001",
                fault_injector=inject_migration_failure("partial_tables_created", crash=True),
            )
        self.assertEqual(self.source_fingerprint, inspect_schema(clone).schema_fingerprint)

    def test_postcommit_report_failure_is_reconciled_from_marker_and_schema(self) -> None:
        clone, plan = self.clone("postcommit")
        with self.assertRaises(MigrationRehearsalError) as captured:
            apply_migration_to_clone(
                plan,
                clone,
                rehearsal_id="rehearsal_postcommit_001",
                fault_injector=inject_migration_failure("after_commit_report"),
            )
        self.assertTrue(captured.exception.recovery_required)
        state = assess_recovery_state(clone, plan=plan)
        self.assertEqual("completed", state["status"])
        self.assertFalse(state["automatic_retry"])

    def test_integrity_and_foreign_key_report_failures_require_recovery(self) -> None:
        for checkpoint in ("integrity_checked", "foreign_keys_checked"):
            with self.subTest(checkpoint=checkpoint):
                clone, plan = self.clone(checkpoint)
                with self.assertRaises(MigrationRehearsalError) as captured:
                    apply_migration_to_clone(
                        plan,
                        clone,
                        rehearsal_id="rehearsal_postcheck_001",
                        fault_injector=inject_migration_failure(checkpoint),
                    )
                self.assertTrue(captured.exception.recovery_required)
                self.assertEqual(
                    "completed", assess_recovery_state(clone, plan=plan)["status"],
                )

    def test_external_schema_change_at_commit_boundary_is_recovery_required(self) -> None:
        clone, plan = self.clone("external")

        def inject(checkpoint: str, conn: sqlite3.Connection) -> None:
            if checkpoint == "before_commit":
                conn.execute("CREATE TABLE external_schema_drift(value TEXT)")

        with self.assertRaises(MigrationRehearsalError) as captured:
            apply_migration_to_clone(
                plan,
                clone,
                rehearsal_id="rehearsal_external_001",
                fault_injector=inject,
            )
        self.assertTrue(captured.exception.recovery_required)
        self.assertEqual(
            "recovery_required",
            assess_recovery_state(clone, plan=plan)["status"],
        )

    def test_verified_backup_recovers_postmigration_validation_failure(self) -> None:
        clone, plan = self.clone("restore")
        backup = self.root / "verified-backup.db"
        create_consistent_backup(clone, backup)
        with self.assertRaises(MigrationRehearsalError):
            apply_migration_to_clone(
                plan,
                clone,
                rehearsal_id="rehearsal_restore_001",
                fault_injector=inject_migration_failure("integrity_checked"),
            )
        restored = restore_from_backup(clone, backup)
        self.assertEqual("restored", restored["status"])
        self.assertEqual(self.source_fingerprint, inspect_schema(clone).schema_fingerprint)

    def test_duplicate_migration_is_already_applied(self) -> None:
        clone, plan = self.clone("duplicate")
        first = apply_migration_to_clone(
            plan, clone, rehearsal_id="rehearsal_duplicate_001",
        )
        second = apply_migration_to_clone(
            plan, clone, rehearsal_id="rehearsal_duplicate_002",
        )
        self.assertEqual("completed", first.status)
        self.assertEqual("already_applied", second.status)

    def test_digest_mismatch_and_downgrade_are_blocked(self) -> None:
        clone, plan = self.clone("digest")
        altered = replace(plan, migration_digest="f" * 64)
        with self.assertRaisesRegex(
            MigrationRehearsalError, "migration_plan_digest_mismatch",
        ):
            apply_migration_to_clone(
                altered, clone, rehearsal_id="rehearsal_digest_001",
            )
        with self.assertRaisesRegex(
            MigrationRehearsalError, "automatic_downgrade_not_supported",
        ):
            downgrade_schema(clone, target_version=MIGRATION_VERSION - 1)


if __name__ == "__main__":
    unittest.main()
