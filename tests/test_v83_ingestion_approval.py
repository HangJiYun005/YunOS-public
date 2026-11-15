from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from memory.ingestion_approval import (
    IngestionApprovalError,
    make_approval_artifact,
    make_conflict_decision,
    validate_approval_artifact,
)
from memory.ingestion_assets import build_asset_stage_plan
from memory.ingestion_core import normalized_json_digest
from memory.ingestion_planning import ImportPlan, make_import_conflict
from tests.v83_ingestion_execution_fixtures import (
    CREATED_AT,
    EXPIRES_AT,
    PREFLIGHT_AT,
    build_execution_fixture,
)


def rebuild_plan(plan: ImportPlan, **changes) -> ImportPlan:
    values = {
        "batch_id": plan.batch_id,
        "manifest_id": plan.manifest_id,
        "archive_sha256": plan.archive_sha256,
        "adapter_version": plan.adapter_version,
        "normalization_protocol": plan.normalization_protocol,
        "policy_version": plan.policy_version,
        "production_snapshot_digest": plan.production_snapshot_digest,
        "normalized_records_digest": plan.normalized_records_digest,
        "asset_inventory_digest": plan.asset_inventory_digest,
        "actions": plan.actions,
        "conflicts": plan.conflicts,
        "automatic_decisions": plan.automatic_decisions,
        "counts": plan.counts,
        "approval_required": plan.approval_required,
        "commit_prerequisites": plan.commit_prerequisites,
        "source_snapshot_complete": plan.source_snapshot_complete,
        "created_at": plan.created_at,
        "plan_schema_version": plan.plan_schema_version,
    }
    values.update(changes)
    identity = dict(plan.identity_payload())
    for key, value in changes.items():
        if key == "conflicts":
            identity[key] = [item.to_dict() for item in value]
        elif key == "actions":
            identity[key] = [item.to_dict() for item in value]
        elif key == "automatic_decisions":
            identity[key] = [
                {
                    item_key: item_value
                    for item_key, item_value in item.to_dict().items()
                    if item_key != "decided_at"
                }
                for item in value
            ]
        elif key != "created_at":
            identity[key] = value
    return ImportPlan(
        plan_id="plan_" + normalized_json_digest(identity),
        **values,
    )


class IngestionApprovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = build_execution_fixture(Path(self.temp.name))
        self.candidates = tuple(
            item.operation_id for item in self.fixture.operations
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_approval_binds_plan_stage_and_target(self) -> None:
        validate_approval_artifact(
            self.fixture.approval,
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            target_snapshot_fingerprint=self.fixture.before.target_fingerprint,
            candidate_operation_ids=self.candidates,
            now=PREFLIGHT_AT,
        )

    def test_same_inputs_produce_stable_digest(self) -> None:
        second = make_approval_artifact(
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            target_snapshot_fingerprint=self.fixture.before.target_fingerprint,
            candidate_operation_ids=self.candidates,
            approved_operation_ids=self.candidates,
            rejected_operation_ids=(),
            conflict_decisions=(),
            approval_scope="exact_plan",
            actor_kind="test_reviewer",
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        self.assertEqual(
            self.fixture.approval.approval_digest,
            second.approval_digest,
        )

    def test_candidate_order_does_not_change_digest(self) -> None:
        first = self.fixture.approval
        second = make_approval_artifact(
            import_plan=self.fixture.plan,
            stage_plan=self.fixture.stage_plan,
            target_snapshot_fingerprint=self.fixture.before.target_fingerprint,
            candidate_operation_ids=tuple(reversed(self.candidates)),
            approved_operation_ids=tuple(reversed(self.candidates)),
            rejected_operation_ids=(),
            conflict_decisions=(),
            approval_scope="exact_plan",
            actor_kind="test_reviewer",
            created_at=CREATED_AT,
            expires_at=EXPIRES_AT,
        )
        self.assertEqual(first.approval_digest, second.approval_digest)

    def test_expired_approval_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            IngestionApprovalError,
            "approval_expired",
        ):
            validate_approval_artifact(
                self.fixture.approval,
                import_plan=self.fixture.plan,
                stage_plan=self.fixture.stage_plan,
                target_snapshot_fingerprint=(
                    self.fixture.before.target_fingerprint
                ),
                candidate_operation_ids=self.candidates,
                now=EXPIRES_AT,
            )

    def test_target_change_invalidates_approval(self) -> None:
        with self.assertRaisesRegex(
            IngestionApprovalError,
            "approval_target_stale",
        ):
            validate_approval_artifact(
                self.fixture.approval,
                import_plan=self.fixture.plan,
                stage_plan=self.fixture.stage_plan,
                target_snapshot_fingerprint="b" * 64,
                candidate_operation_ids=self.candidates,
                now=PREFLIGHT_AT,
            )

    def test_operation_whitelist_must_partition_exact_candidates(self) -> None:
        with self.assertRaisesRegex(
            IngestionApprovalError,
            "approval_operation_partition_incomplete",
        ):
            make_approval_artifact(
                import_plan=self.fixture.plan,
                stage_plan=self.fixture.stage_plan,
                target_snapshot_fingerprint=(
                    self.fixture.before.target_fingerprint
                ),
                candidate_operation_ids=self.candidates,
                approved_operation_ids=(),
                rejected_operation_ids=(),
                conflict_decisions=(),
                approval_scope="exact_plan",
                actor_kind="test_reviewer",
                created_at=CREATED_AT,
                expires_at=EXPIRES_AT,
            )

    def test_unplanned_operation_cannot_be_approved(self) -> None:
        with self.assertRaisesRegex(
            IngestionApprovalError,
            "approval_contains_unplanned_operation",
        ):
            make_approval_artifact(
                import_plan=self.fixture.plan,
                stage_plan=self.fixture.stage_plan,
                target_snapshot_fingerprint=(
                    self.fixture.before.target_fingerprint
                ),
                candidate_operation_ids=self.candidates,
                approved_operation_ids=(
                    *self.candidates,
                    "execop_" + "f" * 64,
                ),
                rejected_operation_ids=(),
                conflict_decisions=(),
                approval_scope="exact_plan",
                actor_kind="test_reviewer",
                created_at=CREATED_AT,
                expires_at=EXPIRES_AT,
            )

    def test_scope_is_exact_not_wildcard(self) -> None:
        with self.assertRaisesRegex(
            IngestionApprovalError,
            "invalid_approval_scope",
        ):
            make_approval_artifact(
                import_plan=self.fixture.plan,
                stage_plan=self.fixture.stage_plan,
                target_snapshot_fingerprint=(
                    self.fixture.before.target_fingerprint
                ),
                candidate_operation_ids=self.candidates,
                approved_operation_ids=self.candidates,
                rejected_operation_ids=(),
                conflict_decisions=(),
                approval_scope="all_future_plans",
                actor_kind="test_reviewer",
                created_at=CREATED_AT,
                expires_at=EXPIRES_AT,
            )

    def test_high_risk_conflict_requires_explicit_actor(self) -> None:
        action = self.fixture.plan.actions[0]
        conflict = make_import_conflict(
            batch_id=self.fixture.plan.batch_id,
            record_kind=action.record_kind,
            source_identity=action.source_identity,
            production_identity_digest=None,
            conflict_type="content_changed",
            evidence_hashes=("c" * 64,),
            risk_level="high",
            allowed_decisions=("accept_incoming", "reject"),
        )
        plan = rebuild_plan(
            self.fixture.plan,
            conflicts=(conflict,),
            approval_required=True,
        )
        stage = build_asset_stage_plan(
            import_plan=plan,
            attachments=(),
            asset_entities=(),
        )
        decision = make_conflict_decision(
            conflict=conflict,
            selected_action="accept_incoming",
            allowed_operation_ids=self.candidates,
            rejected_operation_ids=(),
            decision_reason_code="explicit_test_decision",
            actor_kind="automatic",
        )
        with self.assertRaisesRegex(
            IngestionApprovalError,
            "high_risk_conflict_requires_explicit_actor",
        ):
            make_approval_artifact(
                import_plan=plan,
                stage_plan=stage,
                target_snapshot_fingerprint=(
                    self.fixture.before.target_fingerprint
                ),
                candidate_operation_ids=self.candidates,
                approved_operation_ids=self.candidates,
                rejected_operation_ids=(),
                conflict_decisions=(decision,),
                approval_scope="exact_plan",
                actor_kind="test_reviewer",
                created_at=CREATED_AT,
                expires_at=EXPIRES_AT,
            )

    def test_unresolved_conflict_blocks_exact_approval(self) -> None:
        action = self.fixture.plan.actions[0]
        conflict = make_import_conflict(
            batch_id=self.fixture.plan.batch_id,
            record_kind=action.record_kind,
            source_identity=action.source_identity,
            production_identity_digest=None,
            conflict_type="legacy_binding",
            evidence_hashes=("d" * 64,),
            risk_level="medium",
            allowed_decisions=("keep_existing", "reject"),
        )
        plan = rebuild_plan(
            self.fixture.plan,
            conflicts=(conflict,),
            approval_required=True,
        )
        stage = build_asset_stage_plan(
            import_plan=plan,
            attachments=(),
            asset_entities=(),
        )
        with self.assertRaisesRegex(
            IngestionApprovalError,
            "required_conflict_unresolved",
        ):
            make_approval_artifact(
                import_plan=plan,
                stage_plan=stage,
                target_snapshot_fingerprint=(
                    self.fixture.before.target_fingerprint
                ),
                candidate_operation_ids=self.candidates,
                approved_operation_ids=self.candidates,
                rejected_operation_ids=(),
                conflict_decisions=(),
                approval_scope="exact_plan",
                actor_kind="test_reviewer",
                created_at=CREATED_AT,
                expires_at=EXPIRES_AT,
            )


if __name__ == "__main__":
    unittest.main()
