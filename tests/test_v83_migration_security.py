from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_migration_rehearsal import (
    create_consistent_backup,
    persist_synthetic_integration,
)
from memory.ingestion_migrations import (
    MIGRATION_TABLE_DDL,
    MIGRATION_VERSION,
    PRODUCTION_DATABASE,
    MigrationRehearsalError,
    _lock_path,
    apply_migration_to_clone,
    apply_production_migration,
    build_migration_plan,
    build_production_migration_approval_package,
    inspect_schema,
    validate_clone_database_path,
)
from tests.v83_migration_fixtures import create_legacy_database, migrate_database


class MigrationSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83f-security-")
        self.root = Path(self.temp.name)
        self.source = create_legacy_database(self.root / "source.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_production_path_and_production_entrypoint_are_permanently_blocked(self) -> None:
        with self.assertRaisesRegex(
            MigrationRehearsalError, "production_migration_not_authorized",
        ):
            validate_clone_database_path(PRODUCTION_DATABASE)
        with self.assertRaisesRegex(
            MigrationRehearsalError, "production_migration_not_authorized",
        ):
            apply_production_migration()

    def test_relative_unc_device_ads_and_outside_paths_are_rejected(self) -> None:
        candidates = (
            "relative.db",
            r"\\server\share\clone.db",
            r"\\?\C:\temp\clone.db",
            r"\\.\C:\temp\clone.db",
            str(self.root / "clone.db") + ":secret",
            str(Path("E:/AyunOS/data/not-production-name.db")),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(MigrationRehearsalError):
                    validate_clone_database_path(candidate, must_exist=False)

    def test_unknown_same_name_table_blocks_before_any_migration(self) -> None:
        database = create_legacy_database(self.root / "collision.db")
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE ingestion_batches(unknown_column TEXT)")
        with self.assertRaisesRegex(
            MigrationRehearsalError, "unknown_same_name_schema_object",
        ):
            build_migration_plan(inspect_schema(database))

    def test_higher_unknown_user_version_is_blocked(self) -> None:
        database = create_legacy_database(self.root / "higher.db")
        with closing(sqlite3.connect(database)) as conn:
            conn.execute(f"PRAGMA user_version={MIGRATION_VERSION + 1}")
        with self.assertRaisesRegex(
            MigrationRehearsalError, "higher_unknown_schema_version",
        ):
            build_migration_plan(inspect_schema(database))

    def test_same_version_different_digest_fails_closed(self) -> None:
        database = create_legacy_database(self.root / "digest.db")
        with closing(sqlite3.connect(database)) as conn:
            conn.execute(MIGRATION_TABLE_DDL[0])
            conn.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "migration_unknown_same_version", MIGRATION_VERSION,
                    "unknown", "f" * 64, "now", "unknown", "a" * 64,
                    "b" * 64, "rehearsal_unknown_001", "unknown",
                ),
            )
            conn.commit()
        with self.assertRaisesRegex(
            MigrationRehearsalError, "migration_digest_mismatch",
        ):
            build_migration_plan(inspect_schema(database))

    def test_missing_marker_index_and_trigger_are_schema_drift(self) -> None:
        for kind in ("marker", "index", "trigger"):
            with self.subTest(kind=kind):
                database = create_legacy_database(self.root / f"drift-{kind}.db")
                migrate_database(database)
                with closing(sqlite3.connect(database)) as conn:
                    if kind == "marker":
                        conn.execute("DROP TABLE schema_migrations")
                    elif kind == "index":
                        conn.execute("DROP INDEX ingestion_batches_state_idx")
                    else:
                        conn.execute("DROP TRIGGER ingestion_batch_events_no_delete")
                with self.assertRaises(MigrationRehearsalError) as captured:
                    build_migration_plan(inspect_schema(database))
                self.assertTrue(captured.exception.recovery_required)

    def test_concurrent_migrator_lock_is_fail_closed(self) -> None:
        database = create_legacy_database(self.root / "locked.db")
        plan = build_migration_plan(inspect_schema(database))
        lock = _lock_path(database.resolve())
        lock.write_text("held", encoding="utf-8")
        with self.assertRaisesRegex(
            MigrationRehearsalError, "concurrent_migrator_blocked",
        ):
            apply_migration_to_clone(
                plan, database, rehearsal_id="rehearsal_locked_001",
            )
        self.assertEqual("legacy", inspect_schema(database).marker_state)

    def test_insufficient_disk_gate_runs_before_transaction(self) -> None:
        database = create_legacy_database(self.root / "disk.db")
        plan = build_migration_plan(inspect_schema(database))
        with self.assertRaisesRegex(
            MigrationRehearsalError, "insufficient_disk_space",
        ):
            apply_migration_to_clone(
                plan,
                database,
                rehearsal_id="rehearsal_disk_001",
                minimum_free_bytes=2**63 - 1,
            )
        self.assertEqual("legacy", inspect_schema(database).marker_state)

    def test_reports_contain_no_paths_private_text_or_credentials(self) -> None:
        database = create_legacy_database(self.root / "privacy.db")
        plan = migrate_database(database)
        synthetic = persist_synthetic_integration(database)
        package = build_production_migration_approval_package(
            plan=plan,
            current_production_fingerprint=plan.source_schema_fingerprint,
            compatibility_summary={"passed": True},
            failure_injection_summary={"passed": True},
            rollback_summary={"passed": True},
            rehearsal_run_count=1,
            expires_at="2026-07-29T00:00:00Z",
        )
        raw = json.dumps({"synthetic": synthetic, "package": package}).lower()
        for forbidden in (
            str(self.root).lower(),
            "synthetic user text",
            "synthetic assistant text",
            "tailnet",
            "cookie",
            "bearer ",
            "sk-",
        ):
            self.assertNotIn(forbidden, raw)


if __name__ == "__main__":
    unittest.main()
