from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
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
    HISTORICAL_INGESTION_TABLES,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
    MigrationRehearsalError,
    apply_migration_to_clone,
    build_migration_plan,
    inject_migration_failure,
    inspect_schema,
)
from memory.production_migration_clone import execute_v8301_clone_protocol
from tests.v83_migration_fixtures import create_legacy_database


class ProductionMigrationRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v8301-recovery-")
        self.root = Path(self.temp.name)
        self.source = create_legacy_database(self.root / "source.db")
        self.source_snapshot = inspect_schema(self.source)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def clone(self, name: str):
        directory = self.root / name
        directory.mkdir()
        target = directory / "target.db"
        create_consistent_backup(self.source, target)
        return directory, target, build_migration_plan(inspect_schema(target))

    @staticmethod
    def journal(root: Path):
        return [
            json.loads(line)
            for line in (root / "execution_journal.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    def test_exact_v8301_clone_success_preserves_legacy_and_excludes_v8302(self) -> None:
        root, clone, plan = self.clone("success")
        before = inspect_schema(clone)
        result = execute_v8301_clone_protocol(
            plan=plan,
            clone_path=clone,
            execution_root=root,
            execution_id="execution_v8301_clone_success_001",
        )
        after = inspect_schema(clone)
        self.assertEqual("completed", result.status)
        self.assertEqual(MIGRATION_VERSION, after.migration_version)
        self.assertEqual(MIGRATION_DIGEST, after.migration_digest)
        self.assertEqual(plan.expected_target_schema_fingerprint, after.schema_fingerprint)
        self.assertEqual(before.row_counts, after.row_counts)
        self.assertEqual(before.id_set_digests, after.id_set_digests)
        self.assertEqual(before.critical_field_digests, after.critical_field_digests)
        self.assertFalse(set(HISTORICAL_INGESTION_TABLES).intersection(after.object_names))
        with closing(sqlite3.connect(clone)) as connection:
            versions = [
                int(row[0])
                for row in connection.execute(
                    "SELECT migration_version FROM schema_migrations "
                    "ORDER BY migration_version"
                )
            ]
        self.assertEqual([8301], versions)
        events = [record["event"] for record in self.journal(root)]
        for expected in (
            "lease_acquired",
            "transaction_started",
            "migration_applied",
            "marker_written",
            "transaction_committed",
            "verification_started",
            "verification_passed",
            "execution_completed",
        ):
            self.assertIn(expected, events)

    def test_already_applied_is_safe_and_does_not_add_v8302(self) -> None:
        root, clone, plan = self.clone("already")
        first = execute_v8301_clone_protocol(
            plan=plan,
            clone_path=clone,
            execution_root=root,
            execution_id="execution_v8301_clone_already_001",
        )
        second = apply_migration_to_clone(
            plan=plan,
            clone_path=clone,
            rehearsal_id="execution_v8301_clone_already_002",
        )
        self.assertEqual("completed", first.status)
        self.assertEqual("already_applied", second.status)
        snapshot = inspect_schema(clone)
        self.assertEqual(8301, snapshot.migration_version)
        self.assertFalse(set(HISTORICAL_INGESTION_TABLES).intersection(snapshot.object_names))

    def test_digest_and_schema_drift_fail_before_migration(self) -> None:
        root, clone, plan = self.clone("drift")
        altered = replace(plan, migration_digest="f" * 64)
        with self.assertRaisesRegex(
            MigrationRehearsalError,
            "migration_plan_digest_mismatch",
        ):
            execute_v8301_clone_protocol(
                plan=altered,
                clone_path=clone,
                execution_root=root,
                execution_id="execution_v8301_clone_digest_001",
            )
        with closing(sqlite3.connect(clone)) as connection:
            connection.execute("CREATE TABLE external_drift(value TEXT)")
        with self.assertRaisesRegex(
            MigrationRehearsalError,
            "migration_source_schema_drift",
        ):
            execute_v8301_clone_protocol(
                plan=plan,
                clone_path=clone,
                execution_root=root,
                execution_id="execution_v8301_clone_digest_001",
            )
        self.assertEqual("legacy", inspect_schema(clone).marker_state)

    def test_precommit_failure_rolls_back_and_journals_execution_failed(self) -> None:
        root, clone, plan = self.clone("precommit")
        with self.assertRaises(MigrationRehearsalError) as captured:
            execute_v8301_clone_protocol(
                plan=plan,
                clone_path=clone,
                execution_root=root,
                execution_id="execution_v8301_clone_precommit_001",
                fault_injector=inject_migration_failure("before_commit"),
            )
        self.assertFalse(captured.exception.recovery_required)
        after = inspect_schema(clone)
        self.assertEqual(self.source_snapshot.schema_fingerprint, after.schema_fingerprint)
        self.assertEqual("legacy", after.marker_state)
        events = [record["event"] for record in self.journal(root)]
        self.assertIn("execution_failed", events)
        self.assertNotIn("transaction_committed", events)
        self.assertNotIn("recovery_required", events)

    def test_postcommit_uncertainty_requires_recovery_and_forbids_auto_retry(self) -> None:
        root, clone, plan = self.clone("postcommit")
        with self.assertRaises(MigrationRehearsalError) as captured:
            execute_v8301_clone_protocol(
                plan=plan,
                clone_path=clone,
                execution_root=root,
                execution_id="execution_v8301_clone_postcommit_001",
                fault_injector=inject_migration_failure("after_commit_report"),
            )
        self.assertTrue(captured.exception.recovery_required)
        events = [record["event"] for record in self.journal(root)]
        self.assertIn("transaction_committed", events)
        self.assertIn("recovery_required", events)
        recovery = assess_recovery_state(clone, plan=plan)
        self.assertEqual("completed", recovery["status"])
        self.assertFalse(recovery["automatic_retry"])

    def test_verified_backup_restores_postcommit_clone(self) -> None:
        root, clone, plan = self.clone("restore")
        backup = root / "verified-backup.db"
        create_consistent_backup(clone, backup)
        with self.assertRaises(MigrationRehearsalError):
            execute_v8301_clone_protocol(
                plan=plan,
                clone_path=clone,
                execution_root=root,
                execution_id="execution_v8301_clone_restore_001",
                fault_injector=inject_migration_failure("integrity_checked"),
            )
        restored = restore_from_backup(clone, backup)
        self.assertEqual("restored", restored["status"])
        self.assertEqual(
            self.source_snapshot.schema_fingerprint,
            inspect_schema(clone).schema_fingerprint,
        )


if __name__ == "__main__":
    unittest.main()
