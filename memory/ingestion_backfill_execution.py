"""Clone-only historical backfill execution for YunOS v8.3-G.

Historical operations use the unified v8.3-E ``ApprovalArtifact``,
``ExecutionBundle``, execution entry point, journal, writer lease, and
verification entry point.  This module supplies only the v8302 operation
handlers and compatibility snapshot policy.  A SQLite authorizer denies every
write outside the active v8302 ingestion tables.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import re
import sqlite3
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from memory.ingestion_approval import (
    ApprovalArtifact,
    make_approval_artifact,
    make_conflict_decision,
    validate_approval_artifact,
)
from memory.ingestion_assets import (
    ASSET_STAGE_PLAN_SCHEMA_VERSION,
    AssetStagePlan,
)
from memory.ingestion_backfill import (
    HistoricalAttachmentBindingCandidate,
    HistoricalBackfillError,
    HistoricalBackfillPlan,
    HistoricalBackfillResult,
    HistoricalBindingCandidate,
)
from memory.ingestion_core import (
    BatchState,
    ImportBatch,
    bind_batch_approval,
    build_asset_stage_fingerprint,
    make_plan_digest,
    normalized_json_digest,
    transition_batch,
)
from memory.ingestion_execution import (
    EXECUTION_BUNDLE_SCHEMA_VERSION,
    ExecutionBundle,
    ExecutionOperation,
    TestExecutionTarget,
    acquire_writer_lease,
    append_execution_journal_event,
    execute_test_bundle,
    make_execution_operation,
    release_writer_lease,
)
from memory.ingestion_historical_schema import (
    HISTORICAL_ATTACHMENT_BINDING_SCHEMA_VERSION,
    HISTORICAL_SOURCE_BINDING_SCHEMA_VERSION,
    UNIFIED_V2_TABLES,
    HistoricalSourceManifest,
    ManifestSubject,
    UnifiedAuthorityRepository,
    acquire_unified_writer_lease,
    append_unified_execution_journal_event,
    canonical_historical_json,
    execute_historical_operation_handler,
    validate_unified_approval,
    verify_unified_execution,
)
from memory.ingestion_migration_rehearsal import (
    capture_legacy_compatibility,
    create_consistent_backup,
    restore_from_backup,
    run_compatibility_suite,
)
from memory.ingestion_migrations import (
    HISTORICAL_MIGRATION_DIGEST,
    HISTORICAL_MIGRATION_VERSION,
    verify_historical_migrated_schema,
)
from memory.ingestion_verification import (
    COUNT_KEYS,
    EXECUTION_VERIFICATION_VERSION,
    VERIFICATION_EXPECTATION_VERSION,
    ExecutionVerificationResult,
    VerificationExpectation,
    verify_test_execution,
)


HISTORICAL_EXECUTION_SCHEMA_VERSION = "v8.3-g-historical-execution-1"
HISTORICAL_EXECUTION_EVENT_VERSION = "v8.3-g-historical-execution-event-1"
HISTORICAL_VERIFICATION_SNAPSHOT_VERSION = "v8.3-g-verification-snapshot-1"

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_DB = (_PROJECT_ROOT / "data" / "ayunos_chatgpt.db").resolve()
_PRODUCTION_DATA = (_PROJECT_ROOT / "data").resolve()
_PRODUCTION_IMPORTS = (_PROJECT_ROOT / "imports").resolve()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _plus_hours(value: str, hours: int) -> str:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return (
        (parsed + timedelta(hours=hours))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_historical_clone_target(value: str | Path) -> Path:
    target = Path(value).resolve(strict=True)
    if (
        not target.is_file()
        or target.is_symlink()
        or target == _PRODUCTION_DB
        or _is_relative_to(target, _PRODUCTION_DATA)
        or _is_relative_to(target, _PRODUCTION_IMPORTS)
        or target.name != "target.sqlite"
    ):
        raise HistoricalBackfillError("production_path_forbidden")
    root = target.parent
    allowed_output = (
        _PROJECT_ROOT
        / "output"
        / "v8.3-g-historical-backfill-compatibility"
    ).resolve()
    temp_root = Path(__import__("tempfile").gettempdir()).resolve()
    if not (
        _is_relative_to(root, temp_root)
        or _is_relative_to(root, allowed_output)
    ):
        raise HistoricalBackfillError("clone_path_outside_allowed_boundary")
    return target


def assert_unified_historical_authority() -> None:
    """Fail closed if a historical authority alias diverges from v8.3-E."""

    if (
        validate_unified_approval is not validate_approval_artifact
        or append_unified_execution_journal_event
        is not append_execution_journal_event
        or acquire_unified_writer_lease is not acquire_writer_lease
        or verify_unified_execution is not verify_test_execution
        or not callable(execute_test_bundle)
    ):
        raise HistoricalBackfillError("unified_authority_alias_mismatch")


def _execution_target(
    clone: Path,
    plan: HistoricalBackfillPlan,
) -> TestExecutionTarget:
    assets = clone.parent / "assets"
    assets.mkdir(exist_ok=True)
    target_id = "testtarget_" + normalized_json_digest({
        "clone_target": plan.target_snapshot_fingerprint,
        "batch_id": plan.unified_batch_id,
    })[:32]
    identity = normalized_json_digest({"test_target_id": target_id})
    root_fingerprint = normalized_json_digest({
        "target_identity_digest": identity,
        "schema_version": HISTORICAL_EXECUTION_SCHEMA_VERSION,
        "node": "v8.3-g-historical-backfill-compatibility",
    })
    return TestExecutionTarget(
        target_id=target_id,
        target_identity_digest=identity,
        execution_root_fingerprint=root_fingerprint,
        root=clone.parent,
        database_path=clone,
        asset_root=assets,
    )


def _legacy_logical_contract_digest(
    contract: Mapping[str, Any],
) -> str:
    return normalized_json_digest({
        key: contract[key]
        for key in (
            "row_counts",
            "id_set_digests",
            "critical_field_digests",
            "source_values_digest",
            "attachment_json_digest",
            "desktop_query_digest",
            "fts_contract_digest",
            "message_context_digest",
            "conversation_messages_digest",
        )
    })


def build_empty_asset_stage_plan(
    plan: HistoricalBackfillPlan,
) -> AssetStagePlan:
    fingerprint = build_asset_stage_fingerprint(())
    inventory_digest = normalized_json_digest({
        "historical_backfill": plan.plan_digest,
        "asset_entities": [],
    })
    identity = {
        "batch_id": plan.batch_id,
        "manifest_id": plan.manifest_id,
        "import_plan_id": plan.plan_id,
        "import_plan_digest": plan.plan_digest,
        "asset_inventory_digest": inventory_digest,
        "operations": [],
        "occurrence_bindings": {},
        "expected_asset_delta": {
            "entity_count": 0,
            "occurrence_count": 0,
            "physical_bytes": 0,
            "logical_bytes": 0,
        },
        "asset_stage_fingerprint": fingerprint.to_dict(),
        "warning_codes": [],
        "plan_schema_version": ASSET_STAGE_PLAN_SCHEMA_VERSION,
    }
    digest = make_plan_digest(identity)
    return AssetStagePlan(
        stage_plan_id="astage_" + digest,
        stage_plan_digest=digest,
        batch_id=plan.batch_id,
        manifest_id=plan.manifest_id,
        import_plan_id=plan.plan_id,
        import_plan_digest=plan.plan_digest,
        asset_inventory_digest=inventory_digest,
        operations=(),
        occurrences=(),
        entity_descriptors=(),
        occurrence_bindings={},
        expected_asset_delta=identity["expected_asset_delta"],
        asset_stage_fingerprint=fingerprint,
        warning_codes=(),
    )


def _candidate_operation(
    candidate: HistoricalBindingCandidate,
) -> ExecutionOperation:
    operation_type = {
        "native_authoritative": "verify_native_source_binding",
        "deterministic_legacy": "create_legacy_source_binding",
        "probable_legacy": "flag_probable_binding",
        "conflicting_evidence": "record_backfill_conflict",
        "unsupported_source": "record_backfill_conflict",
        "invalid_relationship": "record_backfill_conflict",
    }.get(candidate.provenance_kind, "record_legacy_unknown")
    return make_execution_operation(
        plan_action_id="planaction_" + candidate.binding_digest,
        operation_type=operation_type,
        payload={
            "candidate_id": candidate.candidate_id,
            "target_record_kind": candidate.target_record_kind,
            "target_record_ref": candidate.target_record_ref,
            "proposed_source_type": candidate.proposed_source_type,
            "source_record_id_digest": candidate.source_record_id_digest,
            "provenance_kind": candidate.provenance_kind,
            "evidence_digest": normalized_json_digest(
                list(candidate.evidence_ids)
            ),
            "binding_digest": candidate.binding_digest,
            "requires_approval": candidate.requires_approval,
            "conflict_state": candidate.conflict_state,
        },
    )


def _attachment_operations(
    candidate: HistoricalAttachmentBindingCandidate,
) -> tuple[ExecutionOperation, ExecutionOperation]:
    create = make_execution_operation(
        plan_action_id="planaction_" + candidate.binding_digest,
        operation_type="create_historical_attachment_binding",
        payload={
            "candidate_id": candidate.candidate_id,
            "logical_media_id": candidate.logical_media_id,
            "source_attachment_id_digest": (
                candidate.source_attachment_id_digest
            ),
            "message_id": candidate.message_id,
            "entity_digest": None,
            "attachment_state": candidate.attachment_state,
            "size_conflict_state": candidate.size_conflict_state,
            "provenance_kind": candidate.provenance_kind,
            "evidence_digest": candidate.evidence_digest,
            "occurrence_index": candidate.occurrence_index,
            "binding_digest": candidate.binding_digest,
        },
    )
    verify = make_execution_operation(
        plan_action_id="planaction_" + normalized_json_digest({
            "media_verification": candidate.binding_digest,
        }),
        operation_type="verify_media_identity",
        payload={
            "logical_media_id": candidate.logical_media_id,
            "message_id_digest": normalized_json_digest({
                "message_id": candidate.message_id,
            }),
            "occurrence_index": candidate.occurrence_index,
            "evidence_digest": candidate.evidence_digest,
            "entity_digest": None,
            "size_conflict_state": candidate.size_conflict_state,
        },
        dependency_operation_ids=(create.operation_id,),
    )
    return create, verify


def build_historical_operations(
    plan: HistoricalBackfillPlan,
) -> tuple[ExecutionOperation, ...]:
    operations: list[ExecutionOperation] = [
        _candidate_operation(candidate)
        for candidate in plan.binding_candidates
    ]
    for candidate in plan.attachment_binding_candidates:
        operations.extend(_attachment_operations(candidate))
    for conflict in plan.conflicts:
        operations.append(make_execution_operation(
            plan_action_id="planaction_" + conflict.conflict_id.removeprefix(
                "hconflict_"
            ),
            operation_type="record_backfill_conflict",
            payload={
                "conflict_id": conflict.conflict_id,
                "conflict_type": conflict.conflict_type,
                "severity": conflict.severity,
                "candidate_refs": list(conflict.candidate_refs),
                "evidence_digests": list(conflict.evidence_digests),
                "proposed_actions": list(conflict.proposed_actions),
                "default_action": conflict.default_action,
                "requires_approval": conflict.requires_approval,
                "reason_code": conflict.reason_code,
            },
        ))
    return tuple(sorted(operations, key=lambda item: item.operation_id))


def build_historical_approval(
    plan: HistoricalBackfillPlan,
    operations: Sequence[ExecutionOperation],
    *,
    created_at: str,
    expires_at: str,
    actor_kind: str = "test_operator",
) -> tuple[AssetStagePlan, ApprovalArtifact]:
    stage_plan = build_empty_asset_stage_plan(plan)
    candidates = tuple(item.operation_id for item in operations)
    rejected = {
        item.operation_id
        for item in operations
        if item.operation_type == "flag_probable_binding"
    }
    approved = set(candidates) - rejected
    conflict_operation_ids: dict[str, list[str]] = {}
    for operation in operations:
        conflict_id = operation.payload.get("conflict_id")
        if isinstance(conflict_id, str):
            conflict_operation_ids.setdefault(conflict_id, []).append(
                operation.operation_id
            )
    decisions = tuple(
        make_conflict_decision(
            conflict=conflict,
            selected_action=conflict.default_action,
            allowed_operation_ids=tuple(
                conflict_operation_ids.get(conflict.conflict_id, ())
            ),
            rejected_operation_ids=(),
            decision_reason_code="conflict_default_block",
            actor_kind=actor_kind,
        )
        for conflict in plan.conflicts
    )
    approval = make_approval_artifact(
        import_plan=plan,
        stage_plan=stage_plan,
        target_snapshot_fingerprint=plan.target_snapshot_fingerprint,
        candidate_operation_ids=candidates,
        approved_operation_ids=tuple(approved),
        rejected_operation_ids=tuple(rejected),
        conflict_decisions=decisions,
        approval_scope="exact_plan",
        actor_kind=actor_kind,
        created_at=created_at,
        expires_at=expires_at,
    )
    validate_unified_approval(
        approval,
        import_plan=plan,
        stage_plan=stage_plan,
        target_snapshot_fingerprint=plan.target_snapshot_fingerprint,
        candidate_operation_ids=candidates,
        now=created_at,
    )
    return stage_plan, approval


def _verification_expectation(
    plan: HistoricalBackfillPlan,
    approval: ApprovalArtifact,
) -> VerificationExpectation:
    payload = {
        "expected_deltas": {key: 0 for key in COUNT_KEYS},
        "approved_operation_ids": list(approval.approved_operation_ids),
        "rejected_operation_ids": list(approval.rejected_operation_ids),
        "expected_conflict_resolutions": 0,
        "database_before_fingerprint": plan.target_snapshot_fingerprint,
        "asset_before_fingerprint": normalized_json_digest({
            "historical_assets": "unchanged",
        }),
        "expected_after_logical_digest": plan.verification_expectations[
            "legacy_logical_contract_digest"
        ],
        "expected_asset_digests": [],
        "expected_identity_additions": {},
        "expectation_schema_version": VERIFICATION_EXPECTATION_VERSION,
    }
    return VerificationExpectation(
        expectation_digest=normalized_json_digest(payload),
        **payload,
    )


def build_historical_execution_bundle(
    plan: HistoricalBackfillPlan,
    approval: ApprovalArtifact,
    stage_plan: AssetStagePlan,
    operations: Sequence[ExecutionOperation],
    target: TestExecutionTarget,
    *,
    database_backup_fingerprint: str,
) -> ExecutionBundle:
    expectation = _verification_expectation(plan, approval)
    payload = {
        "batch_id": plan.batch_id,
        "manifest_id": plan.manifest_id,
        "plan_digest": plan.plan_digest,
        "stage_plan_digest": stage_plan.stage_plan_digest,
        "approval_digest": approval.approval_digest,
        "target_snapshot_fingerprint": plan.target_snapshot_fingerprint,
        "approved_operations": list(approval.approved_operation_ids),
        "rejected_operations": list(approval.rejected_operation_ids),
        "database_operations": [
            item.to_dict()
            for item in sorted(operations, key=lambda value: value.operation_id)
        ],
        "approved_assets": [],
        "verification_expectations": expectation.to_dict(),
        "database_backup_fingerprint": database_backup_fingerprint,
        "execution_root_fingerprint": target.execution_root_fingerprint,
        "conflict_decisions": [],
        "execution_schema_version": EXECUTION_BUNDLE_SCHEMA_VERSION,
    }
    digest = normalized_json_digest(payload)
    return ExecutionBundle(
        execution_id="execution_" + digest,
        database_operations=tuple(operations),
        approved_assets=(),
        verification_expectations=expectation,
        conflict_decisions=(),
        **{
            key: value
            for key, value in payload.items()
            if key not in {
                "database_operations",
                "approved_assets",
                "verification_expectations",
                "conflict_decisions",
                "execution_schema_version",
            }
        },
    )


def _insert_manifest(
    connection: sqlite3.Connection,
    manifest: HistoricalSourceManifest,
) -> None:
    connection.execute(
        """
        INSERT INTO ingestion_historical_manifests VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            manifest.historical_manifest_id,
            manifest.source_type,
            manifest.provenance_kind,
            manifest.archive_state,
            manifest.archive_sha256,
            manifest.archive_size,
            manifest.adapter_name,
            manifest.adapter_version,
            manifest.evidence_policy_version,
            manifest.evidence_digest,
            manifest.reconstruction_basis_digest,
            manifest.validation_status,
            canonical_historical_json(dict(manifest.record_counts)),
            canonical_historical_json(dict(manifest.asset_counts)),
            canonical_historical_json(list(manifest.warnings)),
            manifest.created_at,
            manifest.manifest_schema_version,
        ),
    )


