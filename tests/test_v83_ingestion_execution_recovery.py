from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from memory.ingestion_approval import make_approval_artifact
from memory.ingestion_execution import (
    ExecutionInterrupted,
    IngestionExecutionError,
    execute_test_bundle,
    preflight_execution,
    read_execution_journal,
    recover_test_execution,
    release_writer_lease,
    rollback_test_execution,
)
from memory.ingestion_verification import capture_test_target_snapshot
from tests.v83_ingestion_execution_fixtures import (
    CREATED_AT,
    EXPIRES_AT,
    PREFLIGHT_AT,
    approve_fixture,
    build_execution_fixture,
)


class IngestionExecutionRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = build_execution_fixture(
            Path(self.temp.name),
            with_asset=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def preflight(self):
        result = preflight_execution(
            batch=self.fixture.batch,
            manifest=self.fixture.manifest,
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            stage_result=self.fixture.stage_result,
            approval=self.fixture.approval,
            operations=self.fixture.operations,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=self.fixture.staged_entities,
            now=PREFLIGHT_AT,
        )
        self.assertTrue(result.passed, result.reason_codes)
        return result

    def rollback_approval(self, bundle):
        current = capture_test_target_snapshot(
            self.fixture.target.database_path,
            self.fixture.target.asset_root,
        )
        return make_approval_artifact(
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            target_snapshot_fingerprint=current.target_fingerprint,
            candidate_operation_ids=(
                *bundle.approved_operations,
                *bundle.rejected_operations,
            ),
            approved_operation_ids=bundle.approved_operations,
            rejected_operation_ids=bundle.rejected_operations,
            conflict_decisions=(),
            approval_scope="rollback_only",
            actor_kind="test_reviewer",
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )

    def test_crash_after_asset_promotion_recovers_by_exact_cleanup(self) -> None:
        preflight = self.preflight()
        with self.assertRaises(ExecutionInterrupted) as raised:
            execute_test_bundle(
                bundle=preflight.bundle,
                target=self.fixture.target,
                before_snapshot=self.fixture.before,
                staged_entities=self.fixture.staged_entities,
                fault_injector=lambda point: (
                    point == "after_asset_promoted"
                ),
            )
        report = recover_test_execution(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            lease_token=raised.exception.lease_token,
        )
        self.assertEqual("rolled_back", report.status)
        self.assertEqual(
            self.fixture.before.target_fingerprint,
            capture_test_target_snapshot(
                self.fixture.target.database_path,
                self.fixture.target.asset_root,
            ).target_fingerprint,
        )

    def test_crash_after_database_commit_recovers_to_completed(self) -> None:
        preflight = self.preflight()
        with self.assertRaises(ExecutionInterrupted) as raised:
            execute_test_bundle(
                bundle=preflight.bundle,
                target=self.fixture.target,
                before_snapshot=self.fixture.before,
                staged_entities=self.fixture.staged_entities,
                fault_injector=lambda point: (
                    point == "after_database_committed"
                ),
            )
        report = recover_test_execution(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            lease_token=raised.exception.lease_token,
        )
        self.assertEqual("completed", report.status)
        self.assertTrue(report.verification.passed)
        repeated = recover_test_execution(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            lease_token=raised.exception.lease_token,
        )
        self.assertEqual("already_completed", repeated.status)

    def test_completed_execution_can_be_explicitly_rolled_back(self) -> None:
        preflight = self.preflight()
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=self.fixture.staged_entities,
        )
        self.assertEqual("completed", result.status)
        report = rollback_test_execution(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            rollback_approval=self.rollback_approval(preflight.bundle),
            now=PREFLIGHT_AT,
        )
        self.assertEqual("rolled_back", report.status)
        self.assertTrue(report.database_restored)
        self.assertEqual(
            self.fixture.before.target_fingerprint,
            report.target_after_fingerprint,
        )

    def test_shared_preexisting_entity_is_not_deleted_by_rollback(self) -> None:
        stage_operation = self.fixture.stage_plan.operations[0]
        source = self.fixture.staged_entities[0].staged_path.read_bytes()
        final = self.fixture.target.asset_root / "final"
        final.mkdir(parents=True)
        shared = final / ("sha256_" + stage_operation.entity_digest)
        shared.write_bytes(source)
        self.fixture.before = capture_test_target_snapshot(
            self.fixture.target.database_path,
            self.fixture.target.asset_root,
        )
        self.fixture.approval, self.fixture.batch = approve_fixture(
            self.fixture,
            target_snapshot=self.fixture.before,
        )
        preflight = self.preflight()
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=self.fixture.staged_entities,
        )
        self.assertEqual((), result.created_asset_digests)
        self.assertEqual(
            (stage_operation.entity_digest,),
            result.reused_asset_digests,
        )
        report = rollback_test_execution(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            rollback_approval=self.rollback_approval(preflight.bundle),
            now=PREFLIGHT_AT,
        )
        self.assertEqual("rolled_back", report.status)
        self.assertTrue(shared.is_file())

    def test_rollback_requires_rollback_only_approval(self) -> None:
        preflight = self.preflight()
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=self.fixture.staged_entities,
        )
        self.assertEqual("completed", result.status)
        with self.assertRaisesRegex(
            Exception,
            "approval_scope_mismatch",
        ):
            rollback_test_execution(
                bundle=preflight.bundle,
                target=self.fixture.target,
                before_snapshot=self.fixture.before,
                import_plan=self.fixture.plan,
                stage_plan=self.fixture.stage_plan,
                rollback_approval=self.fixture.approval,
                now=PREFLIGHT_AT,
            )

    def test_tampered_journal_blocks_recovery_and_preserves_assets(self) -> None:
        preflight = self.preflight()
        with self.assertRaises(ExecutionInterrupted) as raised:
            execute_test_bundle(
                bundle=preflight.bundle,
                target=self.fixture.target,
                before_snapshot=self.fixture.before,
                staged_entities=self.fixture.staged_entities,
                fault_injector=lambda point: (
                    point == "after_asset_promoted"
                ),
            )
        execution_root = (
            self.fixture.target.root
            / "executions"
            / preflight.bundle.execution_id
        )
        journal = execution_root / "journal.jsonl"
        original = journal.read_text(encoding="utf-8")
        journal.write_text(
            original.replace(
                '"event_type":"asset_promoted"',
                '"event_type":"asset_rolled_back"',
                1,
            ),
            encoding="utf-8",
        )
        try:
            with self.assertRaisesRegex(
                IngestionExecutionError,
                "execution_journal_corrupt",
            ):
                recover_test_execution(
                    bundle=preflight.bundle,
                    target=self.fixture.target,
                    before_snapshot=self.fixture.before,
                    lease_token=raised.exception.lease_token,
                )
            digest = self.fixture.stage_plan.operations[0].entity_digest
            self.assertTrue(
                (
                    self.fixture.target.asset_root
                    / "final"
                    / ("sha256_" + digest)
                ).is_file()
            )
        finally:
            journal.write_text(original, encoding="utf-8")
            release_writer_lease(
                self.fixture.target,
                raised.exception.lease_token,
            )

    def test_unknown_file_prevents_rollback_preflight_and_is_preserved(self) -> None:
        preflight = self.preflight()
        execute_test_bundle(
            bundle=preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=self.fixture.staged_entities,
        )
        unknown = self.fixture.target.asset_root / "final" / "unknown.bin"
        unknown.write_bytes(b"unknown synthetic file")
        with self.assertRaisesRegex(
            Exception,
            "unexpected_asset_target_entry",
        ):
            self.rollback_approval(preflight.bundle)
        self.assertEqual(b"unknown synthetic file", unknown.read_bytes())


if __name__ == "__main__":
    unittest.main()
