from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_execution import (
    execute_test_bundle,
    make_execution_operation,
    preflight_execution,
)
from memory.ingestion_verification import (
    capture_test_target_snapshot,
    make_verification_expectation,
    verify_test_execution,
)
from tests.v83_ingestion_execution_fixtures import (
    PREFLIGHT_AT,
    approve_fixture,
    build_execution_fixture,
)


class IngestionVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = build_execution_fixture(Path(self.temp.name))
        self.preflight = preflight_execution(
            batch=self.fixture.batch,
            manifest=self.fixture.manifest,
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            stage_result=self.fixture.stage_result,
            approval=self.fixture.approval,
            operations=self.fixture.operations,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=(),
            now=PREFLIGHT_AT,
        )
        self.result = execute_test_bundle(
            bundle=self.preflight.bundle,
            target=self.fixture.target,
            before_snapshot=self.fixture.before,
            staged_entities=(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def verify(self, executed_ids=None):
        return verify_test_execution(
            execution_id=self.preflight.bundle.execution_id,
            database_path=self.fixture.target.database_path,
            asset_root=self.fixture.target.asset_root,
            before_snapshot=self.fixture.before,
            expectation=self.preflight.bundle.verification_expectations,
            executed_operation_ids=(
                executed_ids
                if executed_ids is not None
                else self.result.applied_operation_ids
            ),
            resolved_conflict_count=0,
        )

    def test_expected_delta_and_logical_rows_pass(self) -> None:
        _, verification = self.verify()
        self.assertTrue(verification.passed)
        self.assertTrue(
            verification.invariant_results["logical_rows_exact"]
        )

    def test_unplanned_database_record_is_detected(self) -> None:
        with closing(sqlite3.connect(
            self.fixture.target.database_path
        )) as connection:
            connection.execute(
                """INSERT INTO conversations(
                     conversation_id,source_identity,safe_metadata_json
                   ) VALUES(?,?,?)""",
                (
                    "conversation_" + "e" * 32,
                    "source_conversation_" + "e" * 64,
                    "{}",
                ),
            )
            connection.commit()
        _, verification = self.verify()
        self.assertFalse(verification.passed)
        self.assertFalse(
            verification.invariant_results["logical_rows_exact"]
        )
        self.assertFalse(
            verification.invariant_results["identity_sets_exact"]
        )

    def test_unplanned_asset_is_detected(self) -> None:
        final = self.fixture.target.asset_root / "final"
        final.mkdir(parents=True, exist_ok=True)
        payload = b"unexpected synthetic asset"
        digest = hashlib.sha256(payload).hexdigest()
        (final / ("sha256_" + digest)).write_bytes(payload)
        _, verification = self.verify()
        self.assertFalse(verification.passed)
        self.assertFalse(
            verification.invariant_results["asset_set_exact"]
        )

    def test_missing_approved_operation_is_detected(self) -> None:
        _, verification = self.verify(executed_ids=())
        self.assertFalse(verification.passed)
        self.assertFalse(
            verification.invariant_results[
                "approved_operations_complete"
            ]
        )

    def test_rejected_operation_reported_as_executed_is_detected(self) -> None:
        other = make_execution_operation(
            plan_action_id=self.fixture.operations[0].plan_action_id,
            operation_type="create_conversation",
            payload={
                "conversation_id": "conversation_" + "f" * 32,
                "source_identity": "source_conversation_" + "f" * 64,
            },
        )
        expectation = self.preflight.bundle.verification_expectations
        rejected_expectation = make_verification_expectation(
            expected_deltas=expectation.expected_deltas,
            approved_operation_ids=(
                self.fixture.operations[0].operation_id,
            ),
            rejected_operation_ids=(other.operation_id,),
            expected_conflict_resolutions=0,
            before_snapshot=self.fixture.before,
            expected_after_logical_digest=(
                expectation.expected_after_logical_digest
            ),
            approved_asset_digests=expectation.expected_asset_digests,
            expected_identity_additions=(
                expectation.expected_identity_additions
            ),
        )
        _, verification = verify_test_execution(
            execution_id=self.preflight.bundle.execution_id,
            database_path=self.fixture.target.database_path,
            asset_root=self.fixture.target.asset_root,
            before_snapshot=self.fixture.before,
            expectation=rejected_expectation,
            executed_operation_ids=(
                self.fixture.operations[0].operation_id,
                other.operation_id,
            ),
            resolved_conflict_count=0,
        )
        self.assertFalse(verification.passed)
        self.assertFalse(
            verification.invariant_results["rejected_operations_absent"]
        )

    def test_orphan_message_is_detected(self) -> None:
        with closing(sqlite3.connect(
            self.fixture.target.database_path
        )) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """INSERT INTO messages(
                     message_id,conversation_id,source_identity,content_hash,
                     safe_metadata_json
                   ) VALUES(?,?,?,?,?)""",
                (
                    "message_" + "d" * 32,
                    "conversation_" + "d" * 32,
                    "source_message_" + "d" * 64,
                    "d" * 64,
                    "{}",
                ),
            )
            connection.commit()
        _, verification = self.verify()
        self.assertFalse(verification.passed)
        self.assertFalse(
            verification.invariant_results["no_orphan_messages"]
        )

    def test_target_fingerprint_detects_sqlite_file_change(self) -> None:
        before_vacuum = capture_test_target_snapshot(
            self.fixture.target.database_path,
            self.fixture.target.asset_root,
        )
        with closing(sqlite3.connect(
            self.fixture.target.database_path
        )) as connection:
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        after_change = capture_test_target_snapshot(
            self.fixture.target.database_path,
            self.fixture.target.asset_root,
        )
        self.assertNotEqual(
            before_vacuum.target_fingerprint,
            after_change.target_fingerprint,
        )
        self.assertEqual(
            before_vacuum.logical_digest,
            after_change.logical_digest,
        )


if __name__ == "__main__":
    unittest.main()
