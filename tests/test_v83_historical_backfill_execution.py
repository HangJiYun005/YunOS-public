from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_approval import IngestionApprovalError
from memory.ingestion_backfill import HistoricalBackfillError
from memory.ingestion_backfill_execution import (
    assert_unified_historical_authority,
    build_historical_approval,
    build_historical_operations,
    execute_historical_backfill,
    validate_historical_clone_target,
)
from memory.ingestion_migration_rehearsal import (
    capture_legacy_compatibility,
    run_compatibility_suite,
)
from memory.ingestion_migrations import (
    HISTORICAL_MIGRATION_DIGEST,
    HISTORICAL_MIGRATION_VERSION,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
)
from tests.v83_historical_backfill_fixtures import (
    CREATED_AT,
    EXPIRES_AT,
    build_historical_fixture,
)


class HistoricalBackfillExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83g-exec-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def execute(self, fixture, **kwargs):
        return execute_historical_backfill(
            clone_path=fixture.database,
            manifest=fixture.manifest,
            subject=fixture.subject,
            plan=fixture.plan,
            approval_created_at=CREATED_AT,
            approval_expires_at=EXPIRES_AT,
            **kwargs,
        )

    def test_clone_backfill_writes_only_v8302_tables(self) -> None:
        fixture = build_historical_fixture(self.root)
        before = capture_legacy_compatibility(fixture.database)
        result, approval, bundle = self.execute(fixture)
        after = capture_legacy_compatibility(fixture.database)
        self.assertEqual("completed", result.final_state)
        self.assertEqual(3, result.applied_bindings)
        self.assertEqual(1, result.attachment_bindings)
        self.assertTrue(run_compatibility_suite(
            before,
            after,
            require_empty_ingestion_tables=False,
        )["passed"])
        self.assertEqual(fixture.plan.plan_digest, approval.plan_digest)
        self.assertEqual(approval.approval_digest, bundle.approval_digest)

    def test_completed_plan_replay_skips_all_bindings(self) -> None:
        fixture = build_historical_fixture(self.root)
        first, _, _ = self.execute(fixture)
        replay, _, _ = self.execute(fixture)
        self.assertEqual("completed", replay.final_state)
        self.assertEqual(first.execution_id, replay.execution_id)
        self.assertEqual(4, replay.skipped_bindings)
        with closing(sqlite3.connect(fixture.database)) as connection:
            source = connection.execute(
                "SELECT COUNT(*) FROM ingestion_historical_source_bindings"
            ).fetchone()[0]
            attachments = connection.execute(
                "SELECT COUNT(*) FROM ingestion_historical_attachment_bindings"
            ).fetchone()[0]
        self.assertEqual((3, 1), (source, attachments))

    def test_native_records_are_verified_without_source_binding_rows(self) -> None:
        fixture = build_historical_fixture(
            self.root,
            source_platform="chatgpt",
        )
        result, _, _ = self.execute(fixture)
        self.assertEqual("completed", result.final_state)
        self.assertEqual(0, result.applied_bindings)
        self.assertEqual(1, result.attachment_bindings)

    def test_old_table_authorizer_rolls_back_transaction(self) -> None:
        fixture = build_historical_fixture(self.root)
        before = capture_legacy_compatibility(fixture.database)
        result, _, _ = self.execute(
            fixture,
            fault_injector=lambda checkpoint: (
                checkpoint == "attempt_old_table_write"
            ),
        )
        after = capture_legacy_compatibility(fixture.database)
        self.assertEqual("failed", result.final_state)
        self.assertIn("old_table_write_guard", result.errors[0])
        self.assertTrue(run_compatibility_suite(
            before,
            after,
            require_empty_ingestion_tables=False,
        )["passed"])

    def test_snapshot_change_requires_new_plan(self) -> None:
        fixture = build_historical_fixture(self.root)
        with closing(sqlite3.connect(fixture.database)) as connection:
            connection.execute(
                "UPDATE conversations SET title=?",
                ("changed disposable title",),
            )
            connection.commit()
        with self.assertRaisesRegex(
            HistoricalBackfillError,
            "historical_target_snapshot_stale",
        ):
            self.execute(fixture)

    def test_expired_approval_is_rejected(self) -> None:
        fixture = build_historical_fixture(self.root)
        operations = build_historical_operations(fixture.plan)
        _, approval = build_historical_approval(
            fixture.plan,
            operations,
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        with self.assertRaisesRegex(IngestionApprovalError, "approval_expired"):
            execute_historical_backfill(
                clone_path=fixture.database,
                manifest=fixture.manifest,
                subject=fixture.subject,
                plan=fixture.plan,
                approval=approval,
                approval_created_at="2026-07-31T00:00:00.000000Z",
                approval_expires_at="2026-08-01T00:00:00.000000Z",
            )

    def test_unified_authority_aliases_are_runtime_asserted(self) -> None:
        self.assertIsNone(assert_unified_historical_authority())

    def test_operation_types_are_closed_and_non_destructive(self) -> None:
        fixture = build_historical_fixture(self.root)
        operations = build_historical_operations(fixture.plan)
        self.assertEqual(
            {
                "create_legacy_source_binding",
                "create_historical_attachment_binding",
                "verify_media_identity",
            },
            {item.operation_type for item in operations},
        )
        self.assertFalse(any(
            item.operation_type.startswith(("update_", "delete_", "rewrite_"))
            for item in operations
        ))

    def test_production_database_path_is_hard_refused(self) -> None:
        production = Path("data/ayunos_chatgpt.db").resolve()
        with self.assertRaisesRegex(
            HistoricalBackfillError,
            "production_path_forbidden",
        ):
            validate_historical_clone_target(production)

    def test_fixed_migration_chain_is_exposed(self) -> None:
        self.assertEqual(8301, MIGRATION_VERSION)
        self.assertEqual(8302, HISTORICAL_MIGRATION_VERSION)
        self.assertEqual(
            "854a505aaa943306c87af95ddbddd44d24fe240d43a6364102256f3778d5bf5e",
            MIGRATION_DIGEST,
        )
        self.assertEqual(
            "44a14504c1741a1c05dcd69e370f639569b4b5ac44776bd5a45028ae82e41a44",
            HISTORICAL_MIGRATION_DIGEST,
        )


if __name__ == "__main__":
    unittest.main()
