from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from memory.ingestion_core import (
    BatchState,
    SourceManifest,
    build_source_manifest,
    transition_batch,
)
from memory.ingestion_historical_schema import (
    HistoricalAttachmentBinding,
    HistoricalSourceBinding,
    HistoricalSourceManifest,
    ManifestSubject,
    UnifiedAuthorityRepository,
    build_historical_attachment_binding,
    build_historical_source_binding,
    build_historical_source_manifest,
    build_manifest_subject,
    canonical_historical_json,
)
from memory.ingestion_migrations import (
    HistoricalMigrationPlan,
    apply_historical_migration_to_clone,
    build_historical_migration_plan,
    inspect_schema,
)
from tests.v83_migration_fixtures import create_legacy_database, migrate_database


CREATED_AT = "2026-07-29T00:00:00.000000Z"
UPDATED_AT = "2026-07-29T00:01:00.000000Z"
EXPIRES_AT = "2026-07-30T00:00:00.000000Z"
EVIDENCE_POLICY_VERSION = "v8.3-g0-evidence-policy-1"
EVIDENCE_DIGEST = "1" * 64
RECONSTRUCTION_BASIS_DIGEST = "2" * 64
TARGET_SNAPSHOT = "3" * 64
SOURCE_RECORD_DIGEST = "4" * 64
SOURCE_ATTACHMENT_DIGEST = "5" * 64
ENTITY_DIGEST = "6" * 64
DATABASE_BEFORE = "7" * 64
DATABASE_AFTER = "8" * 64
ASSET_AFTER = "9" * 64
LOGICAL_MEDIA_ID = "ym1_" + ("a" * 52)


def create_v8301_database(path: Path) -> Path:
    create_legacy_database(path)
    migrate_database(path, rehearsal_id="rehearsal_g0_v8301_001")
    return path


def migrate_v8302(
    path: Path,
    *,
    rehearsal_id: str = "rehearsal_g0_v8302_001",
) -> HistoricalMigrationPlan:
    plan = build_historical_migration_plan(inspect_schema(path))
    apply_historical_migration_to_clone(
        plan,
        path,
        rehearsal_id=rehearsal_id,
    )
    return plan


def ordinary_manifest() -> SourceManifest:
    return build_source_manifest(
        source_type="chatgpt",
        source_version="export_v1",
        adapter_name="chatgpt_adapter",
        adapter_version="v8.3-c",
        archive_sha256="a" * 64,
        archive_size=128,
        archive_kind="zip",
        discovered_at=CREATED_AT,
        scanned_at=CREATED_AT,
        record_counts={"conversations": 1, "messages": 1},
        asset_counts={"attachments": 1},
    )


def historical_manifest(
    *,
    provenance_kind: str = "deterministic_legacy",
    adapter_name: str | None = None,
    adapter_version: str | None = None,
) -> HistoricalSourceManifest:
    return build_historical_source_manifest(
        source_type="legacy_reconstruction",
        provenance_kind=provenance_kind,
        archive_state="unavailable",
        archive_sha256=None,
        archive_size=None,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        evidence_policy_version=EVIDENCE_POLICY_VERSION,
        evidence_digest=EVIDENCE_DIGEST,
        reconstruction_basis_digest=RECONSTRUCTION_BASIS_DIGEST,
        validation_status="evidence_reconstructed",
        record_counts={"conversations": 1, "messages": 2},
        asset_counts={"metadata_only": 1},
        warnings=("original_archive_unavailable",),
        created_at=CREATED_AT,
    )


def subjects() -> tuple[
    SourceManifest,
    HistoricalSourceManifest,
    ManifestSubject,
    ManifestSubject,
]:
    source = ordinary_manifest()
    historical = historical_manifest()
    return (
        source,
        historical,
        build_manifest_subject(source_manifest=source, created_at=CREATED_AT),
        build_manifest_subject(
            historical_manifest=historical,
            created_at=CREATED_AT,
        ),
    )


def insert_source_manifest(
    conn: sqlite3.Connection,
    manifest: SourceManifest,
) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_source_manifests VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            manifest.manifest_id,
            manifest.source_type,
            manifest.source_version,
            manifest.adapter_name,
            manifest.adapter_version,
            manifest.archive_sha256,
            manifest.archive_size,
            manifest.archive_kind,
            manifest.manifest_schema_version,
            manifest.validation_status,
            canonical_historical_json(dict(manifest.record_counts)),
            canonical_historical_json(dict(manifest.asset_counts)),
            canonical_historical_json(list(manifest.warnings)),
            manifest.discovered_at,
            manifest.scanned_at,
        ),
    )


