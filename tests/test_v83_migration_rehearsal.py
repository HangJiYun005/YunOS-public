from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from memory.ingestion_migration_rehearsal import (
    approval_expiry,
    capture_file_gate,
    capture_legacy_compatibility,
    create_consistent_backup,
    persist_synthetic_integration,
    restore_from_backup,
    run_compatibility_suite,
)
from memory.ingestion_approval import make_approval_artifact
from memory.ingestion_execution import (
    execute_test_bundle,
    preflight_execution,
    rollback_test_execution,
)
from memory.ingestion_verification import capture_test_target_snapshot
from memory.ingestion_migrations import (
    apply_migration_to_clone,
    build_migration_plan,
    build_production_migration_approval_package,
    inspect_schema,
)
from tests.v83_migration_fixtures import create_legacy_database
from tests.v83_ingestion_execution_fixtures import (
    CREATED_AT,
    EXPIRES_AT,
    PREFLIGHT_AT,
    build_execution_fixture,
)


class MigrationRehearsalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83f-rehearsal-")
        self.root = Path(self.temp.name)
        self.source = create_legacy_database(self.root / "source.db")
        self.source_gate = capture_file_gate(self.source)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_online_backup_is_consistent_and_does_not_copy_sidecars(self) -> None:
        clone = self.root / "clone.db"
        result = create_consistent_backup(self.source, clone)
        self.assertTrue(result.source_gates_unchanged)
        self.assertTrue(result.logical_content_equal)
        self.assertEqual("ok", result.integrity_check)
        self.assertEqual(self.source_gate, capture_file_gate(self.source))
        self.assertFalse(Path(str(clone) + "-wal").exists())
        self.assertFalse(Path(str(clone) + "-shm").exists())

    def test_complete_clone_migration_and_compatibility_rehearsal(self) -> None:
        clone = self.root / "clone.db"
        create_consistent_backup(self.source, clone)
        before = capture_legacy_compatibility(clone)
        plan = build_migration_plan(inspect_schema(clone))
        result = apply_migration_to_clone(
            plan, clone, rehearsal_id="rehearsal_complete_001",
        )
        after = capture_legacy_compatibility(clone)
        compatibility = run_compatibility_suite(before, after)
        self.assertEqual("completed", result.status)
        self.assertTrue(compatibility["passed"], compatibility["checks"])

    def test_synthetic_chain_populates_every_audit_table_except_leases(self) -> None:
        clone = self.root / "synthetic.db"
        create_consistent_backup(self.source, clone)
        plan = build_migration_plan(inspect_schema(clone))
        apply_migration_to_clone(
            plan, clone, rehearsal_id="rehearsal_synthetic_001",
        )
        result = persist_synthetic_integration(
            clone,
            external_contract_digests={
                "adapter": "a" * 64,
                "planner": "b" * 64,
                "staging": "c" * 64,
                "approval": "d" * 64,
                "execution": "e" * 64,
                "verification": "f" * 64,
            },
        )
        self.assertTrue(result["passed"])
        self.assertTrue(result["legacy_data_unchanged"])
        self.assertEqual(0, result["table_record_counts"]["ingestion_leases"])
        self.assertTrue(all(
            count == 1
            for name, count in result["table_record_counts"].items()
            if name != "ingestion_leases"
        ))

    def test_verified_backup_restores_migrated_and_synthetic_clone(self) -> None:
        clone = self.root / "restore.db"
        backup = self.root / "restore-backup.db"
        create_consistent_backup(self.source, clone)
        create_consistent_backup(clone, backup)
        legacy_fingerprint = inspect_schema(clone).schema_fingerprint
        plan = build_migration_plan(inspect_schema(clone))
        apply_migration_to_clone(
            plan, clone, rehearsal_id="rehearsal_restore_001",
        )
        persist_synthetic_integration(clone)
        result = restore_from_backup(clone, backup)
        self.assertEqual("restored", result["status"])
        self.assertEqual(legacy_fingerprint, result["schema_fingerprint"])
        self.assertTrue(result["legacy_logical_content_equal"])

    def test_approval_package_is_sanitized_and_does_not_authorize(self) -> None:
        plan = build_migration_plan(inspect_schema(self.source))
        compatibility = {"passed": True, "private_text": False}
        failures = {"passed": 17, "failed": 0}
        rollback = {"passed": True}
        package = build_production_migration_approval_package(
            plan=plan,
            current_production_fingerprint=plan.source_schema_fingerprint,
            compatibility_summary=compatibility,
            failure_injection_summary=failures,
            rollback_summary=rollback,
            rehearsal_run_count=3,
            expires_at=approval_expiry(),
        )
        raw = json.dumps(package, sort_keys=True)
        self.assertFalse(package["production_execution_authorized"])
        self.assertEqual("awaiting_explicit_human_approval", package["status"])
        self.assertNotIn(str(self.root), raw)
        self.assertNotIn("tailnet", raw.lower())
        self.assertNotIn("cookie", raw.lower())
        self.assertNotIn("token", raw.lower())

    def test_real_a_through_e_synthetic_core_executes_verifies_and_rolls_back(self) -> None:
        fixture = build_execution_fixture(self.root / "e-core", with_asset=True)
        preflight = preflight_execution(
            batch=fixture.batch,
            manifest=fixture.manifest,
            import_plan=fixture.plan,
            stage_plan=fixture.stage_plan,
            stage_result=fixture.stage_result,
            approval=fixture.approval,
            operations=fixture.operations,
            target=fixture.target,
            before_snapshot=fixture.before,
            staged_entities=fixture.staged_entities,
            now=PREFLIGHT_AT,
        )
        self.assertTrue(preflight.passed, preflight.reason_codes)
        result = execute_test_bundle(
            bundle=preflight.bundle,
            target=fixture.target,
            before_snapshot=fixture.before,
            staged_entities=fixture.staged_entities,
        )
        self.assertEqual("completed", result.status)
        self.assertTrue(result.verification.passed)
        current = capture_test_target_snapshot(
            fixture.target.database_path, fixture.target.asset_root,
        )
        rollback_approval = make_approval_artifact(
            import_plan=fixture.plan,
            stage_plan=fixture.stage_plan,
            target_snapshot_fingerprint=current.target_fingerprint,
            candidate_operation_ids=(
                *preflight.bundle.approved_operations,
                *preflight.bundle.rejected_operations,
            ),
            approved_operation_ids=preflight.bundle.approved_operations,
            rejected_operation_ids=preflight.bundle.rejected_operations,
            conflict_decisions=(),
            approval_scope="rollback_only",
            actor_kind="test_reviewer",
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        rollback = rollback_test_execution(
            bundle=preflight.bundle,
            target=fixture.target,
            before_snapshot=fixture.before,
            import_plan=fixture.plan,
            stage_plan=fixture.stage_plan,
            rollback_approval=rollback_approval,
            now=PREFLIGHT_AT,
        )
        self.assertEqual("rolled_back", rollback.status)
        self.assertEqual(
            fixture.before.target_fingerprint, rollback.target_after_fingerprint,
        )


if __name__ == "__main__":
    unittest.main()
