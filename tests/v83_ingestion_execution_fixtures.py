from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from memory.ingestion_adapters.chatgpt import (
    ChatGPTAdapter,
    DETERMINISTIC_TIMESTAMP,
    empty_target_snapshot,
)
from memory.ingestion_approval import ApprovalArtifact, make_approval_artifact
from memory.ingestion_assets import (
    AssetStagePlan,
    AssetStageResult,
    build_asset_stage_plan,
)
from memory.ingestion_core import ImportBatch, create_import_batch
from memory.ingestion_execution import (
    ExecutionOperation,
    StagedEntitySource,
    TestExecutionTarget,
    initialize_test_execution_target,
    make_execution_operation,
)
from memory.ingestion_planning import ImportPlan
from memory.ingestion_verification import (
    TestTargetSnapshot,
    capture_test_target_snapshot,
)
from tests.test_v83_asset_stage_plan import asset_part
from tests.v83_chatgpt_adapter_fixtures import (
    PNG,
    simple_conversation,
    write_zip_source,
)


CREATED_AT = "2026-07-28T00:00:00.000000Z"
PREFLIGHT_AT = "2026-07-28T01:00:00.000000Z"
EXPIRES_AT = "2026-07-29T00:00:00.000000Z"


@dataclass
class ExecutionFixture:
    root: Path
    manifest: object
    plan: ImportPlan
    stage_plan: AssetStagePlan
    stage_result: AssetStageResult
    target: TestExecutionTarget
    before: TestTargetSnapshot
    operations: tuple[ExecutionOperation, ...]
    staged_entities: tuple[StagedEntitySource, ...]
    approval: ApprovalArtifact
    batch: ImportBatch


def _stage_result(plan: AssetStagePlan) -> AssetStageResult:
    return AssetStageResult(
        batch_id=plan.batch_id,
        stage_plan_digest=plan.stage_plan_digest,
        status="staged",
        entities_staged=len(plan.operations),
        entities_reused=0,
        entities_quarantined=0,
        bytes_processed=sum(item.actual_size for item in plan.operations),
        asset_stage_fingerprint=plan.asset_stage_fingerprint,
        warning_codes=(),
        reason_codes=(),
        ledger_digest=("b" * 64 if plan.operations else None),
        verification_digest="a" * 64,
    )


def approve_fixture(
    fixture: ExecutionFixture,
    *,
    operations: Sequence[ExecutionOperation] | None = None,
    approved_ids: Sequence[str] | None = None,
    rejected_ids: Sequence[str] = (),
    scope: str = "exact_plan",
    target_snapshot: TestTargetSnapshot | None = None,
    created_at: str = CREATED_AT,
    expires_at: str = EXPIRES_AT,
) -> tuple[ApprovalArtifact, ImportBatch]:
    selected_operations = tuple(operations or fixture.operations)
    candidate = {
        item.operation_id for item in selected_operations
    } | {
        item.operation_id for item in fixture.stage_plan.operations
    }
    approved = tuple(
        approved_ids if approved_ids is not None else sorted(candidate)
    )
    approval = make_approval_artifact(
        import_plan=fixture.plan,
        stage_plan=fixture.stage_plan,
        target_snapshot_fingerprint=(
            target_snapshot or fixture.before
        ).target_fingerprint,
        candidate_operation_ids=tuple(candidate),
        approved_operation_ids=approved,
        rejected_operation_ids=tuple(rejected_ids),
        conflict_decisions=(),
        approval_scope=scope,
        actor_kind="test_reviewer",
        created_at=created_at,
        expires_at=expires_at,
    )
    return approval, replace(
        fixture.batch,
        current_state="approved",
        approval_digest=approval.approval_digest,
    )


def build_execution_fixture(
    root: Path,
    *,
    with_asset: bool = False,
) -> ExecutionFixture:
    root.mkdir(parents=True, exist_ok=True)
    if with_asset:
        staged_file = root / "verified-entity.bin"
        staged_file.write_bytes(PNG)
        source = write_zip_source(
            root,
            [
                simple_conversation(
                    assistant_parts=[
                        asset_part(
                            "asset-1",
                            "assets/image.png",
                            PNG,
                            "image/png",
                        )
                    ]
                )
            ],
            assets={"assets/image.png": PNG},
        )
    else:
        staged_file = None
        source = write_zip_source(root, [simple_conversation()])
    result = ChatGPTAdapter(
        source,
        timestamp=DETERMINISTIC_TIMESTAMP,
    ).produce_import_plan(
        target_snapshot=empty_target_snapshot(),
        batch_mode="full",
    )
    plan = result.import_plan
    stage_plan = build_asset_stage_plan(
        import_plan=plan,
        attachments=result.bundle.attachments if with_asset else (),
        asset_entities=result.bundle.asset_entities if with_asset else (),
    )
    target = initialize_test_execution_target(root / "execution-target")
    before = capture_test_target_snapshot(
        target.database_path,
        target.asset_root,
    )
    conversation_action = next(
        item for item in plan.actions if item.record_kind == "conversation"
    )
    conversation = make_execution_operation(
        plan_action_id=conversation_action.action_id,
        operation_type="create_conversation",
        payload={
            "conversation_id": "conversation_" + "1" * 32,
            "source_identity": conversation_action.source_identity,
            "safe_metadata": {"fixture": "safe"},
        },
    )
    operations: list[ExecutionOperation] = [conversation]
    staged_entities: list[StagedEntitySource] = []
    if with_asset:
        stage_operation = stage_plan.operations[0]
        asset_action = next(
            item for item in plan.actions if item.record_kind == "asset"
        )
        register = make_execution_operation(
            plan_action_id=asset_action.action_id,
            operation_type="register_asset_entity",
            payload={
                "entity_digest": stage_operation.entity_digest,
                "size_bytes": stage_operation.actual_size,
                "mime": stage_operation.detected_mime,
            },
            dependency_operation_ids=(stage_operation.operation_id,),
        )
        operations.append(register)
        staged_entities.append(StagedEntitySource(
            operation_id=stage_operation.operation_id,
            entity_digest=stage_operation.entity_digest,
            actual_size=stage_operation.actual_size,
            detected_mime=stage_operation.detected_mime,
            staged_path=staged_file,
        ))
    batch = create_import_batch(
        result.manifest,
        batch_mode="full",
        plan=plan.identity_payload(),
        timestamp=DETERMINISTIC_TIMESTAMP,
        batch_id_factory=lambda: plan.batch_id,
    )
    fixture = ExecutionFixture(
        root=root,
        manifest=result.manifest,
        plan=plan,
        stage_plan=stage_plan,
        stage_result=_stage_result(stage_plan),
        target=target,
        before=before,
        operations=tuple(operations),
        staged_entities=tuple(staged_entities),
        approval=None,  # type: ignore[arg-type]
        batch=batch,
    )
    fixture.approval, fixture.batch = approve_fixture(fixture)
    return fixture