def _insert_approval(
    connection: sqlite3.Connection,
    plan: HistoricalBackfillPlan,
    subject: ManifestSubject,
    approval: ApprovalArtifact,
) -> None:
    connection.execute(
        """
        INSERT INTO ingestion_approvals_v2 VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            approval.approval_id,
            approval.batch_id,
            subject.manifest_subject_id,
            approval.plan_id,
            approval.plan_digest,
            approval.stage_plan_id,
            approval.stage_plan_digest,
            approval.target_snapshot_fingerprint,
            approval.approval_digest,
            approval.approval_scope,
            approval.actor_kind,
            approval.created_at,
            approval.expires_at,
            approval.approval_schema_version,
        ),
    )


def _execution_event(
    connection: sqlite3.Connection,
    *,
    execution_id: str,
    sequence: int,
    event_type: str,
    previous_state: str,
    next_state: str,
    occurred_at: str,
    reason_code: str,
    previous_digest: str,
) -> str:
    payload_digest = normalized_json_digest({
        "reason_code": reason_code,
        "event_type": event_type,
    })
    payload = {
        "execution_id": execution_id,
        "sequence": sequence,
        "event_type": event_type,
        "previous_state": previous_state,
        "next_state": next_state,
        "occurred_at": occurred_at,
        "actor_kind": "test_operator",
        "reason_code": reason_code,
        "payload_digest": payload_digest,
        "previous_event_digest": previous_digest,
        "event_schema_version": HISTORICAL_EXECUTION_EVENT_VERSION,
    }
    digest = normalized_json_digest(payload)
    connection.execute(
        """
        INSERT INTO ingestion_execution_events_v2 VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            execution_id,
            sequence,
            "event_" + digest,
            event_type,
            previous_state,
            next_state,
            occurred_at,
            "test_operator",
            reason_code,
            payload_digest,
            previous_digest,
            digest,
            HISTORICAL_EXECUTION_EVENT_VERSION,
        ),
    )
    return digest


