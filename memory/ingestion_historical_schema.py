"""YunOS v8.3-G0 historical evidence and unified v2 authority.

Historical manifests are honest, nullable evidence records.  They do not own a
parallel batch, approval, execution, recovery, or verification state machine.
Both ordinary and historical manifests are registered as ``ManifestSubject``
instances and then use the v8.3-A/B/E authority functions and repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence
import re
import sqlite3

from memory.ingestion_core import (
    BatchEvent,
    DatabaseFingerprint,
    ImportBatch,
    SourceManifest,
    canonical_json_bytes,
    create_import_batch,
    normalized_json_digest,
)
from memory.ingestion_approval import validate_approval_artifact
from memory.ingestion_execution import (
    acquire_writer_lease,
    append_execution_journal_event,
    execute_test_bundle,
)
from memory.ingestion_verification import verify_test_execution


HISTORICAL_MANIFEST_SCHEMA_VERSION = "v8.3-g0-historical-manifest-2"
MANIFEST_SUBJECT_SCHEMA_VERSION = "v8.3-g0-manifest-subject-1"
HISTORICAL_SOURCE_BINDING_SCHEMA_VERSION = "v8.3-g0-source-binding-2"
HISTORICAL_ATTACHMENT_BINDING_SCHEMA_VERSION = "v8.3-g0-attachment-binding-2"
UNIFIED_BATCH_SCHEMA_VERSION = "v8.3-g0-unified-batch-1"

HISTORICAL_PROVENANCE_KINDS = frozenset({
    "deterministic_legacy",
    "probable_legacy",
    "legacy_unknown",
    "conflicting_evidence",
})
HISTORICAL_BINDING_STATES = frozenset({
    "approved",
    "applied",
    "verified",
    "deferred",
    "rejected",
    "recorded_unknown",
    "blocked",
})
HISTORICAL_ATTACHMENT_STATES = frozenset({
    "resolved",
    "metadata_only",
    "asset_record_missing",
    "entity_missing",
    "unresolved",
})
HISTORICAL_SIZE_CONFLICT_STATES = frozenset({
    "clear",
    "size_conflict_unresolved",
})
MANIFEST_KINDS = frozenset({"source", "historical_reconstruction"})

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_MEDIA_ID_RE = re.compile(r"ym1_[a-z2-7]+")
_ZERO_SHA = "0" * 64


class HistoricalSchemaError(ValueError):
    """Fail-closed historical evidence validation error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _token(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise HistoricalSchemaError(reason)
    return value


def _identity(value: Any, prefix: str, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or _ID_RE.fullmatch(value) is None
    ):
        raise HistoricalSchemaError(reason)
    return value


def _digest(value: Any, reason: str) -> str:
    if (
        not isinstance(value, str)
        or _SHA_RE.fullmatch(value) is None
        or value == _ZERO_SHA
    ):
        raise HistoricalSchemaError(reason)
    return value


def _optional_digest(value: Any, reason: str) -> str | None:
    if value is None:
        return None
    return _digest(value, reason)


def _timestamp(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HistoricalSchemaError(reason)
    return value


def _counts(value: Mapping[str, int], reason: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise HistoricalSchemaError(reason)
    normalized: dict[str, int] = {}
    for key, count in value.items():
        _token(key, reason)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise HistoricalSchemaError(reason)
        normalized[key] = count
    return MappingProxyType(dict(sorted(normalized.items())))


def _warnings(value: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or len(value) > 256:
        raise HistoricalSchemaError("invalid_historical_warnings")
    return tuple(_token(item, "invalid_historical_warning") for item in value)


def _private_reference(value: Any, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", value)
        or "\x00" in value
    ):
        raise HistoricalSchemaError(reason)
    return value


def canonical_historical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return prefix + normalized_json_digest(payload)


@dataclass(frozen=True)
class HistoricalSourceManifest:
    historical_manifest_id: str
    source_type: str
    provenance_kind: str
    archive_state: str
    archive_sha256: None
    archive_size: None
    adapter_name: str | None
    adapter_version: str | None
    evidence_policy_version: str
    evidence_digest: str
    reconstruction_basis_digest: str
    validation_status: str
    record_counts: Mapping[str, int]
    asset_counts: Mapping[str, int]
    warnings: tuple[str, ...]
    created_at: str
    manifest_schema_version: str = HISTORICAL_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(
            self.historical_manifest_id,
            "historical_manifest_",
            "invalid_historical_manifest_id",
        )
        _token(self.source_type, "invalid_historical_source_type")
        if self.provenance_kind not in HISTORICAL_PROVENANCE_KINDS:
            raise HistoricalSchemaError("invalid_historical_provenance_kind")
        if self.archive_state != "unavailable":
            raise HistoricalSchemaError("historical_archive_must_be_unavailable")
        if self.archive_sha256 is not None or self.archive_size is not None:
            raise HistoricalSchemaError(
                "unavailable_archive_must_not_have_archive_facts"
            )
        if (self.adapter_name is None) != (self.adapter_version is None):
            raise HistoricalSchemaError("partial_historical_adapter_identity")
        if self.adapter_name is not None:
            _token(self.adapter_name, "invalid_historical_adapter_name")
            _token(self.adapter_version, "invalid_historical_adapter_version")
        _token(
            self.evidence_policy_version,
            "invalid_historical_evidence_policy_version",
        )
        _digest(self.evidence_digest, "invalid_historical_evidence_digest")
        _digest(
            self.reconstruction_basis_digest,
            "invalid_reconstruction_basis_digest",
        )
        _token(self.validation_status, "invalid_historical_validation_status")
        object.__setattr__(
            self,
            "record_counts",
            _counts(self.record_counts, "invalid_historical_record_counts"),
        )
        object.__setattr__(
            self,
            "asset_counts",
            _counts(self.asset_counts, "invalid_historical_asset_counts"),
        )
        object.__setattr__(self, "warnings", _warnings(self.warnings))
        _timestamp(self.created_at, "invalid_historical_created_at")
        if self.manifest_schema_version != HISTORICAL_MANIFEST_SCHEMA_VERSION:
            raise HistoricalSchemaError("historical_manifest_schema_mismatch")
        if self.historical_manifest_id != _stable_id(
            "historical_manifest_", self.identity_payload()
        ):
            raise HistoricalSchemaError("historical_manifest_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "identity_protocol": self.manifest_schema_version,
            "source_type": self.source_type,
            "provenance_kind": self.provenance_kind,
            "archive_state": self.archive_state,
            "archive_sha256": self.archive_sha256,
            "archive_size": self.archive_size,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "evidence_policy_version": self.evidence_policy_version,
            "evidence_digest": self.evidence_digest,
            "reconstruction_basis_digest": self.reconstruction_basis_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "historical_manifest_id": self.historical_manifest_id,
            **self.identity_payload(),
            "validation_status": self.validation_status,
            "record_counts": dict(self.record_counts),
            "asset_counts": dict(self.asset_counts),
            "warnings": list(self.warnings),
            "created_at": self.created_at,
        }


def build_historical_source_manifest(
    *,
    source_type: str,
    provenance_kind: str,
    archive_state: str = "unavailable",
    archive_sha256: None = None,
    archive_size: None = None,
    adapter_name: str | None,
    adapter_version: str | None,
    evidence_policy_version: str,
    evidence_digest: str,
    reconstruction_basis_digest: str,
    validation_status: str,
    record_counts: Mapping[str, int],
    asset_counts: Mapping[str, int],
    warnings: Sequence[str],
    created_at: str,
) -> HistoricalSourceManifest:
    payload = {
        "identity_protocol": HISTORICAL_MANIFEST_SCHEMA_VERSION,
        "source_type": source_type,
        "provenance_kind": provenance_kind,
        "archive_state": archive_state,
        "archive_sha256": archive_sha256,
        "archive_size": archive_size,
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "evidence_policy_version": evidence_policy_version,
        "evidence_digest": evidence_digest,
        "reconstruction_basis_digest": reconstruction_basis_digest,
    }
    return HistoricalSourceManifest(
        historical_manifest_id=_stable_id("historical_manifest_", payload),
        validation_status=validation_status,
        record_counts=record_counts,
        asset_counts=asset_counts,
        warnings=tuple(warnings),
        created_at=created_at,
        **{key: value for key, value in payload.items() if key != "identity_protocol"},
    )


@dataclass(frozen=True)
class ManifestSubject:
    manifest_subject_id: str
    manifest_kind: str
    source_manifest_id: str | None
    historical_manifest_id: str | None
    subject_digest: str
    evidence_policy_version: str | None
    created_at: str
    subject_schema_version: str = MANIFEST_SUBJECT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(
            self.manifest_subject_id,
            "manifest_subject_",
            "invalid_manifest_subject_id",
        )
        if self.manifest_kind not in MANIFEST_KINDS:
            raise HistoricalSchemaError("invalid_manifest_kind")
        source_present = self.source_manifest_id is not None
        historical_present = self.historical_manifest_id is not None
        if source_present == historical_present:
            raise HistoricalSchemaError("manifest_subject_requires_exactly_one_ref")
        if self.manifest_kind == "source":
            if not source_present:
                raise HistoricalSchemaError("source_subject_reference_mismatch")
            _identity(
                self.source_manifest_id,
                "manifest_",
                "invalid_source_manifest_id",
            )
            if self.evidence_policy_version is not None:
                raise HistoricalSchemaError("source_subject_has_evidence_policy")
        else:
            if not historical_present:
                raise HistoricalSchemaError("historical_subject_reference_mismatch")
            _identity(
                self.historical_manifest_id,
                "historical_manifest_",
                "invalid_historical_manifest_id",
            )
            _token(
                self.evidence_policy_version,
                "invalid_subject_evidence_policy",
            )
        _digest(self.subject_digest, "invalid_manifest_subject_digest")
        _timestamp(self.created_at, "invalid_manifest_subject_created_at")
        if self.subject_schema_version != MANIFEST_SUBJECT_SCHEMA_VERSION:
            raise HistoricalSchemaError("manifest_subject_schema_mismatch")
        expected = normalized_json_digest(self.identity_payload())
        if self.subject_digest != expected:
            raise HistoricalSchemaError("manifest_subject_digest_mismatch")
        if self.manifest_subject_id != "manifest_subject_" + expected:
            raise HistoricalSchemaError("manifest_subject_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "manifest_kind": self.manifest_kind,
            "source_manifest_id": self.source_manifest_id,
            "historical_manifest_id": self.historical_manifest_id,
            "evidence_policy_version": self.evidence_policy_version,
            "subject_schema_version": self.subject_schema_version,
        }


def build_manifest_subject(
    *,
    source_manifest: SourceManifest | None = None,
    historical_manifest: HistoricalSourceManifest | None = None,
    created_at: str,
) -> ManifestSubject:
    if (source_manifest is None) == (historical_manifest is None):
        raise HistoricalSchemaError("manifest_subject_requires_exactly_one_ref")
    if source_manifest is not None and not isinstance(source_manifest, SourceManifest):
        raise HistoricalSchemaError("invalid_source_manifest")
    if (
        historical_manifest is not None
        and not isinstance(historical_manifest, HistoricalSourceManifest)
    ):
        raise HistoricalSchemaError("invalid_historical_manifest")
    payload = {
        "manifest_kind": (
            "source" if source_manifest is not None
            else "historical_reconstruction"
        ),
        "source_manifest_id": (
            source_manifest.manifest_id if source_manifest is not None else None
        ),
        "historical_manifest_id": (
            historical_manifest.historical_manifest_id
            if historical_manifest is not None else None
        ),
        "evidence_policy_version": (
            historical_manifest.evidence_policy_version
            if historical_manifest is not None else None
        ),
        "subject_schema_version": MANIFEST_SUBJECT_SCHEMA_VERSION,
    }
    digest = normalized_json_digest(payload)
    return ManifestSubject(
        manifest_subject_id="manifest_subject_" + digest,
        subject_digest=digest,
        created_at=created_at,
        **payload,
    )


@dataclass(frozen=True)
class _SubjectCoreView:
    manifest_id: str
    source_type: str
    adapter_name: str
    adapter_version: str


def create_unified_import_batch(
    subject: ManifestSubject,
    manifest: SourceManifest | HistoricalSourceManifest,
    *,
    batch_mode: str,
    plan: Mapping[str, Any],
    database_before_fingerprint: DatabaseFingerprint | None = None,
    timestamp: str | None = None,
    batch_id_factory: Any = None,
) -> ImportBatch:
    """Adapt either manifest kind into the single v8.3-A batch constructor."""
    if subject.manifest_kind == "source":
        if (
            not isinstance(manifest, SourceManifest)
            or subject.source_manifest_id != manifest.manifest_id
        ):
            raise HistoricalSchemaError("manifest_subject_binding_mismatch")
        adapter_name = manifest.adapter_name
        adapter_version = manifest.adapter_version
    else:
        if (
            not isinstance(manifest, HistoricalSourceManifest)
            or subject.historical_manifest_id != manifest.historical_manifest_id
        ):
            raise HistoricalSchemaError("manifest_subject_binding_mismatch")
        adapter_name = manifest.adapter_name or "historical_reconstruction"
        adapter_version = (
            manifest.adapter_version or manifest.evidence_policy_version
        )
    view = _SubjectCoreView(
        manifest_id=subject.manifest_subject_id,
        source_type=manifest.source_type,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
    )
    return create_import_batch(
        view,  # type: ignore[arg-type]
        batch_mode=batch_mode,
        plan=plan,
        database_before_fingerprint=database_before_fingerprint,
        timestamp=timestamp,
        batch_id_factory=batch_id_factory,
    )


def execute_historical_operation_handler(*args: Any, **kwargs: Any) -> Any:
    """Historical operations enter the exact v8.3-E executor."""
    return execute_test_bundle(*args, **kwargs)


# Both source kinds bind to these exact v8.3-E authorities.  They are aliases,
# not historical variants.
validate_unified_approval = validate_approval_artifact
append_unified_execution_journal_event = append_execution_journal_event
acquire_unified_writer_lease = acquire_writer_lease
verify_unified_execution = verify_test_execution


@dataclass(frozen=True)
class HistoricalSourceBinding:
    binding_id: str
    manifest_subject_id: str
    unified_batch_id: str
    target_record_kind: str
    target_record_id: str
    proposed_source_type: str
    source_record_id_digest: str | None
    provenance_kind: str
    evidence_digest: str
    binding_state: str
    created_at: str
    binding_schema_version: str = HISTORICAL_SOURCE_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.binding_id, "historical_source_binding_", "invalid_binding_id")
        _identity(
            self.manifest_subject_id,
            "manifest_subject_",
            "invalid_manifest_subject_id",
        )
        _identity(self.unified_batch_id, "batch_", "invalid_unified_batch_id")
        if self.target_record_kind not in {"conversation", "message"}:
            raise HistoricalSchemaError("invalid_historical_target_record_kind")
        _private_reference(self.target_record_id, "invalid_target_record_id")
        _token(self.proposed_source_type, "invalid_proposed_source_type")
        source_digest = _optional_digest(
            self.source_record_id_digest,
            "invalid_source_record_id_digest",
        )
        if self.provenance_kind not in HISTORICAL_PROVENANCE_KINDS:
            raise HistoricalSchemaError("invalid_historical_provenance_kind")
        if self.binding_state not in HISTORICAL_BINDING_STATES:
            raise HistoricalSchemaError("invalid_historical_binding_state")
        if self.provenance_kind == "legacy_unknown" and (
            source_digest is not None or self.binding_state != "recorded_unknown"
        ):
            raise HistoricalSchemaError("legacy_unknown_binding_must_remain_unknown")
        if self.provenance_kind == "probable_legacy" and (
            self.binding_state not in {"deferred", "rejected"}
        ):
            raise HistoricalSchemaError("probable_binding_cannot_be_automatic")
        if (
            self.provenance_kind == "conflicting_evidence"
            and self.binding_state != "blocked"
        ):
            raise HistoricalSchemaError("conflicting_binding_must_be_blocked")
        if (
            self.provenance_kind == "deterministic_legacy"
            and source_digest is None
        ):
            raise HistoricalSchemaError(
                "deterministic_binding_requires_source_digest"
            )
        _digest(self.evidence_digest, "invalid_historical_evidence_digest")
        _timestamp(self.created_at, "invalid_historical_binding_created_at")
        if self.binding_id != _stable_id(
            "historical_source_binding_", self.identity_payload()
        ):
            raise HistoricalSchemaError("source_binding_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "identity_protocol": self.binding_schema_version,
            "manifest_subject_id": self.manifest_subject_id,
            "unified_batch_id": self.unified_batch_id,
            "target_record_kind": self.target_record_kind,
            "target_record_id_digest": normalized_json_digest({
                "target_record_id": self.target_record_id,
            }),
            "proposed_source_type": self.proposed_source_type,
            "source_record_id_digest": self.source_record_id_digest,
            "provenance_kind": self.provenance_kind,
            "evidence_digest": self.evidence_digest,
            "binding_state": self.binding_state,
        }


def build_historical_source_binding(**values: Any) -> HistoricalSourceBinding:
    payload = {
        "identity_protocol": HISTORICAL_SOURCE_BINDING_SCHEMA_VERSION,
        "manifest_subject_id": values["manifest_subject_id"],
        "unified_batch_id": values["unified_batch_id"],
        "target_record_kind": values["target_record_kind"],
        "target_record_id_digest": normalized_json_digest({
            "target_record_id": values["target_record_id"],
        }),
        "proposed_source_type": values["proposed_source_type"],
        "source_record_id_digest": values["source_record_id_digest"],
        "provenance_kind": values["provenance_kind"],
        "evidence_digest": values["evidence_digest"],
        "binding_state": values["binding_state"],
    }
    return HistoricalSourceBinding(
        binding_id=_stable_id("historical_source_binding_", payload),
        **values,
    )


@dataclass(frozen=True)
class HistoricalAttachmentBinding:
    binding_id: str
    manifest_subject_id: str
    unified_batch_id: str
    logical_media_id: str
    source_attachment_id_digest: str | None
    message_id: str
    entity_digest: str | None
    attachment_state: str
    size_conflict_state: str
    provenance_kind: str
    evidence_digest: str
    occurrence_index: int
    created_at: str
    binding_schema_version: str = HISTORICAL_ATTACHMENT_BINDING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(
            self.binding_id,
            "historical_attachment_binding_",
            "invalid_attachment_binding_id",
        )
        _identity(
            self.manifest_subject_id,
            "manifest_subject_",
            "invalid_manifest_subject_id",
        )
        _identity(self.unified_batch_id, "batch_", "invalid_unified_batch_id")
        if (
            not isinstance(self.logical_media_id, str)
            or _MEDIA_ID_RE.fullmatch(self.logical_media_id) is None
        ):
            raise HistoricalSchemaError("invalid_historical_logical_media_id")
        _optional_digest(
            self.source_attachment_id_digest,
            "invalid_source_attachment_digest",
        )
        _private_reference(self.message_id, "invalid_historical_message_id")
        entity_digest = _optional_digest(
            self.entity_digest,
            "invalid_historical_entity_digest",
        )
        if self.attachment_state not in HISTORICAL_ATTACHMENT_STATES:
            raise HistoricalSchemaError("invalid_historical_attachment_state")
        if self.size_conflict_state not in HISTORICAL_SIZE_CONFLICT_STATES:
            raise HistoricalSchemaError("invalid_size_conflict_state")
        if self.provenance_kind not in HISTORICAL_PROVENANCE_KINDS:
            raise HistoricalSchemaError("invalid_historical_provenance_kind")
        if self.attachment_state != "resolved" and entity_digest is not None:
            raise HistoricalSchemaError("nonresolved_entity_must_not_have_digest")
        if self.attachment_state == "resolved" and entity_digest is None:
            raise HistoricalSchemaError("resolved_entity_requires_digest")
        if (
            self.size_conflict_state == "size_conflict_unresolved"
            and entity_digest is not None
        ):
            raise HistoricalSchemaError(
                "unresolved_size_conflict_must_not_have_entity"
            )
        _digest(self.evidence_digest, "invalid_historical_evidence_digest")
        if (
            isinstance(self.occurrence_index, bool)
            or not isinstance(self.occurrence_index, int)
            or self.occurrence_index < 0
        ):
            raise HistoricalSchemaError("invalid_occurrence_index")
        _timestamp(self.created_at, "invalid_attachment_binding_created_at")
        if self.binding_id != _stable_id(
            "historical_attachment_binding_", self.identity_payload()
        ):
            raise HistoricalSchemaError("attachment_binding_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "identity_protocol": self.binding_schema_version,
            "manifest_subject_id": self.manifest_subject_id,
            "unified_batch_id": self.unified_batch_id,
            "logical_media_id": self.logical_media_id,
            "source_attachment_id_digest": self.source_attachment_id_digest,
            "message_id_digest": normalized_json_digest({
                "message_id": self.message_id,
            }),
            "entity_digest": self.entity_digest,
            "attachment_state": self.attachment_state,
            "size_conflict_state": self.size_conflict_state,
            "provenance_kind": self.provenance_kind,
            "evidence_digest": self.evidence_digest,
            "occurrence_index": self.occurrence_index,
        }


def build_historical_attachment_binding(
    **values: Any,
) -> HistoricalAttachmentBinding:
    payload = {
        "identity_protocol": HISTORICAL_ATTACHMENT_BINDING_SCHEMA_VERSION,
        "manifest_subject_id": values["manifest_subject_id"],
        "unified_batch_id": values["unified_batch_id"],
        "logical_media_id": values["logical_media_id"],
        "source_attachment_id_digest": values["source_attachment_id_digest"],
        "message_id_digest": normalized_json_digest({
            "message_id": values["message_id"],
        }),
        "entity_digest": values["entity_digest"],
        "attachment_state": values["attachment_state"],
        "size_conflict_state": values["size_conflict_state"],
        "provenance_kind": values["provenance_kind"],
        "evidence_digest": values["evidence_digest"],
        "occurrence_index": values["occurrence_index"],
    }
    return HistoricalAttachmentBinding(
        binding_id=_stable_id("historical_attachment_binding_", payload),
        **values,
    )


class UnifiedAuthorityRepository:
    """One SQLite repository for every v2 manifest kind and batch mode."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_subject(self, subject: ManifestSubject) -> None:
        self.connection.execute(
            """
            INSERT INTO ingestion_manifest_subjects(
                manifest_subject_id,manifest_kind,source_manifest_id,
                historical_manifest_id,subject_digest,evidence_policy_version,
                created_at,subject_schema_version
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                subject.manifest_subject_id,
                subject.manifest_kind,
                subject.source_manifest_id,
                subject.historical_manifest_id,
                subject.subject_digest,
                subject.evidence_policy_version,
                subject.created_at,
                subject.subject_schema_version,
            ),
        )

    def create_batch(
        self,
        subject: ManifestSubject,
        manifest: SourceManifest | HistoricalSourceManifest,
        *,
        batch_mode: str,
        plan: Mapping[str, Any],
        target_snapshot_fingerprint: str,
        timestamp: str,
        batch_id_factory: Any,
    ) -> ImportBatch:
        _digest(target_snapshot_fingerprint, "invalid_target_snapshot")
        batch = create_unified_import_batch(
            subject,
            manifest,
            batch_mode=batch_mode,
            plan=plan,
            timestamp=timestamp,
            batch_id_factory=batch_id_factory,
        )
        self.connection.execute(
            """
            INSERT INTO ingestion_batches_v2(
                batch_id,manifest_subject_id,source_type,batch_mode,
                current_state,plan_digest,approval_digest,
                target_snapshot_fingerprint,warning_count,error_count,
                conflict_count,event_sequence,recovery_status,created_at,
                updated_at,batch_schema_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                batch.batch_id,
                subject.manifest_subject_id,
                batch.source_type,
                batch.batch_mode,
                batch.current_state,
                batch.plan_digest,
                None,
                target_snapshot_fingerprint,
                batch.warning_count,
                batch.error_count,
                batch.conflict_count,
                batch.event_sequence,
                batch.recovery_status,
                batch.created_at,
                batch.updated_at,
                UNIFIED_BATCH_SCHEMA_VERSION,
            ),
        )
        return batch

    def append_event(self, event: BatchEvent) -> None:
        self.connection.execute(
            """
            INSERT INTO ingestion_batch_events_v2(
                batch_id,sequence,event_id,event_type,previous_state,next_state,
                occurred_at,actor_kind,reason_code,payload_digest,
                previous_event_digest,event_digest,event_schema_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.batch_id,
                event.sequence,
                event.event_id,
                event.event_type,
                event.previous_state,
                event.next_state,
                event.occurred_at,
                event.actor_kind,
                event.reason_code,
                event.payload_digest,
                event.previous_event_digest,
                event.event_digest,
                event.event_schema_version,
            ),
        )

    def update_batch_snapshot(self, batch: ImportBatch) -> None:
        self.connection.execute(
            """
            UPDATE ingestion_batches_v2
            SET current_state=?,approval_digest=?,warning_count=?,error_count=?,
                conflict_count=?,event_sequence=?,recovery_status=?,updated_at=?
            WHERE batch_id=?
            """,
            (
                batch.current_state,
                batch.approval_digest or None,
                batch.warning_count,
                batch.error_count,
                batch.conflict_count,
                batch.event_sequence,
                batch.recovery_status,
                batch.updated_at,
                batch.batch_id,
            ),
        )


# Active v8302 DDL.  v8301 objects are intentionally not changed.
UNIFIED_V2_TABLES = (
    "ingestion_historical_manifests",
    "ingestion_manifest_subjects",
    "ingestion_batches_v2",
    "ingestion_batch_events_v2",
    "ingestion_conflicts_v2",
    "ingestion_decisions_v2",
    "ingestion_approvals_v2",
    "ingestion_executions_v2",
    "ingestion_execution_events_v2",
    "ingestion_verifications_v2",
    "ingestion_leases_v2",
    "ingestion_historical_source_bindings",
    "ingestion_historical_attachment_bindings",
)


UNIFIED_V2_TABLE_DDL = (
    """
    CREATE TABLE ingestion_historical_manifests(
      historical_manifest_id TEXT PRIMARY KEY,
      source_type TEXT NOT NULL CHECK(source_type<>'' AND instr(source_type,'/')=0 AND instr(source_type,'\\')=0),
      provenance_kind TEXT NOT NULL CHECK(provenance_kind IN ('deterministic_legacy','probable_legacy','legacy_unknown','conflicting_evidence')),
      archive_state TEXT NOT NULL CHECK(archive_state='unavailable'),
      archive_sha256 TEXT CHECK(archive_sha256 IS NULL),
      archive_size INTEGER CHECK(archive_size IS NULL),
      adapter_name TEXT,
      adapter_version TEXT,
      evidence_policy_version TEXT NOT NULL,
      evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*' AND evidence_digest<>'0000000000000000000000000000000000000000000000000000000000000000'),
      reconstruction_basis_digest TEXT NOT NULL CHECK(length(reconstruction_basis_digest)=64 AND reconstruction_basis_digest NOT GLOB '*[^0-9a-f]*' AND reconstruction_basis_digest<>'0000000000000000000000000000000000000000000000000000000000000000'),
      validation_status TEXT NOT NULL,
      record_counts_json TEXT NOT NULL CHECK(json_valid(record_counts_json) AND json(record_counts_json)=record_counts_json),
      asset_counts_json TEXT NOT NULL CHECK(json_valid(asset_counts_json) AND json(asset_counts_json)=asset_counts_json),
      warnings_json TEXT NOT NULL CHECK(json_valid(warnings_json) AND json(warnings_json)=warnings_json),
      created_at TEXT NOT NULL,
      manifest_schema_version TEXT NOT NULL,
      CHECK((adapter_name IS NULL AND adapter_version IS NULL) OR (adapter_name IS NOT NULL AND adapter_name<>'' AND adapter_version IS NOT NULL AND adapter_version<>''))
    )
    """,
    """
    CREATE TABLE ingestion_manifest_subjects(
      manifest_subject_id TEXT PRIMARY KEY,
      manifest_kind TEXT NOT NULL CHECK(manifest_kind IN ('source','historical_reconstruction')),
      source_manifest_id TEXT,
      historical_manifest_id TEXT,
      subject_digest TEXT NOT NULL UNIQUE CHECK(length(subject_digest)=64 AND subject_digest NOT GLOB '*[^0-9a-f]*'),
      evidence_policy_version TEXT,
      created_at TEXT NOT NULL,
      subject_schema_version TEXT NOT NULL,
      FOREIGN KEY(source_manifest_id) REFERENCES ingestion_source_manifests(manifest_id),
      FOREIGN KEY(historical_manifest_id) REFERENCES ingestion_historical_manifests(historical_manifest_id),
      CHECK((manifest_kind='source' AND source_manifest_id IS NOT NULL AND historical_manifest_id IS NULL AND evidence_policy_version IS NULL) OR (manifest_kind='historical_reconstruction' AND source_manifest_id IS NULL AND historical_manifest_id IS NOT NULL AND evidence_policy_version IS NOT NULL))
    )
    """,
    """
    CREATE TABLE ingestion_batches_v2(
      batch_id TEXT PRIMARY KEY,
      manifest_subject_id TEXT NOT NULL,
      source_type TEXT NOT NULL,
      batch_mode TEXT NOT NULL CHECK(batch_mode IN ('full','incremental','historical_backfill','verification_only')),
      current_state TEXT NOT NULL CHECK(current_state IN ('discovered','validating','validation_failed','planned','awaiting_approval','approved','committing','verifying','completed','completed_with_warnings','failed','recovery_required','rolling_back','rolled_back','cancelled')),
      plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64 AND plan_digest NOT GLOB '*[^0-9a-f]*'),
      approval_digest TEXT CHECK(approval_digest IS NULL OR (length(approval_digest)=64 AND approval_digest NOT GLOB '*[^0-9a-f]*')),
      target_snapshot_fingerprint TEXT NOT NULL CHECK(length(target_snapshot_fingerprint)=64 AND target_snapshot_fingerprint NOT GLOB '*[^0-9a-f]*'),
      warning_count INTEGER NOT NULL CHECK(warning_count>=0),
      error_count INTEGER NOT NULL CHECK(error_count>=0),
      conflict_count INTEGER NOT NULL CHECK(conflict_count>=0),
      event_sequence INTEGER NOT NULL CHECK(event_sequence>=0),
      recovery_status TEXT NOT NULL CHECK(recovery_status IN ('not_required','required','rolling_back','rolled_back')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      batch_schema_version TEXT NOT NULL,
      FOREIGN KEY(manifest_subject_id) REFERENCES ingestion_manifest_subjects(manifest_subject_id)
    )
    """,
    """
    CREATE TABLE ingestion_batch_events_v2(
      batch_id TEXT NOT NULL,
      sequence INTEGER NOT NULL CHECK(sequence>=1),
      event_id TEXT NOT NULL UNIQUE,
      event_type TEXT NOT NULL,
      previous_state TEXT NOT NULL,
      next_state TEXT NOT NULL,
      occurred_at TEXT NOT NULL,
      actor_kind TEXT NOT NULL,
      reason_code TEXT NOT NULL,
      payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64 AND payload_digest NOT GLOB '*[^0-9a-f]*'),
      previous_event_digest TEXT NOT NULL CHECK(previous_event_digest='' OR (length(previous_event_digest)=64 AND previous_event_digest NOT GLOB '*[^0-9a-f]*')),
      event_digest TEXT NOT NULL UNIQUE CHECK(length(event_digest)=64 AND event_digest NOT GLOB '*[^0-9a-f]*'),
      event_schema_version TEXT NOT NULL,
      PRIMARY KEY(batch_id,sequence),
      FOREIGN KEY(batch_id) REFERENCES ingestion_batches_v2(batch_id)
    )
    """,
    """
    CREATE TABLE ingestion_conflicts_v2(
      conflict_id TEXT PRIMARY KEY,
      batch_id TEXT NOT NULL,
      conflict_type TEXT NOT NULL,
      risk_level TEXT NOT NULL,
      evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
      proposed_actions_json TEXT NOT NULL CHECK(json_valid(proposed_actions_json) AND json(proposed_actions_json)=proposed_actions_json),
      requires_approval INTEGER NOT NULL CHECK(requires_approval IN (0,1)),
      conflict_state TEXT NOT NULL,
      created_at TEXT NOT NULL,
      conflict_schema_version TEXT NOT NULL,
      FOREIGN KEY(batch_id) REFERENCES ingestion_batches_v2(batch_id)
    )
    """,
    """
    CREATE TABLE ingestion_decisions_v2(
      decision_id TEXT PRIMARY KEY,
      conflict_id TEXT NOT NULL UNIQUE,
      selected_action TEXT NOT NULL,
      allowed_operation_ids_json TEXT NOT NULL CHECK(json_valid(allowed_operation_ids_json) AND json(allowed_operation_ids_json)=allowed_operation_ids_json),
      rejected_operation_ids_json TEXT NOT NULL CHECK(json_valid(rejected_operation_ids_json) AND json(rejected_operation_ids_json)=rejected_operation_ids_json),
      evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
      decision_reason_code TEXT NOT NULL,
      actor_kind TEXT NOT NULL,
      created_at TEXT NOT NULL,
      decision_schema_version TEXT NOT NULL,
      FOREIGN KEY(conflict_id) REFERENCES ingestion_conflicts_v2(conflict_id)
    )
    """,
    """
    CREATE TABLE ingestion_approvals_v2(
      approval_id TEXT PRIMARY KEY,
      batch_id TEXT NOT NULL,
      manifest_subject_id TEXT NOT NULL,
      plan_id TEXT NOT NULL,
      plan_digest TEXT NOT NULL CHECK(length(plan_digest)=64 AND plan_digest NOT GLOB '*[^0-9a-f]*'),
      stage_plan_id TEXT NOT NULL,
      stage_plan_digest TEXT NOT NULL CHECK(length(stage_plan_digest)=64 AND stage_plan_digest NOT GLOB '*[^0-9a-f]*'),
      target_snapshot_fingerprint TEXT NOT NULL CHECK(length(target_snapshot_fingerprint)=64 AND target_snapshot_fingerprint NOT GLOB '*[^0-9a-f]*'),
      approval_digest TEXT NOT NULL UNIQUE CHECK(length(approval_digest)=64 AND approval_digest NOT GLOB '*[^0-9a-f]*'),
      approval_scope TEXT NOT NULL CHECK(approval_scope='exact_plan'),
      actor_kind TEXT NOT NULL,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      approval_schema_version TEXT NOT NULL,
      FOREIGN KEY(batch_id) REFERENCES ingestion_batches_v2(batch_id),
      FOREIGN KEY(manifest_subject_id) REFERENCES ingestion_manifest_subjects(manifest_subject_id)
    )
    """,
    """
    CREATE TABLE ingestion_executions_v2(
      execution_id TEXT PRIMARY KEY,
      batch_id TEXT NOT NULL,
      approval_digest TEXT NOT NULL,
      bundle_digest TEXT NOT NULL CHECK(length(bundle_digest)=64 AND bundle_digest NOT GLOB '*[^0-9a-f]*'),
      execution_state TEXT NOT NULL CHECK(execution_state IN ('approved','committing','verifying','completed','completed_with_warnings','failed','recovery_required','rolling_back','rolled_back')),
      database_before_fingerprint TEXT NOT NULL CHECK(length(database_before_fingerprint)=64 AND database_before_fingerprint NOT GLOB '*[^0-9a-f]*'),
      database_after_fingerprint TEXT CHECK(database_after_fingerprint IS NULL OR (length(database_after_fingerprint)=64 AND database_after_fingerprint NOT GLOB '*[^0-9a-f]*')),
      verification_digest TEXT CHECK(verification_digest IS NULL OR (length(verification_digest)=64 AND verification_digest NOT GLOB '*[^0-9a-f]*')),
      recovery_status TEXT NOT NULL CHECK(recovery_status IN ('not_required','required','rolling_back','rolled_back')),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      execution_schema_version TEXT NOT NULL,
      FOREIGN KEY(batch_id) REFERENCES ingestion_batches_v2(batch_id),
      FOREIGN KEY(approval_digest) REFERENCES ingestion_approvals_v2(approval_digest)
    )
    """,
    """
    CREATE TABLE ingestion_execution_events_v2(
      execution_id TEXT NOT NULL,
      sequence INTEGER NOT NULL CHECK(sequence>=1),
      event_id TEXT NOT NULL UNIQUE,
      event_type TEXT NOT NULL,
      previous_state TEXT NOT NULL,
      next_state TEXT NOT NULL,
      occurred_at TEXT NOT NULL,
      actor_kind TEXT NOT NULL,
      reason_code TEXT NOT NULL,
      payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64 AND payload_digest NOT GLOB '*[^0-9a-f]*'),
      previous_event_digest TEXT NOT NULL CHECK(previous_event_digest='' OR (length(previous_event_digest)=64 AND previous_event_digest NOT GLOB '*[^0-9a-f]*')),
      event_digest TEXT NOT NULL UNIQUE CHECK(length(event_digest)=64 AND event_digest NOT GLOB '*[^0-9a-f]*'),
      event_schema_version TEXT NOT NULL,
      PRIMARY KEY(execution_id,sequence),
      FOREIGN KEY(execution_id) REFERENCES ingestion_executions_v2(execution_id)
    )
    """,
    """
    CREATE TABLE ingestion_verifications_v2(
      verification_id TEXT PRIMARY KEY,
      execution_id TEXT NOT NULL UNIQUE,
      batch_id TEXT NOT NULL,
      passed INTEGER NOT NULL CHECK(passed IN (0,1)),
      invariant_results_json TEXT NOT NULL CHECK(json_valid(invariant_results_json) AND json(invariant_results_json)=invariant_results_json),
      actual_deltas_json TEXT NOT NULL CHECK(json_valid(actual_deltas_json) AND json(actual_deltas_json)=actual_deltas_json),
      expected_deltas_json TEXT NOT NULL CHECK(json_valid(expected_deltas_json) AND json(expected_deltas_json)=expected_deltas_json),
      database_after_fingerprint TEXT NOT NULL CHECK(length(database_after_fingerprint)=64 AND database_after_fingerprint NOT GLOB '*[^0-9a-f]*'),
      asset_after_fingerprint TEXT NOT NULL CHECK(length(asset_after_fingerprint)=64 AND asset_after_fingerprint NOT GLOB '*[^0-9a-f]*'),
      verification_digest TEXT NOT NULL UNIQUE CHECK(length(verification_digest)=64 AND verification_digest NOT GLOB '*[^0-9a-f]*'),
      created_at TEXT NOT NULL,
      verification_schema_version TEXT NOT NULL,
      FOREIGN KEY(execution_id) REFERENCES ingestion_executions_v2(execution_id),
      FOREIGN KEY(batch_id) REFERENCES ingestion_batches_v2(batch_id)
    )
    """,
    """
    CREATE TABLE ingestion_leases_v2(
      lease_id TEXT PRIMARY KEY,
      target_writer_digest TEXT NOT NULL CHECK(length(target_writer_digest)=64 AND target_writer_digest NOT GLOB '*[^0-9a-f]*'),
      batch_id TEXT NOT NULL,
      lease_token_digest TEXT NOT NULL CHECK(length(lease_token_digest)=64 AND lease_token_digest NOT GLOB '*[^0-9a-f]*'),
      acquired_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      released_at TEXT,
      lease_schema_version TEXT NOT NULL,
      FOREIGN KEY(batch_id) REFERENCES ingestion_batches_v2(batch_id)
    )
    """,
    """
    CREATE TABLE ingestion_historical_source_bindings(
      binding_id TEXT PRIMARY KEY,
      manifest_subject_id TEXT NOT NULL,
      unified_batch_id TEXT NOT NULL,
      target_record_kind TEXT NOT NULL CHECK(target_record_kind IN ('conversation','message')),
      target_record_id TEXT NOT NULL CHECK(target_record_id<>'' AND substr(target_record_id,1,1) NOT IN ('/','\\') AND NOT (length(target_record_id)>=3 AND substr(target_record_id,2,1)=':' AND substr(target_record_id,3,1) IN ('/','\\')) AND instr(target_record_id,char(0))=0),
      proposed_source_type TEXT NOT NULL,
      source_record_id_digest TEXT CHECK(source_record_id_digest IS NULL OR (length(source_record_id_digest)=64 AND source_record_id_digest NOT GLOB '*[^0-9a-f]*' AND source_record_id_digest<>'0000000000000000000000000000000000000000000000000000000000000000')),
      provenance_kind TEXT NOT NULL CHECK(provenance_kind IN ('deterministic_legacy','probable_legacy','legacy_unknown','conflicting_evidence')),
      evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
      binding_state TEXT NOT NULL CHECK(binding_state IN ('approved','applied','verified','deferred','rejected','recorded_unknown','blocked')),
      created_at TEXT NOT NULL,
      binding_schema_version TEXT NOT NULL,
      FOREIGN KEY(manifest_subject_id) REFERENCES ingestion_manifest_subjects(manifest_subject_id),
      FOREIGN KEY(unified_batch_id) REFERENCES ingestion_batches_v2(batch_id),
      CHECK(provenance_kind<>'legacy_unknown' OR (source_record_id_digest IS NULL AND binding_state='recorded_unknown')),
      CHECK(provenance_kind<>'probable_legacy' OR binding_state IN ('deferred','rejected')),
      CHECK(provenance_kind<>'conflicting_evidence' OR binding_state='blocked'),
      CHECK(provenance_kind<>'deterministic_legacy' OR source_record_id_digest IS NOT NULL)
    )
    """,
    """
    CREATE TABLE ingestion_historical_attachment_bindings(
      binding_id TEXT PRIMARY KEY,
      manifest_subject_id TEXT NOT NULL,
      unified_batch_id TEXT NOT NULL,
      logical_media_id TEXT NOT NULL CHECK(logical_media_id GLOB 'ym1_[a-z2-7]*' AND logical_media_id NOT GLOB '*[^a-z0-9_]*'),
      source_attachment_id_digest TEXT CHECK(source_attachment_id_digest IS NULL OR (length(source_attachment_id_digest)=64 AND source_attachment_id_digest NOT GLOB '*[^0-9a-f]*' AND source_attachment_id_digest<>'0000000000000000000000000000000000000000000000000000000000000000')),
      message_id TEXT NOT NULL CHECK(message_id<>'' AND substr(message_id,1,1) NOT IN ('/','\\') AND NOT (length(message_id)>=3 AND substr(message_id,2,1)=':' AND substr(message_id,3,1) IN ('/','\\')) AND instr(message_id,char(0))=0),
      entity_digest TEXT CHECK(entity_digest IS NULL OR (length(entity_digest)=64 AND entity_digest NOT GLOB '*[^0-9a-f]*' AND entity_digest<>'0000000000000000000000000000000000000000000000000000000000000000')),
      attachment_state TEXT NOT NULL CHECK(attachment_state IN ('resolved','metadata_only','asset_record_missing','entity_missing','unresolved')),
      size_conflict_state TEXT NOT NULL CHECK(size_conflict_state IN ('clear','size_conflict_unresolved')),
      provenance_kind TEXT NOT NULL CHECK(provenance_kind IN ('deterministic_legacy','probable_legacy','legacy_unknown','conflicting_evidence')),
      evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64 AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
      occurrence_index INTEGER NOT NULL CHECK(occurrence_index>=0),
      created_at TEXT NOT NULL,
      binding_schema_version TEXT NOT NULL,
      UNIQUE(unified_batch_id,logical_media_id,message_id,occurrence_index),
      FOREIGN KEY(manifest_subject_id) REFERENCES ingestion_manifest_subjects(manifest_subject_id),
      FOREIGN KEY(unified_batch_id) REFERENCES ingestion_batches_v2(batch_id),
      CHECK((attachment_state='resolved' AND entity_digest IS NOT NULL) OR (attachment_state<>'resolved' AND entity_digest IS NULL)),
      CHECK(size_conflict_state<>'size_conflict_unresolved' OR entity_digest IS NULL)
    )
    """,
)


UNIFIED_V2_INDEX_DDL = (
    "CREATE INDEX idx_historical_manifests_evidence ON ingestion_historical_manifests(evidence_digest)",
    "CREATE UNIQUE INDEX idx_manifest_subject_source ON ingestion_manifest_subjects(source_manifest_id) WHERE source_manifest_id IS NOT NULL",
    "CREATE UNIQUE INDEX idx_manifest_subject_historical ON ingestion_manifest_subjects(historical_manifest_id) WHERE historical_manifest_id IS NOT NULL",
    "CREATE INDEX idx_manifest_subject_digest ON ingestion_manifest_subjects(subject_digest)",
    "CREATE INDEX idx_batches_v2_plan ON ingestion_batches_v2(plan_digest,target_snapshot_fingerprint)",
    "CREATE INDEX idx_batch_events_v2_digest ON ingestion_batch_events_v2(event_digest)",
    "CREATE INDEX idx_conflicts_v2_batch ON ingestion_conflicts_v2(batch_id,conflict_state)",
    "CREATE INDEX idx_executions_v2_batch ON ingestion_executions_v2(batch_id,execution_state)",
    "CREATE UNIQUE INDEX idx_leases_v2_active_writer ON ingestion_leases_v2(target_writer_digest) WHERE released_at IS NULL",
    "CREATE INDEX idx_historical_source_bindings_target ON ingestion_historical_source_bindings(target_record_kind,target_record_id)",
    "CREATE INDEX idx_historical_source_bindings_source ON ingestion_historical_source_bindings(proposed_source_type,source_record_id_digest)",
    "CREATE INDEX idx_historical_attachment_bindings_message ON ingestion_historical_attachment_bindings(message_id)",
    "CREATE INDEX idx_historical_attachment_bindings_media ON ingestion_historical_attachment_bindings(logical_media_id)",
)


def _append_only_triggers(table: str) -> tuple[str, str]:
    return (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'{table}_append_only'); END",
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'{table}_append_only'); END",
    )


