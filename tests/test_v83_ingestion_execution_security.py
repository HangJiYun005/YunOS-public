from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from memory.ingestion_execution import (
    IngestionExecutionError,
    _write_recovery_marker,
    acquire_writer_lease,
    execute_test_bundle,
    initialize_test_execution_target,
    preflight_execution,
    read_execution_journal,
    release_writer_lease,
    validate_test_execution_root,
)
from memory.ingestion_verification import (
    IngestionVerificationError,
    capture_test_target_snapshot,
)
from tests.v83_ingestion_execution_fixtures import (
    EXPIRES_AT,
    PREFLIGHT_AT,
    approve_fixture,
    build_execution_fixture,
)


class IngestionExecutionSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = build_execution_fixture(self.root / "fixture")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def preflight(self, **changes):
        values = {
            "batch": self.fixture.batch,
            "manifest": self.fixture.manifest,
            "import_plan": self.fixture.plan,
            "stage_plan": self.fixture.stage_plan,
            "stage_result": self.fixture.stage_result,
            "approval": self.fixture.approval,
            "operations": self.fixture.operations,
            "target": self.fixture.target,
            "before_snapshot": self.fixture.before,
            "staged_entities": self.fixture.staged_entities,
            "now": PREFLIGHT_AT,
        }
        values.update(changes)
        return preflight_execution(**values)

    def test_relative_and_traversal_roots_are_rejected(self) -> None:
        for value in (
            "relative-root",
            str(self.root / ".." / "escape"),
            str(self.root / "%2e%2e" / "escape"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(IngestionExecutionError):
                    validate_test_execution_root(value)

    def test_unc_device_ads_and_production_roots_are_rejected(self) -> None:
        values = (
            r"\\server\share\execution",
            r"\\?\C:\Temp\execution",
            r"C:\Temp\execution:stream",
            str(Path("E:/AyunOS/data/test-execution")),
            str(Path("E:/AyunOS/imports/test-execution")),
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(IngestionExecutionError):
                    validate_test_execution_root(value)

    def test_production_database_lacks_test_execution_schema(self) -> None:
        production = Path("E:/AyunOS/data/ayunos_chatgpt.db")
        if not production.is_file():
            self.skipTest("production database not present")
        with self.assertRaisesRegex(
            IngestionVerificationError,
            "production_execution_schema_missing",
        ):
            capture_test_target_snapshot(
                production,
                self.fixture.target.asset_root,
            )

    def test_changed_target_blocks_preflight(self) -> None:
        with self.fixture.target.database_path.open("ab") as handle:
            handle.write(b" ")
        result = self.preflight()
        self.assertFalse(result.passed)
        self.assertIn("target_snapshot_changed", result.reason_codes)

    def test_expired_approval_blocks_preflight(self) -> None:
        result = self.preflight(now=EXPIRES_AT)
        self.assertFalse(result.passed)
        self.assertIn(
            "approval_invalid_or_expired",
            result.reason_codes,
        )

    def test_incomplete_asset_result_blocks_preflight(self) -> None:
        fixture = build_execution_fixture(
            self.root / "asset-fixture",
            with_asset=True,
        )
        from dataclasses import replace

        incomplete = replace(
            fixture.stage_result,
            status="failed",
            asset_stage_fingerprint=None,
            reason_codes=("synthetic_failure",),
        )
        result = preflight_execution(
            batch=fixture.batch,
            manifest=fixture.manifest,
            import_plan=fixture.plan,
            stage_plan=fixture.stage_plan,
            stage_result=incomplete,
            approval=fixture.approval,
            operations=fixture.operations,
            target=fixture.target,
            before_snapshot=fixture.before,
            staged_entities=fixture.staged_entities,
            now=PREFLIGHT_AT,
        )
        self.assertFalse(result.passed)
        self.assertIn(
            "asset_stage_result_incomplete",
            result.reason_codes,
        )

    def test_disk_budget_gate_fails_before_execution(self) -> None:
        result = self.preflight(minimum_free_bytes=10**30)
        self.assertFalse(result.passed)
        self.assertIn("insufficient_disk_space", result.reason_codes)
        self.assertEqual(
            0,
            len(list((self.fixture.target.root).glob("executions/*"))),
        )

    def test_target_and_batch_leases_are_cross_process_style_exclusive(self) -> None:
        first = acquire_writer_lease(
            self.fixture.target,
            self.fixture.batch.batch_id,
        )
        try:
            with self.assertRaisesRegex(
                IngestionExecutionError,
                "writer_lease_conflict",
            ):
                acquire_writer_lease(
                    self.fixture.target,
                    self.fixture.batch.batch_id,
                )
            other = initialize_test_execution_target(
                self.root / "other-target"
            )
            with self.assertRaisesRegex(
                IngestionExecutionError,
                "writer_lease_conflict",
            ):
                acquire_writer_lease(
                    other,
                    self.fixture.batch.batch_id,
                )
        finally:
            release_writer_lease(self.fixture.target, first)

    def test_different_targets_and_batches_can_run_in_parallel(self) -> None:
        other = build_execution_fixture(self.root / "other-fixture")
        other_batch_id = "batch_" + "f" * 32
        first = acquire_writer_lease(
            self.fixture.target,
            self.fixture.batch.batch_id,
        )
        try:
            second = acquire_writer_lease(
                other.target,
                other_batch_id,
            )
            release_writer_lease(other.target, second)
        finally:
            release_writer_lease(self.fixture.target, first)

    def test_recovery_marker_blocks_preflight(self) -> None:
        passed = self.preflight()
        self.assertTrue(passed.passed)
        _write_recovery_marker(
            self.fixture.target,
            passed.bundle,
            "synthetic_recovery_required",
        )
        result = self.preflight()
        self.assertFalse(result.passed)
        self.assertIn("recovery_required_exists", result.reason_codes)

    def test_journal_and_audit_report_do_not_contain_private_values(self) -> None:
        passed = self.preflight()
        self.assertTrue(passed.passed, passed.reason_codes)
        result = execute_test_bundle(
            bundle=passed.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=(),
        )
        self.assertEqual("completed", result.status)
        execution_root = (
            self.fixture.target.root
            / "executions"
            / passed.bundle.execution_id
        )
        journal_path = execution_root / "journal.jsonl"
        events = read_execution_journal(
            journal_path,
            execution_id=passed.bundle.execution_id,
        )
        self.assertGreater(len(events), 5)
        combined = (
            journal_path.read_text(encoding="utf-8")
            + (execution_root / "audit-report.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn(str(self.fixture.root), combined)
        self.assertNotIn("fixture", combined)
        self.assertNotIn(
            self.fixture.plan.actions[0].source_identity,
            combined,
        )


if __name__ == "__main__":
    unittest.main()