def _transition(
    repository: UnifiedAuthorityRepository,
    batch: ImportBatch,
    state: BatchState,
    *,
    occurred_at: str,
    previous_digest: str,
    reason_code: str,
) -> tuple[ImportBatch, str]:
    kwargs: dict[str, Any] = {}
    if state is BatchState.COMMITTING:
        kwargs = {
            "supplied_approval_digest": batch.approval_digest,
            "current_manifest_id": batch.manifest_id,
        }
    updated, event = transition_batch(
        batch,
        state,
        actor_kind="test_operator",
        reason_code=reason_code,
        occurred_at=occurred_at,
        previous_event_digest=previous_digest,
        **kwargs,
    )
    repository.append_event(event)
    repository.update_batch_snapshot(updated)
    return updated, event.event_digest


def _write_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger: str | None,
) -> int:
    del arg2, db_name, trigger
    write_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
    }
    if action in write_actions and str(arg1 or "") not in UNIFIED_V2_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _existing_exact(
    connection: sqlite3.Connection,
    table: str,
    identity_column: str,
    identity: str,
    expected: Sequence[Any],
) -> bool:
    row = connection.execute(
        f'SELECT * FROM "{table}" WHERE "{identity_column}"=?',
        (identity,),
    ).fetchone()
    return row is not None and tuple(row) == tuple(expected)