_APPEND_ONLY_V2 = (
    "ingestion_historical_manifests",
    "ingestion_manifest_subjects",
    "ingestion_batch_events_v2",
    "ingestion_conflicts_v2",
    "ingestion_decisions_v2",
    "ingestion_approvals_v2",
    "ingestion_execution_events_v2",
    "ingestion_verifications_v2",
    "ingestion_historical_source_bindings",
    "ingestion_historical_attachment_bindings",
)


UNIFIED_V2_TRIGGER_DDL = (
    tuple(
        trigger
        for table in _APPEND_ONLY_V2
        for trigger in _append_only_triggers(table)
    )
    + (
        "CREATE TRIGGER ingestion_batches_v2_no_delete BEFORE DELETE ON ingestion_batches_v2 BEGIN SELECT RAISE(ABORT,'ingestion_batches_v2_append_only'); END",
        """
        CREATE TRIGGER ingestion_batches_v2_immutable_identity BEFORE UPDATE ON ingestion_batches_v2
        WHEN OLD.batch_id<>NEW.batch_id OR OLD.manifest_subject_id<>NEW.manifest_subject_id OR OLD.source_type<>NEW.source_type OR OLD.batch_mode<>NEW.batch_mode OR OLD.plan_digest<>NEW.plan_digest OR OLD.target_snapshot_fingerprint<>NEW.target_snapshot_fingerprint OR OLD.created_at<>NEW.created_at OR OLD.batch_schema_version<>NEW.batch_schema_version
        BEGIN SELECT RAISE(ABORT,'ingestion_batches_v2_immutable_identity'); END
        """,
        "CREATE TRIGGER ingestion_executions_v2_no_delete BEFORE DELETE ON ingestion_executions_v2 BEGIN SELECT RAISE(ABORT,'ingestion_executions_v2_append_only'); END",
        """
        CREATE TRIGGER ingestion_executions_v2_immutable_identity BEFORE UPDATE ON ingestion_executions_v2
        WHEN OLD.execution_id<>NEW.execution_id OR OLD.batch_id<>NEW.batch_id OR OLD.approval_digest<>NEW.approval_digest OR OLD.bundle_digest<>NEW.bundle_digest OR OLD.database_before_fingerprint<>NEW.database_before_fingerprint OR OLD.created_at<>NEW.created_at OR OLD.execution_schema_version<>NEW.execution_schema_version
        BEGIN SELECT RAISE(ABORT,'ingestion_executions_v2_immutable_identity'); END
        """,
        "CREATE TRIGGER ingestion_leases_v2_no_delete BEFORE DELETE ON ingestion_leases_v2 BEGIN SELECT RAISE(ABORT,'ingestion_leases_v2_append_only'); END",
        """
        CREATE TRIGGER ingestion_leases_v2_immutable_identity BEFORE UPDATE ON ingestion_leases_v2
        WHEN OLD.lease_id<>NEW.lease_id OR OLD.target_writer_digest<>NEW.target_writer_digest OR OLD.batch_id<>NEW.batch_id OR OLD.lease_token_digest<>NEW.lease_token_digest OR OLD.acquired_at<>NEW.acquired_at OR OLD.expires_at<>NEW.expires_at OR OLD.lease_schema_version<>NEW.lease_schema_version
        BEGIN SELECT RAISE(ABORT,'ingestion_leases_v2_immutable_identity'); END
        """,
        """
        CREATE TRIGGER ingestion_batch_events_v2_chain BEFORE INSERT ON ingestion_batch_events_v2
        WHEN (NEW.sequence=1 AND NEW.previous_event_digest<>'') OR
             (NEW.sequence>1 AND NOT EXISTS(SELECT 1 FROM ingestion_batch_events_v2 p WHERE p.batch_id=NEW.batch_id AND p.sequence=NEW.sequence-1 AND p.event_digest=NEW.previous_event_digest))
        BEGIN SELECT RAISE(ABORT,'batch_event_v2_chain_mismatch'); END
        """,
        """
        CREATE TRIGGER ingestion_execution_events_v2_chain BEFORE INSERT ON ingestion_execution_events_v2
        WHEN (NEW.sequence=1 AND NEW.previous_event_digest<>'') OR
             (NEW.sequence>1 AND NOT EXISTS(SELECT 1 FROM ingestion_execution_events_v2 p WHERE p.execution_id=NEW.execution_id AND p.sequence=NEW.sequence-1 AND p.event_digest=NEW.previous_event_digest))
        BEGIN SELECT RAISE(ABORT,'execution_event_v2_chain_mismatch'); END
        """,
        """
        CREATE TRIGGER ingestion_approvals_v2_exact_plan BEFORE INSERT ON ingestion_approvals_v2
        WHEN NOT EXISTS(SELECT 1 FROM ingestion_batches_v2 b WHERE b.batch_id=NEW.batch_id AND b.manifest_subject_id=NEW.manifest_subject_id AND b.plan_digest=NEW.plan_digest AND b.target_snapshot_fingerprint=NEW.target_snapshot_fingerprint)
        BEGIN SELECT RAISE(ABORT,'approval_v2_exact_plan_mismatch'); END
        """,
        """
        CREATE TRIGGER ingestion_executions_v2_exact_approval BEFORE INSERT ON ingestion_executions_v2
        WHEN NOT EXISTS(SELECT 1 FROM ingestion_approvals_v2 a WHERE a.batch_id=NEW.batch_id AND a.approval_digest=NEW.approval_digest AND a.approval_scope='exact_plan')
        BEGIN SELECT RAISE(ABORT,'execution_v2_approval_mismatch'); END
        """,
        """
        CREATE TRIGGER ingestion_verifications_v2_exact_execution BEFORE INSERT ON ingestion_verifications_v2
        WHEN NOT EXISTS(SELECT 1 FROM ingestion_executions_v2 e WHERE e.execution_id=NEW.execution_id AND e.batch_id=NEW.batch_id)
        BEGIN SELECT RAISE(ABORT,'verification_v2_execution_mismatch'); END
        """,
    )
)