def insert_historical_manifest(
    conn: sqlite3.Connection,
    manifest: HistoricalSourceManifest,
) -> None:
    conn.execute(
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


def build_bindings(
    subject: ManifestSubject,
    batch_id: str,
) -> tuple[HistoricalSourceBinding, HistoricalAttachmentBinding]:
    source_binding = build_historical_source_binding(
        manifest_subject_id=subject.manifest_subject_id,
        unified_batch_id=batch_id,
        target_record_kind="conversation",
        target_record_id="fixture-conversation-1",
        proposed_source_type="chatgpt",
        source_record_id_digest=SOURCE_RECORD_DIGEST,
        provenance_kind="deterministic_legacy",
        evidence_digest=EVIDENCE_DIGEST,
        binding_state="approved",
        created_at=CREATED_AT,
    )
    attachment_binding = build_historical_attachment_binding(
        manifest_subject_id=subject.manifest_subject_id,
        unified_batch_id=batch_id,
        logical_media_id=LOGICAL_MEDIA_ID,
        source_attachment_id_digest=SOURCE_ATTACHMENT_DIGEST,
        message_id="fixture-message-1",
        entity_digest=None,
        attachment_state="metadata_only",
        size_conflict_state="clear",
        provenance_kind="deterministic_legacy",
        evidence_digest=EVIDENCE_DIGEST,
        occurrence_index=0,
        created_at=CREATED_AT,
    )
    return source_binding, attachment_binding


def insert_unified_chains(path: Path) -> dict[str, Any]:
    source, historical, source_subject, historical_subject = subjects()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        insert_source_manifest(conn, source)
        insert_historical_manifest(conn, historical)
        repository = UnifiedAuthorityRepository(conn)
        repository.add_subject(source_subject)
        repository.add_subject(historical_subject)
        full_batch = repository.create_batch(
            source_subject,
            source,
            batch_mode="full",
            plan={"mode": "full", "operations": []},
            target_snapshot_fingerprint=TARGET_SNAPSHOT,
            timestamp=CREATED_AT,
            batch_id_factory=lambda: "batch_" + "a" * 32,
        )
        historical_batch = repository.create_batch(
            historical_subject,
            historical,
            batch_mode="historical_backfill",
            plan={"mode": "historical_backfill", "operations": []},
            target_snapshot_fingerprint=TARGET_SNAPSHOT,
            timestamp=CREATED_AT,
            batch_id_factory=lambda: "batch_" + "b" * 32,
        )
        events = []
        for batch in (full_batch, historical_batch):
            updated, event = transition_batch(
                batch,
                BatchState.VALIDATING,
                actor_kind="test_operator",
                reason_code="validation_started",
                occurred_at=UPDATED_AT,
            )
            repository.append_event(event)
            repository.update_batch_snapshot(updated)
            events.append(event)
        source_binding, attachment_binding = build_bindings(
            historical_subject,
            historical_batch.batch_id,
        )
        conn.execute(
            "INSERT INTO ingestion_historical_source_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_binding.binding_id,
                source_binding.manifest_subject_id,
                source_binding.unified_batch_id,
                source_binding.target_record_kind,
                source_binding.target_record_id,
                source_binding.proposed_source_type,
                source_binding.source_record_id_digest,
                source_binding.provenance_kind,
                source_binding.evidence_digest,
                source_binding.binding_state,
                source_binding.created_at,
                source_binding.binding_schema_version,
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_historical_attachment_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attachment_binding.binding_id,
                attachment_binding.manifest_subject_id,
                attachment_binding.unified_batch_id,
                attachment_binding.logical_media_id,
                attachment_binding.source_attachment_id_digest,
                attachment_binding.message_id,
                attachment_binding.entity_digest,
                attachment_binding.attachment_state,
                attachment_binding.size_conflict_state,
                attachment_binding.provenance_kind,
                attachment_binding.evidence_digest,
                attachment_binding.occurrence_index,
                attachment_binding.created_at,
                attachment_binding.binding_schema_version,
            ),
        )
        for marker, batch, subject in (
            ("a", full_batch, source_subject),
            ("b", historical_batch, historical_subject),
        ):
            approval_digest = marker * 64
            conn.execute(
                "INSERT INTO ingestion_approvals_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "approval_" + marker * 64,
                    batch.batch_id,
                    subject.manifest_subject_id,
                    "plan_" + marker * 64,
                    batch.plan_digest,
                    "stage_plan_" + marker * 64,
                    marker * 64,
                    TARGET_SNAPSHOT,
                    approval_digest,
                    "exact_plan",
                    "test_operator",
                    CREATED_AT,
                    EXPIRES_AT,
                    "v8.3-e-approval-1",
                ),
            )
            execution_id = "execution_" + marker * 64
            conn.execute(
                "INSERT INTO ingestion_executions_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_id,
                    batch.batch_id,
                    approval_digest,
                    marker * 64,
                    "completed",
                    DATABASE_BEFORE,
                    DATABASE_AFTER,
                    marker * 64,
                    "not_required",
                    CREATED_AT,
                    UPDATED_AT,
                    "v8.3-e-execution-bundle-1",
                ),
            )
            conn.execute(
                "INSERT INTO ingestion_verifications_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "execverify_" + marker * 64,
                    execution_id,
                    batch.batch_id,
                    1,
                    canonical_historical_json({"integrity": True}),
                    canonical_historical_json({"messages": 0}),
                    canonical_historical_json({"messages": 0}),
                    DATABASE_AFTER,
                    ASSET_AFTER,
                    marker * 64,
                    UPDATED_AT,
                    "v8.3-e-execution-verification-1",
                ),
            )
            conn.execute(
                "INSERT INTO ingestion_leases_v2 VALUES(?,?,?,?,?,?,?,?)",
                (
                    "lease_" + marker * 64,
                    "f" * 64,
                    batch.batch_id,
                    marker * 64,
                    CREATED_AT,
                    EXPIRES_AT,
                    UPDATED_AT,
                    "v8.3-e-writer-lease-1",
                ),
            )
        conn.commit()
        counts = {
            table: int(conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0])
            for table in (
                "ingestion_manifest_subjects",
                "ingestion_batches_v2",
                "ingestion_batch_events_v2",
                "ingestion_approvals_v2",
                "ingestion_executions_v2",
                "ingestion_verifications_v2",
                "ingestion_leases_v2",
            )
        }
    return {
        "source_manifest": source,
        "historical_manifest": historical,
        "source_subject": source_subject,
        "historical_subject": historical_subject,
        "full_batch": full_batch,
        "historical_batch": historical_batch,
        "events": tuple(events),
        "source_binding": source_binding,
        "attachment_binding": attachment_binding,
        "counts": counts,
    }