def _safe_failure_reason(
    exc: BaseException,
    *,
    phase: str = "execution",
) -> str:
    declared = getattr(exc, "reason_code", None)
    if isinstance(declared, str) and declared:
        return declared
    if isinstance(exc, sqlite3.IntegrityError):
        safe_phase = re.sub(r"[^a-z0-9_]", "_", phase.lower()).strip("_")
        if not safe_phase:
            safe_phase = "execution"
        return f"historical_integrity_{safe_phase}_failed"
    if isinstance(exc, sqlite3.DatabaseError):
        safe_phase = re.sub(r"[^a-z0-9_]", "_", phase.lower()).strip("_")
        if not safe_phase:
            safe_phase = "execution"
        return f"historical_database_{safe_phase}_failed"
    if isinstance(exc, KeyError):
        return "historical_payload_contract_failed"
    if isinstance(exc, (TypeError, AttributeError)):
        return "historical_type_contract_failed"
    if isinstance(exc, AssertionError):
        return "historical_runtime_assertion_failed"
    if isinstance(exc, OSError):
        return "historical_io_failed"
    return "historical_execution_failed"


def _apply_operation(
    connection: sqlite3.Connection,
    operation: ExecutionOperation,
    *,
    subject: ManifestSubject,
    plan: HistoricalBackfillPlan,
    created_at: str,
) -> tuple[str, str]:
    payload = operation.payload
    operation_type = operation.operation_type
    if operation_type in {
        "verify_native_source_binding",
        "verify_media_identity",
        "record_legacy_unknown",
        "skip_existing_binding",
    }:
        return "verified", operation_type
    if operation_type == "flag_probable_binding":
        return "deferred", operation_type
    if operation_type == "create_legacy_source_binding":
        source_digest = payload["source_record_id_digest"]
        if source_digest is None:
            raise HistoricalBackfillError(
                "deterministic_binding_requires_source_digest"
            )
        binding_id = "historical_source_binding_" + normalized_json_digest({
            "manifest_subject_id": subject.manifest_subject_id,
            "unified_batch_id": plan.batch_id,
            "target_record_kind": payload["target_record_kind"],
            "target_record_id_digest": normalized_json_digest({
                "target_record_id": payload["target_record_ref"],
            }),
            "proposed_source_type": payload["proposed_source_type"],
            "source_record_id_digest": source_digest,
            "provenance_kind": "deterministic_legacy",
            "evidence_digest": payload["evidence_digest"],
            "binding_state": "applied",
            "identity_protocol": HISTORICAL_SOURCE_BINDING_SCHEMA_VERSION,
        })
        row = (
            binding_id,
            subject.manifest_subject_id,
            plan.batch_id,
            payload["target_record_kind"],
            payload["target_record_ref"],
            payload["proposed_source_type"],
            source_digest,
            "deterministic_legacy",
            payload["evidence_digest"],
            "applied",
            created_at,
            HISTORICAL_SOURCE_BINDING_SCHEMA_VERSION,
        )
        if _existing_exact(
            connection,
            "ingestion_historical_source_bindings",
            "binding_id",
            binding_id,
            row,
        ):
            return "skipped", operation_type
        conflict = connection.execute(
            """
            SELECT 1 FROM ingestion_historical_source_bindings
            WHERE
              (target_record_kind=? AND target_record_id=? AND
               (proposed_source_type<>? OR source_record_id_digest<>?))
              OR
              (target_record_kind=? AND proposed_source_type=? AND
               source_record_id_digest=? AND target_record_id<>?)
            LIMIT 1
            """,
            (
                payload["target_record_kind"],
                payload["target_record_ref"],
                payload["proposed_source_type"],
                source_digest,
                payload["target_record_kind"],
                payload["proposed_source_type"],
                source_digest,
                payload["target_record_ref"],
            ),
        ).fetchone()
        if conflict is not None:
            raise HistoricalBackfillError("binding_conflict")
        connection.execute(
            "INSERT INTO ingestion_historical_source_bindings "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        return "applied", operation_type
    if operation_type == "create_historical_attachment_binding":
        binding_id = (
            "historical_attachment_binding_"
            + normalized_json_digest({
                "identity_protocol": HISTORICAL_ATTACHMENT_BINDING_SCHEMA_VERSION,
                "manifest_subject_id": subject.manifest_subject_id,
                "unified_batch_id": plan.batch_id,
                "logical_media_id": payload["logical_media_id"],
                "source_attachment_id_digest": (
                    payload["source_attachment_id_digest"]
                ),
                "message_id_digest": normalized_json_digest({
                    "message_id": payload["message_id"],
                }),
                "entity_digest": None,
                "attachment_state": payload["attachment_state"],
                "size_conflict_state": payload["size_conflict_state"],
                "provenance_kind": payload["provenance_kind"],
                "evidence_digest": payload["evidence_digest"],
                "occurrence_index": payload["occurrence_index"],
            })
        )
        row = (
            binding_id,
            subject.manifest_subject_id,
            plan.batch_id,
            payload["logical_media_id"],
            payload["source_attachment_id_digest"],
            payload["message_id"],
            None,
            payload["attachment_state"],
            payload["size_conflict_state"],
            payload["provenance_kind"],
            payload["evidence_digest"],
            payload["occurrence_index"],
            created_at,
            HISTORICAL_ATTACHMENT_BINDING_SCHEMA_VERSION,
        )
        if _existing_exact(
            connection,
            "ingestion_historical_attachment_bindings",
            "binding_id",
            binding_id,
            row,
        ):
            return "skipped", operation_type
        conflict = connection.execute(
            """
            SELECT 1 FROM ingestion_historical_attachment_bindings
            WHERE unified_batch_id=? AND logical_media_id=? AND message_id=?
              AND occurrence_index=? AND binding_id<>?
            LIMIT 1
            """,
            (
                plan.batch_id,
                payload["logical_media_id"],
                payload["message_id"],
                payload["occurrence_index"],
                binding_id,
            ),
        ).fetchone()
        if conflict is not None:
            raise HistoricalBackfillError("attachment_binding_conflict")
        connection.execute(
            "INSERT INTO ingestion_historical_attachment_bindings "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        return "attachment", operation_type
    if operation_type == "record_backfill_conflict":
        conflict_id = payload.get("conflict_id")
        if not isinstance(conflict_id, str):
            return "verified", operation_type
        row = (
            conflict_id,
            plan.batch_id,
            payload["conflict_type"],
            payload["severity"],
            normalized_json_digest(payload["evidence_digests"]),
            canonical_historical_json(payload["proposed_actions"]),
            1 if payload["requires_approval"] else 0,
            "blocked",
            created_at,
            payload.get(
                "conflict_schema_version",
                "v8.3-g-historical-conflict-1",
            ),
        )
        if _existing_exact(
            connection,
            "ingestion_conflicts_v2",
            "conflict_id",
            conflict_id,
            row,
        ):
            return "skipped", operation_type
        connection.execute(
            "INSERT INTO ingestion_conflicts_v2 VALUES(?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        return "conflict", operation_type
    raise HistoricalBackfillError("unknown_operation_type")


@dataclass(frozen=True)
class HistoricalVerificationSnapshot:
    target_fingerprint: str
    legacy_contract_digest: str
    ingestion_row_counts: Mapping[str, int]
    integrity_check: str
    foreign_key_violations: int
    snapshot_schema_version: str = HISTORICAL_VERIFICATION_SNAPSHOT_VERSION


def _verification_delegate_factory(
    *,
    plan: HistoricalBackfillPlan,
    legacy_before: Mapping[str, Any],
    expected_operations: Sequence[str],
) -> Callable[..., tuple[HistoricalVerificationSnapshot, ExecutionVerificationResult]]:
    def verify(**kwargs: Any) -> tuple[
        HistoricalVerificationSnapshot,
        ExecutionVerificationResult,
    ]:
        execution_id = kwargs["execution_id"]
        database_path = Path(kwargs["database_path"])
        executed = set(kwargs["executed_operation_ids"])
        after_legacy = capture_legacy_compatibility(database_path)
        compatibility = run_compatibility_suite(
            legacy_before,
            after_legacy,
            require_empty_ingestion_tables=False,
        )
        with closing(sqlite3.connect(
            database_path.as_uri() + "?mode=ro",
            uri=True,
        )) as connection:
            connection.execute("PRAGMA query_only=ON")
            counts = {
                table: int(connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0])
                for table in UNIFIED_V2_TABLES
            }
            integrity = str(
                connection.execute("PRAGMA integrity_check").fetchone()[0]
            )
            foreign_keys = len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            )
        invariants = {
            "legacy_compatibility_preserved": compatibility["passed"],
            "integrity_check_ok": integrity == "ok",
            "foreign_key_check_ok": foreign_keys == 0,
            "approved_operations_complete": set(expected_operations).issubset(
                executed
            ),
            "source_binding_delta_exact": (
                counts["ingestion_historical_source_bindings"]
                == plan.expected_binding_delta
            ),
            "attachment_binding_delta_exact": (
                counts["ingestion_historical_attachment_bindings"]
                == plan.expected_attachment_binding_delta
            ),
            "conflict_delta_exact": (
                counts["ingestion_conflicts_v2"]
                == plan.expected_conflict_delta
            ),
            "entity_table_absent": "ingestion_asset_entities" not in counts,
            "historical_authority_unified": all(
                counts[name] == 1
                for name in (
                    "ingestion_historical_manifests",
                    "ingestion_manifest_subjects",
                    "ingestion_batches_v2",
                    "ingestion_approvals_v2",
                    "ingestion_executions_v2",
                    "ingestion_leases_v2",
                )
            ),
        }
        passed = all(invariants.values())
        database_after = _hash_file(database_path)
        actual_deltas = {key: 0 for key in COUNT_KEYS}
        payload = {
            "execution_id": execution_id,
            "passed": passed,
            "invariant_results": invariants,
            "actual_deltas": actual_deltas,
            "expected_deltas": {key: 0 for key in COUNT_KEYS},
            "database_after_fingerprint": database_after,
            "asset_after_fingerprint": normalized_json_digest({
                "historical_assets": "unchanged",
            }),
            "verification_schema_version": EXECUTION_VERIFICATION_VERSION,
        }
        digest = normalized_json_digest(payload)
        result = ExecutionVerificationResult(
            verification_id="execverify_" + digest,
            verification_digest=digest,
            **payload,
        )
        snapshot = HistoricalVerificationSnapshot(
            target_fingerprint=database_after,
            legacy_contract_digest=normalized_json_digest({
                key: after_legacy[key]
                for key in (
                    "row_counts",
                    "id_set_digests",
                    "critical_field_digests",
                    "source_values_digest",
                    "attachment_json_digest",
                )
            }),
            ingestion_row_counts=MappingProxyType(dict(sorted(counts.items()))),
            integrity_check=integrity,
            foreign_key_violations=foreign_keys,
        )
        return snapshot, result

    return verify


def _mark_recovery_required(
    clone: Path,
    plan: HistoricalBackfillPlan,
    bundle: ExecutionBundle,
    *,
    reason_code: str,
) -> None:
    occurred = _utc_now()
    with closing(sqlite3.connect(clone)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.set_authorizer(_write_authorizer)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE ingestion_batches_v2
            SET current_state='recovery_required',recovery_status='required',
                error_count=error_count+1,updated_at=?
            WHERE batch_id=?
            """,
            (occurred, plan.batch_id),
        )
        connection.execute(
            """
            UPDATE ingestion_executions_v2
            SET execution_state='recovery_required',recovery_status='required',
                updated_at=?
            WHERE execution_id=?
            """,
            (occurred, bundle.execution_id),
        )
        connection.commit()


def _execute_delegate(
    *,
    bundle: ExecutionBundle,
    target: TestExecutionTarget,
    plan: HistoricalBackfillPlan,
    manifest: HistoricalSourceManifest,
    subject: ManifestSubject,
    approval: ApprovalArtifact,
    operations: Sequence[ExecutionOperation],
    before_contract: Mapping[str, Any],
    fault_injector: Callable[[str], bool | None] | None,
    **_: Any,
) -> HistoricalBackfillResult:
    clone = validate_historical_clone_target(target.database_path)
    schema_verification = verify_historical_migrated_schema(
        clone,
        require_empty=False,
    )
    with closing(sqlite3.connect(
        clone.as_uri() + "?mode=ro",
        uri=True,
    )) as check:
        existing = check.execute(
            "SELECT current_state,manifest_subject_id,plan_digest,"
            "target_snapshot_fingerprint,"
            "(SELECT execution_id FROM ingestion_executions_v2 e "
            " WHERE e.batch_id=ingestion_batches_v2.batch_id "
            "   AND e.execution_state IN ('completed','completed_with_warnings')"
            " ORDER BY e.created_at LIMIT 1) "
            "FROM ingestion_batches_v2 "
            "WHERE batch_id=?",
            (plan.batch_id,),
        ).fetchone()
    if existing is not None and tuple(existing[1:4]) != (
        plan.manifest_subject_id,
        plan.plan_digest,
        plan.target_snapshot_fingerprint,
    ):
        raise HistoricalBackfillError("completed_plan_identity_mismatch")
    if existing is not None and existing[0] in {
        "completed",
        "completed_with_warnings",
    }:
        return HistoricalBackfillResult(
            plan_id=plan.plan_id,
            execution_id=existing[4] or bundle.execution_id,
            applied_bindings=0,
            skipped_bindings=(
                plan.expected_binding_delta
                + plan.expected_attachment_binding_delta
            ),
            deferred_bindings=len(approval.rejected_operation_ids),
            conflict_records=plan.expected_conflict_delta,
            unknown_records=len(plan.unknown_records),
            attachment_bindings=0,
            verification_digest=None,
            final_state="completed",
            warnings=("exact_plan_replay_skipped",),
            errors=(),
        )
    if existing is not None:
        raise HistoricalBackfillError("historical_batch_not_replayable")
    if any(schema_verification["historical_table_row_counts"].values()):
        raise HistoricalBackfillError("historical_target_not_empty")

    execution_root = clone.parent / "executions" / bundle.execution_id
    execution_root.mkdir(parents=True, exist_ok=False)
    journal = execution_root / "journal.jsonl"
    backup = execution_root / "before-backfill.db"
    backup_result = create_consistent_backup(clone, backup)
    expected_backup = normalized_json_digest({
        "backup_schema_fingerprint": backup_result.backup_schema_fingerprint,
        "source_schema_fingerprint": backup_result.source_schema_fingerprint,
        "target_snapshot_fingerprint": plan.target_snapshot_fingerprint,
    })
    if expected_backup != bundle.database_backup_fingerprint:
        raise HistoricalBackfillError("database_backup_binding_mismatch")
    lease = acquire_unified_writer_lease(target, plan.batch_id)
    applied_bindings = 0
    attachment_bindings = 0
    skipped = 0
    conflicts = 0
    executed: list[str] = []
    batch: ImportBatch | None = None
    last_batch_event = ""
    committed = False
    phase = "preflight"
    try:
        if fault_injector and fault_injector("event_journal_failure"):
            raise HistoricalBackfillError("event_journal_write_failed")
        append_unified_execution_journal_event(
            journal,
            execution_id=bundle.execution_id,
            event_type="preflight_started",
        )
        append_unified_execution_journal_event(
            journal,
            execution_id=bundle.execution_id,
            event_type="preflight_passed",
            details={"operation_count": len(operations)},
        )
        append_unified_execution_journal_event(
            journal,
            execution_id=bundle.execution_id,
            event_type="lease_acquired",
        )
        append_unified_execution_journal_event(
            journal,
            execution_id=bundle.execution_id,
            event_type="backup_created",
            details={"backup_digest": expected_backup},
        )
    except BaseException as exc:
        release_writer_lease(target, lease)
        return HistoricalBackfillResult(
            plan_id=plan.plan_id,
            execution_id=bundle.execution_id,
            applied_bindings=0,
            skipped_bindings=0,
            deferred_bindings=len(approval.rejected_operation_ids),
            conflict_records=0,
            unknown_records=len(plan.unknown_records),
            attachment_bindings=0,
            verification_digest=None,
            final_state="failed",
            warnings=(),
            errors=(_safe_failure_reason(exc, phase="event_journal"),),
        )
    occurred = approval.created_at
    try:
        with closing(sqlite3.connect(clone)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.set_authorizer(_write_authorizer)
            connection.execute("BEGIN IMMEDIATE")
            phase = "manifest"
            _insert_manifest(connection, manifest)
            repository = UnifiedAuthorityRepository(connection)
            phase = "subject"
            repository.add_subject(subject)
            phase = "batch"
            batch = repository.create_batch(
                subject,
                manifest,
                batch_mode="historical_backfill",
                plan=plan.identity_payload(),
                target_snapshot_fingerprint=plan.target_snapshot_fingerprint,
                timestamp=occurred,
                batch_id_factory=lambda: plan.batch_id,
            )
            for state, reason in (
                (BatchState.VALIDATING, "historical_validation_started"),
                (BatchState.PLANNED, "historical_plan_validated"),
                (BatchState.AWAITING_APPROVAL, "historical_approval_required"),
            ):
                phase = "batch_transition"
                batch, last_batch_event = _transition(
                    repository,
                    batch,
                    state,
                    occurred_at=occurred,
                    previous_digest=last_batch_event,
                    reason_code=reason,
                )
            phase = "approval"
            _insert_approval(connection, plan, subject, approval)
            phase = "approval_binding"
            batch = bind_batch_approval(batch, timestamp=occurred)
            repository.update_batch_snapshot(batch)
            phase = "approval_transition"
            batch, last_batch_event = _transition(
                repository,
                batch,
                BatchState.APPROVED,
                occurred_at=occurred,
                previous_digest=last_batch_event,
                reason_code="historical_exact_plan_approved",
            )
            phase = "commit_transition"
            batch, last_batch_event = _transition(
                repository,
                batch,
                BatchState.COMMITTING,
                occurred_at=occurred,
                previous_digest=last_batch_event,
                reason_code="historical_commit_started",
            )
            phase = "execution_record"
            connection.execute(
                """
                INSERT INTO ingestion_executions_v2 VALUES(
                  ?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    bundle.execution_id,
                    plan.batch_id,
                    approval.approval_digest,
                    normalized_json_digest(bundle.identity_payload()),
                    "committing",
                    plan.target_snapshot_fingerprint,
                    None,
                    None,
                    "not_required",
                    occurred,
                    occurred,
                    HISTORICAL_EXECUTION_SCHEMA_VERSION,
                ),
            )
            phase = "execution_event"
            execution_event_digest = _execution_event(
                connection,
                execution_id=bundle.execution_id,
                sequence=1,
                event_type="execution_committing",
                previous_state="approved",
                next_state="committing",
                occurred_at=occurred,
                reason_code="historical_commit_started",
                previous_digest="",
            )
            phase = "lease_record"
            connection.execute(
                "INSERT INTO ingestion_leases_v2 VALUES(?,?,?,?,?,?,?,?)",
                (
                    "lease_" + normalized_json_digest({
                        "execution_id": bundle.execution_id,
                        "target_scope": lease.target_scope_digest,
                    }),
                    lease.target_scope_digest,
                    plan.batch_id,
                    hashlib.sha256(
                        lease.secret.encode("ascii")
                    ).hexdigest(),
                    occurred,
                    approval.expires_at,
                    None,
                    "v8.3-e-writer-lease-1",
                ),
            )
            approved = set(approval.approved_operation_ids)
            if fault_injector and fault_injector("attempt_old_table_write"):
                phase = "old_table_write_guard"
                connection.execute(
                    "UPDATE conversations SET title=title WHERE 1=0"
                )
            for operation in operations:
                if operation.operation_id not in approved:
                    continue
                phase = "operation_" + operation.operation_type
                if (
                    operation.operation_type
                    == "create_historical_attachment_binding"
                    and fault_injector
                    and fault_injector("attachment_binding_interruption")
                ):
                    raise HistoricalBackfillError(
                        "attachment_binding_interrupted"
                    )
                status, _operation_type = _apply_operation(
                    connection,
                    operation,
                    subject=subject,
                    plan=plan,
                    created_at=occurred,
                )
                executed.append(operation.operation_id)
                if status == "applied":
                    applied_bindings += 1
                elif status == "attachment":
                    attachment_bindings += 1
                elif status == "skipped":
                    skipped += 1
                elif status == "conflict":
                    conflicts += 1
            if fault_injector and fault_injector("before_commit"):
                raise HistoricalBackfillError("injected_before_commit_failure")
            phase = "database_commit"
            connection.commit()
            committed = True
        append_unified_execution_journal_event(
            journal,
            execution_id=bundle.execution_id,
            event_type="database_committed",
            details={
                "operation_count": len(executed),
                "conflict_count": conflicts,
            },
        )
        if fault_injector and fault_injector("after_commit"):
            _mark_recovery_required(
                clone,
                plan,
                bundle,
                reason_code="injected_after_commit_failure",
            )
            append_unified_execution_journal_event(
                journal,
                execution_id=bundle.execution_id,
                event_type="recovery_required",
                details={"reason_code": "injected_after_commit_failure"},
            )
            return HistoricalBackfillResult(
                plan_id=plan.plan_id,
                execution_id=bundle.execution_id,
                applied_bindings=applied_bindings,
                skipped_bindings=skipped,
                deferred_bindings=len(approval.rejected_operation_ids),
                conflict_records=conflicts,
                unknown_records=len(plan.unknown_records),
                attachment_bindings=attachment_bindings,
                verification_digest=None,
                final_state="recovery_required",
                warnings=(),
                errors=("injected_after_commit_failure",),
            )
        append_unified_execution_journal_event(
            journal,
            execution_id=bundle.execution_id,
            event_type="verification_started",
        )
        phase = "verification"
        verification_delegate = _verification_delegate_factory(
            plan=plan,
            legacy_before=before_contract,
            expected_operations=approval.approved_operation_ids,
        )
        _after, verification = verify_unified_execution(
            execution_id=bundle.execution_id,
            database_path=clone,
            asset_root=target.asset_root,
            before_snapshot=None,  # delegate owns the clone snapshot protocol
            expectation=bundle.verification_expectations,
            executed_operation_ids=executed,
            resolved_conflict_count=0,
            verification_delegate=verification_delegate,
        )
        if fault_injector and fault_injector("verification_failure"):
            verification = ExecutionVerificationResult(
                verification_id="execverify_" + normalized_json_digest({
                    **verification.identity_payload(),
                    "passed": False,
                    "invariant_results": {
                        **dict(verification.invariant_results),
                        "fault_injected": False,
                    },
                }),
                execution_id=verification.execution_id,
                passed=False,
                invariant_results={
                    **dict(verification.invariant_results),
                    "fault_injected": False,
                },
                actual_deltas=verification.actual_deltas,
                expected_deltas=verification.expected_deltas,
                database_after_fingerprint=(
                    verification.database_after_fingerprint
                ),
                asset_after_fingerprint=verification.asset_after_fingerprint,
                verification_digest=normalized_json_digest({
                    **verification.identity_payload(),
                    "passed": False,
                    "invariant_results": {
                        **dict(verification.invariant_results),
                        "fault_injected": False,
                    },
                }),
            )
        if not verification.passed:
            _mark_recovery_required(
                clone,
                plan,
                bundle,
                reason_code="verification_failed",
            )
            append_unified_execution_journal_event(
                journal,
                execution_id=bundle.execution_id,
                event_type="recovery_required",
                details={"reason_code": "verification_failed"},
            )
            return HistoricalBackfillResult(
                plan_id=plan.plan_id,
                execution_id=bundle.execution_id,
                applied_bindings=applied_bindings,
                skipped_bindings=skipped,
                deferred_bindings=len(approval.rejected_operation_ids),
                conflict_records=conflicts,
                unknown_records=len(plan.unknown_records),
                attachment_bindings=attachment_bindings,
                verification_digest=verification.verification_digest,
                final_state="recovery_required",
                warnings=(),
                errors=("verification_failed",),
            )
        with closing(sqlite3.connect(clone)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.set_authorizer(_write_authorizer)
            connection.execute("BEGIN IMMEDIATE")
            repository = UnifiedAuthorityRepository(connection)
            assert batch is not None
            phase = "verification_transition"
            batch, last_batch_event = _transition(
                repository,
                batch,
                BatchState.VERIFYING,
                occurred_at=_utc_now(),
                previous_digest=last_batch_event,
                reason_code="historical_verification_started",
            )
            phase = "verification_execution_update"
            connection.execute(
                """
                UPDATE ingestion_executions_v2
                SET execution_state='verifying',database_after_fingerprint=?,
                    verification_digest=?,updated_at=?
                WHERE execution_id=?
                """,
                (
                    verification.database_after_fingerprint,
                    verification.verification_digest,
                    _utc_now(),
                    bundle.execution_id,
                ),
            )
            phase = "verification_execution_event"
            event2 = _execution_event(
                connection,
                execution_id=bundle.execution_id,
                sequence=2,
                event_type="execution_verifying",
                previous_state="committing",
                next_state="verifying",
                occurred_at=_utc_now(),
                reason_code="historical_verification_started",
                previous_digest=execution_event_digest,
            )
            phase = "verification_record"
            connection.execute(
                """
                INSERT INTO ingestion_verifications_v2 VALUES(
                  ?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    verification.verification_id,
                    bundle.execution_id,
                    plan.batch_id,
                    1,
                    canonical_historical_json(
                        dict(verification.invariant_results)
                    ),
                    canonical_historical_json(dict(verification.actual_deltas)),
                    canonical_historical_json(
                        dict(verification.expected_deltas)
                    ),
                    verification.database_after_fingerprint,
                    verification.asset_after_fingerprint,
                    verification.verification_digest,
                    _utc_now(),
                    verification.verification_schema_version,
                ),
            )
            if (
                fault_injector
                and fault_injector("after_verification_before_completed")
            ):
                connection.rollback()
                _mark_recovery_required(
                    clone,
                    plan,
                    bundle,
                    reason_code="completion_event_interrupted",
                )
                return HistoricalBackfillResult(
                    plan_id=plan.plan_id,
                    execution_id=bundle.execution_id,
                    applied_bindings=applied_bindings,
                    skipped_bindings=skipped,
                    deferred_bindings=len(approval.rejected_operation_ids),
                    conflict_records=conflicts,
                    unknown_records=len(plan.unknown_records),
                    attachment_bindings=attachment_bindings,
                    verification_digest=verification.verification_digest,
                    final_state="recovery_required",
                    warnings=(),
                    errors=("completion_event_interrupted",),
                )
            phase = "completion_transition"
            batch, last_batch_event = _transition(
                repository,
                batch,
                BatchState.COMPLETED,
                occurred_at=_utc_now(),
                previous_digest=last_batch_event,
                reason_code="historical_backfill_completed",
            )
            phase = "completion_execution_update"
            connection.execute(
                """
                UPDATE ingestion_executions_v2
                SET execution_state='completed',recovery_status='not_required',
                    updated_at=?
                WHERE execution_id=?
                """,
                (_utc_now(), bundle.execution_id),
            )
            phase = "completion_execution_event"
            _execution_event(
                connection,
                execution_id=bundle.execution_id,
                sequence=3,
                event_type="execution_completed",
                previous_state="verifying",
                next_state="completed",
                occurred_at=_utc_now(),
                reason_code="historical_backfill_completed",
                previous_digest=event2,
            )
            phase = "lease_release"
            connection.execute(
                "UPDATE ingestion_leases_v2 SET released_at=? "
                "WHERE batch_id=? AND released_at IS NULL",
                (_utc_now(), plan.batch_id),
            )
            connection.commit()
        append_unified_execution_journal_event(
            journal,
            execution_id=bundle.execution_id,
            event_type="verification_passed",
            details={
                "verification_digest": verification.verification_digest,
            },
        )
        append_unified_execution_journal_event(
            journal,
            execution_id=bundle.execution_id,
            event_type="execution_completed",
        )
        return HistoricalBackfillResult(
            plan_id=plan.plan_id,
            execution_id=bundle.execution_id,
            applied_bindings=applied_bindings,
            skipped_bindings=skipped,
            deferred_bindings=len(approval.rejected_operation_ids),
            conflict_records=conflicts,
            unknown_records=len(plan.unknown_records),
            attachment_bindings=attachment_bindings,
            verification_digest=verification.verification_digest,
            final_state="completed",
            warnings=(),
            errors=(),
        )
    except BaseException as exc:
        reason = _safe_failure_reason(exc, phase=phase)
        if committed:
            _mark_recovery_required(
                clone,
                plan,
                bundle,
                reason_code=reason,
            )
            state = "recovery_required"
        else:
            state = "failed"
        try:
            append_unified_execution_journal_event(
                journal,
                execution_id=bundle.execution_id,
                event_type=(
                    "recovery_required"
                    if committed else "execution_failed"
                ),
                details={"reason_code": reason},
            )
        except Exception:
            pass
        return HistoricalBackfillResult(
            plan_id=plan.plan_id,
            execution_id=bundle.execution_id,
            applied_bindings=0 if not committed else applied_bindings,
            skipped_bindings=skipped,
            deferred_bindings=len(approval.rejected_operation_ids),
            conflict_records=conflicts,
            unknown_records=len(plan.unknown_records),
            attachment_bindings=(
                attachment_bindings if committed else 0
            ),
            verification_digest=None,
            final_state=state,
            warnings=(),
            errors=(reason,),
        )
    finally:
        try:
            release_writer_lease(target, lease)
        except Exception:
            pass


def execute_historical_backfill(
    *,
    clone_path: str | Path,
    manifest: HistoricalSourceManifest,
    subject: ManifestSubject,
    plan: HistoricalBackfillPlan,
    approval: ApprovalArtifact | None = None,
    approval_created_at: str | None = None,
    approval_expires_at: str | None = None,
    fault_injector: Callable[[str], bool | None] | None = None,
) -> tuple[HistoricalBackfillResult, ApprovalArtifact, ExecutionBundle]:
    assert_unified_historical_authority()
    clone = validate_historical_clone_target(clone_path)
    if (
        subject.historical_manifest_id != manifest.historical_manifest_id
        or plan.manifest_subject_id != subject.manifest_subject_id
        or plan.migration_version != HISTORICAL_MIGRATION_VERSION
        or plan.migration_digest != HISTORICAL_MIGRATION_DIGEST
    ):
        raise HistoricalBackfillError("historical_execution_binding_mismatch")
    verify_historical_migrated_schema(clone, require_empty=False)
    legacy_before = capture_legacy_compatibility(clone)
    if (
        _legacy_logical_contract_digest(legacy_before)
        != plan.verification_expectations["legacy_logical_contract_digest"]
    ):
        raise HistoricalBackfillError("historical_target_snapshot_stale")
    operations = build_historical_operations(plan)
    created = approval_created_at or _utc_now()
    expires = approval_expires_at or _plus_hours(created, 24)
    stage_plan, built_approval = build_historical_approval(
        plan,
        operations,
        created_at=created,
        expires_at=expires,
    )
    selected_approval = approval or built_approval
    validate_unified_approval(
        selected_approval,
        import_plan=plan,
        stage_plan=stage_plan,
        target_snapshot_fingerprint=plan.target_snapshot_fingerprint,
        candidate_operation_ids=tuple(
            item.operation_id for item in operations
        ),
        now=created,
    )
    target = _execution_target(clone, plan)
    backup_fingerprint = normalized_json_digest({
        "backup_schema_fingerprint": legacy_before["schema_fingerprint"],
        "source_schema_fingerprint": legacy_before["schema_fingerprint"],
        "target_snapshot_fingerprint": plan.target_snapshot_fingerprint,
    })
    bundle = build_historical_execution_bundle(
        plan,
        selected_approval,
        stage_plan,
        operations,
        target,
        database_backup_fingerprint=backup_fingerprint,
    )
    delegate = lambda **kwargs: _execute_delegate(  # noqa: E731
        **kwargs,
        plan=plan,
        manifest=manifest,
        subject=subject,
        approval=selected_approval,
        operations=operations,
        before_contract=legacy_before,
    )
    result = execute_historical_operation_handler(
        bundle=bundle,
        target=target,
        before_snapshot=None,
        staged_entities=(),
        fault_injector=fault_injector,
        execution_delegate=delegate,
    )
    return result, selected_approval, bundle


def rollback_historical_backfill(
    *,
    clone_path: str | Path,
    execution_id: str,
    fail_restore: bool = False,
) -> Mapping[str, Any]:
    clone = validate_historical_clone_target(clone_path)
    backup = clone.parent / "executions" / execution_id / "before-backfill.db"
    if not backup.is_file():
        raise HistoricalBackfillError("rollback_backup_missing")
    if fail_restore:
        return MappingProxyType({
            "status": "recovery_required",
            "reason_code": "rollback_failed",
        })
    result = restore_from_backup(clone, backup)
    return MappingProxyType({
        "status": "rolled_back",
        "restore_status": result["status"],
        "reason_code": "historical_backfill_rolled_back",
    })


def recover_historical_backfill(
    *,
    clone_path: str | Path,
    plan: HistoricalBackfillPlan,
    bundle: ExecutionBundle,
) -> Mapping[str, Any]:
    clone = validate_historical_clone_target(clone_path)
    with closing(sqlite3.connect(
        clone.as_uri() + "?mode=ro",
        uri=True,
    )) as connection:
        state = connection.execute(
            "SELECT current_state FROM ingestion_batches_v2 WHERE batch_id=?",
            (plan.batch_id,),
        ).fetchone()
        verification = connection.execute(
            "SELECT passed,verification_digest FROM ingestion_verifications_v2 "
            "WHERE execution_id=?",
            (bundle.execution_id,),
        ).fetchone()
    if state is None:
        return MappingProxyType({
            "status": "not_applied",
            "reason_code": "precommit_rollback_complete",
        })
    if state[0] in {"completed", "completed_with_warnings"}:
        return MappingProxyType({
            "status": "completed",
            "reason_code": "already_reconciled",
        })
    if verification is not None and verification[0] == 1:
        return MappingProxyType({
            "status": "recovery_required",
            "reason_code": "completion_event_reconciliation_required",
            "verification_digest": verification[1],
        })
    return MappingProxyType({
        "status": "recovery_required",
        "reason_code": "rollback_or_manual_reconciliation_required",
    })
