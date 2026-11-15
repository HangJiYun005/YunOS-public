from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_backfill_execution import (
    _execution_target,
    execute_historical_backfill,
    recover_historical_backfill,
    rollback_historical_backfill,
)
from memory.ingestion_execution import (
    IngestionExecutionError,
    acquire_writer_lease,
    read_execution_journal,
    release_writer_lease,
)
from tests.v83_historical_backfill_fixtures import (
    CREATED_AT,
    EXPIRES_AT,
    build_historical_fixture,
)


class HistoricalBackfillRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83g-recovery-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fixture(self, name: str = "case"):
        return build_historical_fixture(self.root / name)

    def execute(self, fixture, checkpoint: str | None = None):
        return execute_historical_backfill(
            clone_path=fixture.database,
            manifest=fixture.manifest,
            subject=fixture.subject,
            plan=fixture.plan,
            approval_created_at=CREATED_AT,
            approval_expires_at=EXPIRES_AT,
            fault_injector=(
                None
                if checkpoint is None
                else lambda value: value == checkpoint
            ),
        )

    def v2_counts(self, database: Path) -> tuple[int, int, int]:
        with closing(sqlite3.connect(database)) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "ingestion_batches_v2",
                    "ingestion_historical_source_bindings",
                    "ingestion_historical_attachment_bindings",
                )
            )

    def test_precommit_failure_rolls_back_every_database_write(self) -> None:
        fixture = self.fixture()
        result, _, _ = self.execute(fixture, "before_commit")
        self.assertEqual("failed", result.final_state)
        self.assertEqual((0, 0, 0), self.v2_counts(fixture.database))

    def test_attachment_interruption_rolls_back_source_bindings_too(self) -> None:
        fixture = self.fixture()
        result, _, _ = self.execute(
            fixture,
            "attachment_binding_interruption",
        )
        self.assertEqual("failed", result.final_state)
        self.assertIn("attachment_binding_interrupted", result.errors)
        self.assertEqual((0, 0, 0), self.v2_counts(fixture.database))

    def test_event_journal_failure_releases_writer_lease(self) -> None:
        fixture = self.fixture()
        result, _, _ = self.execute(fixture, "event_journal_failure")
        self.assertEqual("failed", result.final_state)
        self.assertIn("event_journal_write_failed", result.errors)
        target = _execution_target(fixture.database, fixture.plan)
        lease = acquire_writer_lease(target, fixture.plan.batch_id)
        release_writer_lease(target, lease)

    def test_postcommit_failure_requires_reconciliation(self) -> None:
        fixture = self.fixture()
        result, _, bundle = self.execute(fixture, "after_commit")
        recovery = recover_historical_backfill(
            clone_path=fixture.database,
            plan=fixture.plan,
            bundle=bundle,
        )
        self.assertEqual("recovery_required", result.final_state)
        self.assertEqual("recovery_required", recovery["status"])

    def test_verification_failure_marks_recovery_required(self) -> None:
        fixture = self.fixture()
        result, _, _ = self.execute(fixture, "verification_failure")
        self.assertEqual("recovery_required", result.final_state)
        with closing(sqlite3.connect(fixture.database)) as connection:
            state = connection.execute(
                "SELECT current_state,recovery_status "
                "FROM ingestion_batches_v2"
            ).fetchone()
        self.assertEqual(("recovery_required", "required"), tuple(state))

    def test_completion_event_interruption_never_reports_completed(self) -> None:
        fixture = self.fixture()
        result, _, bundle = self.execute(
            fixture,
            "after_verification_before_completed",
        )
        recovery = recover_historical_backfill(
            clone_path=fixture.database,
            plan=fixture.plan,
            bundle=bundle,
        )
        self.assertEqual("recovery_required", result.final_state)
        self.assertEqual("recovery_required", recovery["status"])

    def test_rollback_restores_pre_backfill_clone(self) -> None:
        fixture = self.fixture()
        result, _, bundle = self.execute(fixture, "after_commit")
        self.assertEqual("recovery_required", result.final_state)
        rollback = rollback_historical_backfill(
            clone_path=fixture.database,
            execution_id=bundle.execution_id,
        )
        self.assertEqual("rolled_back", rollback["status"])
        self.assertEqual((0, 0, 0), self.v2_counts(fixture.database))

    def test_rollback_failure_stays_recovery_required(self) -> None:
        fixture = self.fixture()
        _, _, bundle = self.execute(fixture, "after_commit")
        rollback = rollback_historical_backfill(
            clone_path=fixture.database,
            execution_id=bundle.execution_id,
            fail_restore=True,
        )
        self.assertEqual("recovery_required", rollback["status"])
        self.assertEqual("rollback_failed", rollback["reason_code"])

    def test_writer_lease_conflict_fails_closed(self) -> None:
        fixture = self.fixture()
        target = _execution_target(fixture.database, fixture.plan)
        lease = acquire_writer_lease(target, fixture.plan.batch_id)
        try:
            with self.assertRaisesRegex(
                IngestionExecutionError,
                "writer_lease_conflict",
            ):
                self.execute(fixture)
        finally:
            release_writer_lease(target, lease)

    def test_journal_digest_tamper_is_detected(self) -> None:
        fixture = self.fixture()
        result, _, _ = self.execute(fixture)
        journal = (
            fixture.database.parent
            / "executions"
            / result.execution_id
            / "journal.jsonl"
        )
        rows = journal.read_text(encoding="utf-8").splitlines()
        event = json.loads(rows[0])
        event["event_type"] = "execution_failed"
        rows[0] = json.dumps(event, sort_keys=True, separators=(",", ":"))
        journal.write_text("\n".join(rows) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            IngestionExecutionError,
            "execution_journal_corrupt",
        ):
            read_execution_journal(
                journal,
                execution_id=result.execution_id,
            )


if __name__ == "__main__":
    unittest.main()
