"""Deterministic historical evidence and planning for YunOS v8.3-G.

The module is deliberately read-only.  It may inspect the authenticated
production database with ``mode=ro&immutable=1`` and build in-memory evidence,
manifests, subjects, candidates, conflicts, and plans.  SQL execution belongs
to :mod:`memory.ingestion_backfill_execution`.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import re
import sqlite3
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from memory.ingestion_core import make_plan_digest, normalized_json_digest
from memory.ingestion_historical_schema import (
    HistoricalSourceManifest,
    ManifestSubject,
    build_historical_source_manifest,
    build_manifest_subject,
)
from memory.ingestion_migration_rehearsal import capture_legacy_compatibility
from memory.media_index import EntityState, MediaIndexRecord, build_media_index


EVIDENCE_POLICY_VERSION = "v8.3-g-historical-evidence-1"
EVIDENCE_SCHEMA_VERSION = "v8.3-g-historical-evidence-1"
CANDIDATE_SCHEMA_VERSION = "v8.3-g-historical-candidate-1"
ATTACHMENT_CANDIDATE_SCHEMA_VERSION = "v8.3-g-attachment-candidate-1"
CONFLICT_SCHEMA_VERSION = "v8.3-g-historical-conflict-1"
PLAN_SCHEMA_VERSION = "v8.3-g-historical-backfill-plan-1"
RESULT_SCHEMA_VERSION = "v8.3-g-historical-backfill-result-1"
TARGET_SNAPSHOT_SCHEMA_VERSION = "v8.3-g-target-snapshot-1"
ADAPTER_IDENTITY_POLICY_VERSION = "v8.3-c-adapter-identity-1"
MEDIA_IDENTITY_POLICY_VERSION = "v8.2-e4-1a-media-identity-1"

EVIDENCE_CLASSIFICATIONS = frozenset({
    "native_authoritative",
    "deterministic_legacy",
    "probable_legacy",
    "legacy_unknown",
    "conflicting_evidence",
    "unsupported_source",
    "invalid_relationship",
})
EXECUTABLE_OPERATION_TYPES = frozenset({
    "verify_native_source_binding",
    "create_legacy_source_binding",
    "create_historical_attachment_binding",
    "verify_media_identity",
    "record_legacy_unknown",
    "record_backfill_conflict",
    "flag_probable_binding",
    "skip_existing_binding",
})
FORBIDDEN_OPERATION_TYPES = frozenset({
    "update_conversation",
    "update_message",
    "rewrite_source",
    "update_source_id",
    "delete_conversation",
    "delete_message",
    "create_asset_entity",
    "promote_asset",
    "merge_records",
    "update_attachment_json",
    "modify_old_table",
})
SUPPORTED_SOURCES = frozenset({
    "chatgpt",
    "deepseek",
    "claude",
    "gemini",
    "manual",
    "markdown",
    "txt",
})

_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")
_MEDIA_RE = re.compile(r"ym1_[a-z2-7]{52}\Z")
_ZERO_SHA = "0" * 64


class HistoricalBackfillError(RuntimeError):
    """Fail-closed error carrying a sanitized reason code."""

    def __init__(self, reason_code: str) -> None:
        if not isinstance(reason_code, str) or _TOKEN_RE.fullmatch(reason_code) is None:
            reason_code = "historical_backfill_failed"
        super().__init__(reason_code)
        self.reason_code = reason_code


def _sha(value: Any, reason: str) -> str:
    if (
        not isinstance(value, str)
        or _SHA_RE.fullmatch(value) is None
        or value == _ZERO_SHA
    ):
        raise HistoricalBackfillError(reason)
    return value


def _identity(value: Any, prefix: str, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or _IDENTITY_RE.fullmatch(value) is None
    ):
        raise HistoricalBackfillError(reason)
    return value


def _token(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise HistoricalBackfillError(reason)
    return value


def _private_ref(value: Any, reason: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or value.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", value)
    ):
        raise HistoricalBackfillError(reason)
    return value


def _timestamp(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise HistoricalBackfillError(reason)
    return value


def _frozen_counts(value: Mapping[str, int], reason: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise HistoricalBackfillError(reason)
    result: dict[str, int] = {}
    for key, count in value.items():
        _token(key, reason)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise HistoricalBackfillError(reason)
        result[key] = count
    return MappingProxyType(dict(sorted(result.items())))


def _digest_ref(kind: str, value: str) -> str:
    return normalized_json_digest({"record_kind": kind, "private_id": value})


def _source_identity_digest(kind: str, source: str, value: str) -> str:
    return normalized_json_digest({
        "identity_policy_version": ADAPTER_IDENTITY_POLICY_VERSION,
        "record_kind": kind,
        "source_type": source,
        "source_record_id": value,
    })


@dataclass(frozen=True)
class HistoricalEvidence:
    evidence_id: str
    evidence_type: str
    target_record_kind: str
    target_record_id_digest: str
    source_type_candidate: str | None
    source_record_id_digest: str | None
    evidence_digest: str
    evidence_strength: str
    reason_code: str
    evidence_schema_version: str = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.evidence_id, "hevidence_", "invalid_evidence_id")
        _token(self.evidence_type, "invalid_evidence_type")
        if self.target_record_kind not in {"conversation", "message", "attachment"}:
            raise HistoricalBackfillError("invalid_evidence_target_kind")
        _sha(self.target_record_id_digest, "invalid_target_record_digest")
        if self.source_type_candidate is not None:
            _token(self.source_type_candidate, "invalid_evidence_source_type")
        if self.source_record_id_digest is not None:
            _sha(self.source_record_id_digest, "invalid_source_record_digest")
        _sha(self.evidence_digest, "invalid_evidence_digest")
        if self.evidence_strength not in EVIDENCE_CLASSIFICATIONS:
            raise HistoricalBackfillError("invalid_evidence_strength")
        _token(self.reason_code, "invalid_evidence_reason")
        if self.evidence_schema_version != EVIDENCE_SCHEMA_VERSION:
            raise HistoricalBackfillError("evidence_schema_version_mismatch")
        if self.evidence_id != "hevidence_" + self.evidence_digest:
            raise HistoricalBackfillError("evidence_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "target_record_kind": self.target_record_kind,
            "target_record_id_digest": self.target_record_id_digest,
            "source_type_candidate": self.source_type_candidate,
            "source_record_id_digest": self.source_record_id_digest,
            "evidence_strength": self.evidence_strength,
            "reason_code": self.reason_code,
            "evidence_schema_version": self.evidence_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            **self.identity_payload(),
            "evidence_digest": self.evidence_digest,
        }


def make_historical_evidence(**values: Any) -> HistoricalEvidence:
    payload = {
        key: values[key]
        for key in (
            "evidence_type",
            "target_record_kind",
            "target_record_id_digest",
            "source_type_candidate",
            "source_record_id_digest",
            "evidence_strength",
            "reason_code",
        )
    }
    payload["evidence_schema_version"] = EVIDENCE_SCHEMA_VERSION
    digest = normalized_json_digest(payload)
    return HistoricalEvidence(
        evidence_id="hevidence_" + digest,
        evidence_digest=digest,
        **payload,
    )


@dataclass(frozen=True)
class HistoricalBindingCandidate:
    candidate_id: str
    target_record_kind: str
    target_record_ref: str = field(repr=False)
    proposed_source_type: str
    proposed_source_record_id: str | None = field(repr=False)
    provenance_kind: str
    evidence_ids: tuple[str, ...]
    binding_digest: str
    requires_approval: bool
    conflict_state: str
    candidate_schema_version: str = CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.candidate_id, "hcandidate_", "invalid_candidate_id")
        if self.target_record_kind not in {"conversation", "message"}:
            raise HistoricalBackfillError("invalid_candidate_target_kind")
        _private_ref(self.target_record_ref, "invalid_candidate_target_ref")
        _token(self.proposed_source_type, "invalid_candidate_source_type")
        if self.proposed_source_record_id is not None:
            _private_ref(
                self.proposed_source_record_id,
                "invalid_candidate_source_record_id",
            )
        if self.provenance_kind not in EVIDENCE_CLASSIFICATIONS:
            raise HistoricalBackfillError("invalid_candidate_provenance")
        evidence = tuple(sorted(set(self.evidence_ids)))
        for value in evidence:
            _identity(value, "hevidence_", "invalid_candidate_evidence_id")
        if not evidence:
            raise HistoricalBackfillError("candidate_evidence_missing")
        object.__setattr__(self, "evidence_ids", evidence)
        _sha(self.binding_digest, "invalid_candidate_binding_digest")
        if not isinstance(self.requires_approval, bool):
            raise HistoricalBackfillError("invalid_candidate_approval_flag")
        if self.conflict_state not in {"clear", "deferred", "blocked"}:
            raise HistoricalBackfillError("invalid_candidate_conflict_state")
        if self.provenance_kind == "native_authoritative" and self.requires_approval:
            raise HistoricalBackfillError("native_candidate_requires_no_approval")
        if self.provenance_kind == "deterministic_legacy" and not self.requires_approval:
            raise HistoricalBackfillError("legacy_candidate_requires_approval")
        if self.provenance_kind == "probable_legacy" and self.conflict_state != "deferred":
            raise HistoricalBackfillError("probable_candidate_must_defer")
        if self.provenance_kind == "conflicting_evidence" and self.conflict_state != "blocked":
            raise HistoricalBackfillError("conflicting_candidate_must_block")
        if self.candidate_id != "hcandidate_" + self.binding_digest:
            raise HistoricalBackfillError("candidate_identity_mismatch")

    @property
    def source_record_id_digest(self) -> str | None:
        if self.proposed_source_record_id is None:
            return None
        return _source_identity_digest(
            self.target_record_kind,
            self.proposed_source_type,
            self.proposed_source_record_id,
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "target_record_kind": self.target_record_kind,
            "target_record_id_digest": _digest_ref(
                self.target_record_kind,
                self.target_record_ref,
            ),
            "proposed_source_type": self.proposed_source_type,
            "source_record_id_digest": self.source_record_id_digest,
            "provenance_kind": self.provenance_kind,
            "evidence_ids": list(self.evidence_ids),
            "requires_approval": self.requires_approval,
            "conflict_state": self.conflict_state,
            "candidate_schema_version": self.candidate_schema_version,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            **self.identity_payload(),
            "binding_digest": self.binding_digest,
        }


def make_binding_candidate(**values: Any) -> HistoricalBindingCandidate:
    provisional = HistoricalBindingCandidate.__new__(HistoricalBindingCandidate)
    # Reuse the canonical payload implementation without ever placing raw IDs
    # in a persisted plan summary.
    payload = {
        "target_record_kind": values["target_record_kind"],
        "target_record_id_digest": _digest_ref(
            values["target_record_kind"],
            values["target_record_ref"],
        ),
        "proposed_source_type": values["proposed_source_type"],
        "source_record_id_digest": (
            _source_identity_digest(
                values["target_record_kind"],
                values["proposed_source_type"],
                values["proposed_source_record_id"],
            )
            if values["proposed_source_record_id"] is not None else None
        ),
        "provenance_kind": values["provenance_kind"],
        "evidence_ids": sorted(set(values["evidence_ids"])),
        "requires_approval": values["requires_approval"],
        "conflict_state": values["conflict_state"],
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
    }
    del provisional
    digest = normalized_json_digest(payload)
    return HistoricalBindingCandidate(
        candidate_id="hcandidate_" + digest,
        binding_digest=digest,
        **values,
    )


@dataclass(frozen=True)
class HistoricalAttachmentBindingCandidate:
    candidate_id: str
    logical_media_id: str
    message_id: str = field(repr=False)
    source_attachment_id: str | None = field(repr=False)
    occurrence_index: int
    attachment_state: str
    size_conflict_state: str
    provenance_kind: str
    evidence_id: str
    evidence_digest: str
    binding_digest: str
    candidate_schema_version: str = ATTACHMENT_CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.candidate_id, "hattachcandidate_", "invalid_attachment_candidate")
        if _MEDIA_RE.fullmatch(self.logical_media_id) is None:
            raise HistoricalBackfillError("invalid_attachment_media_id")
        _private_ref(self.message_id, "invalid_attachment_message_id")
        if self.source_attachment_id is not None:
            _private_ref(
                self.source_attachment_id,
                "invalid_source_attachment_id",
            )
        if (
            isinstance(self.occurrence_index, bool)
            or not isinstance(self.occurrence_index, int)
            or self.occurrence_index < 0
        ):
            raise HistoricalBackfillError("invalid_attachment_occurrence")
        if self.attachment_state not in {
            "metadata_only",
            "asset_record_missing",
            "entity_missing",
            "unresolved",
        }:
            raise HistoricalBackfillError("invalid_attachment_state")
        if self.size_conflict_state not in {"clear", "size_conflict_unresolved"}:
            raise HistoricalBackfillError("invalid_attachment_size_conflict")
        if self.size_conflict_state == "size_conflict_unresolved" and (
            self.attachment_state != "unresolved"
        ):
            raise HistoricalBackfillError("size_conflict_must_remain_unresolved")
        if self.provenance_kind not in {
            "deterministic_legacy",
            "probable_legacy",
            "legacy_unknown",
            "conflicting_evidence",
        }:
            raise HistoricalBackfillError("invalid_attachment_provenance")
        _identity(self.evidence_id, "hevidence_", "invalid_attachment_evidence")
        _sha(self.evidence_digest, "invalid_attachment_evidence_digest")
        _sha(self.binding_digest, "invalid_attachment_binding_digest")
        if self.candidate_id != "hattachcandidate_" + self.binding_digest:
            raise HistoricalBackfillError("attachment_candidate_identity_mismatch")

    @property
    def source_attachment_id_digest(self) -> str | None:
        if self.source_attachment_id is None:
            return None
        return normalized_json_digest({
            "media_identity_policy_version": MEDIA_IDENTITY_POLICY_VERSION,
            "source_attachment_id": self.source_attachment_id,
        })

    def identity_payload(self) -> dict[str, Any]:
        return {
            "logical_media_id": self.logical_media_id,
            "message_id_digest": _digest_ref("message", self.message_id),
            "source_attachment_id_digest": self.source_attachment_id_digest,
            "occurrence_index": self.occurrence_index,
            "attachment_state": self.attachment_state,
            "size_conflict_state": self.size_conflict_state,
            "provenance_kind": self.provenance_kind,
            "evidence_id": self.evidence_id,
            "evidence_digest": self.evidence_digest,
            "candidate_schema_version": self.candidate_schema_version,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            **self.identity_payload(),
            "binding_digest": self.binding_digest,
        }


def make_attachment_candidate(**values: Any) -> HistoricalAttachmentBindingCandidate:
    source_digest = (
        normalized_json_digest({
            "media_identity_policy_version": MEDIA_IDENTITY_POLICY_VERSION,
            "source_attachment_id": values["source_attachment_id"],
        })
        if values["source_attachment_id"] is not None else None
    )
    payload = {
        "logical_media_id": values["logical_media_id"],
        "message_id_digest": _digest_ref("message", values["message_id"]),
        "source_attachment_id_digest": source_digest,
        "occurrence_index": values["occurrence_index"],
        "attachment_state": values["attachment_state"],
        "size_conflict_state": values["size_conflict_state"],
        "provenance_kind": values["provenance_kind"],
        "evidence_id": values["evidence_id"],
        "evidence_digest": values["evidence_digest"],
        "candidate_schema_version": ATTACHMENT_CANDIDATE_SCHEMA_VERSION,
    }
    digest = normalized_json_digest(payload)
    return HistoricalAttachmentBindingCandidate(
        candidate_id="hattachcandidate_" + digest,
        binding_digest=digest,
        **values,
    )


@dataclass(frozen=True)
class HistoricalBackfillConflict:
    conflict_id: str
    conflict_type: str
    severity: str
    target_refs: tuple[str, ...] = field(repr=False)
    candidate_refs: tuple[str, ...]
    evidence_digests: tuple[str, ...]
    proposed_actions: tuple[str, ...]
    default_action: str
    requires_approval: bool
    reason_code: str
    conflict_schema_version: str = CONFLICT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.conflict_id, "hconflict_", "invalid_conflict_id")
        _token(self.conflict_type, "invalid_conflict_type")
        if self.severity not in {"low", "medium", "high", "critical"}:
            raise HistoricalBackfillError("invalid_conflict_severity")
        for value in self.target_refs:
            _private_ref(value, "invalid_conflict_target")
        for value in self.candidate_refs:
            _identity(value, "hcandidate_", "invalid_conflict_candidate")
        for value in self.evidence_digests:
            _sha(value, "invalid_conflict_evidence")
        actions = tuple(sorted(set(self.proposed_actions)))
        for value in actions:
            _token(value, "invalid_conflict_action")
        if not actions or self.default_action not in actions:
            raise HistoricalBackfillError("invalid_conflict_default_action")
        object.__setattr__(self, "proposed_actions", actions)
        if not isinstance(self.requires_approval, bool):
            raise HistoricalBackfillError("invalid_conflict_approval")
        _token(self.reason_code, "invalid_conflict_reason")
        expected = normalized_json_digest(self.identity_payload())
        if self.conflict_id != "hconflict_" + expected:
            raise HistoricalBackfillError("conflict_identity_mismatch")

    @property
    def risk_level(self) -> str:
        return self.severity

    @property
    def allowed_decisions(self) -> tuple[str, ...]:
        return self.proposed_actions

    @property
    def evidence_hashes(self) -> tuple[str, ...]:
        return self.evidence_digests

    def identity_payload(self) -> dict[str, Any]:
        return {
            "conflict_type": self.conflict_type,
            "severity": self.severity,
            "target_ref_digests": sorted(
                normalized_json_digest({"private_target": value})
                for value in self.target_refs
            ),
            "candidate_refs": sorted(self.candidate_refs),
            "evidence_digests": sorted(self.evidence_digests),
            "proposed_actions": list(self.proposed_actions),
            "default_action": self.default_action,
            "requires_approval": self.requires_approval,
            "reason_code": self.reason_code,
            "conflict_schema_version": self.conflict_schema_version,
        }

    def summary(self) -> dict[str, Any]:
        return {"conflict_id": self.conflict_id, **self.identity_payload()}


def make_backfill_conflict(**values: Any) -> HistoricalBackfillConflict:
    payload = {
        "conflict_type": values["conflict_type"],
        "severity": values["severity"],
        "target_ref_digests": sorted(
            normalized_json_digest({"private_target": value})
            for value in values["target_refs"]
        ),
        "candidate_refs": sorted(values["candidate_refs"]),
        "evidence_digests": sorted(values["evidence_digests"]),
        "proposed_actions": sorted(set(values["proposed_actions"])),
        "default_action": values["default_action"],
        "requires_approval": values["requires_approval"],
        "reason_code": values["reason_code"],
        "conflict_schema_version": CONFLICT_SCHEMA_VERSION,
    }
    return HistoricalBackfillConflict(
        conflict_id="hconflict_" + normalized_json_digest(payload),
        **values,
    )


@dataclass(frozen=True)
class HistoricalTargetSnapshot:
    target_snapshot_fingerprint: str
    schema_fingerprint: str
    logical_contract_digest: str
    database_content_sha256: str
    counts: Mapping[str, int]
    source_distribution: Mapping[str, int]
    compatibility: Mapping[str, Any] = field(repr=False)
    snapshot_schema_version: str = TARGET_SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, reason in (
            (self.target_snapshot_fingerprint, "invalid_target_snapshot"),
            (self.schema_fingerprint, "invalid_schema_fingerprint"),
            (self.logical_contract_digest, "invalid_logical_contract"),
            (self.database_content_sha256, "invalid_database_digest"),
        ):
            _sha(value, reason)
        object.__setattr__(
            self,
            "counts",
            _frozen_counts(self.counts, "invalid_snapshot_counts"),
        )
        object.__setattr__(
            self,
            "source_distribution",
            _frozen_counts(
                self.source_distribution,
                "invalid_source_distribution",
            ),
        )
        if self.snapshot_schema_version != TARGET_SNAPSHOT_SCHEMA_VERSION:
            raise HistoricalBackfillError("target_snapshot_schema_mismatch")
        expected = normalized_json_digest(self.identity_payload())
        if self.target_snapshot_fingerprint != expected:
            raise HistoricalBackfillError("target_snapshot_fingerprint_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_fingerprint": self.schema_fingerprint,
            "logical_contract_digest": self.logical_contract_digest,
            "database_content_sha256": self.database_content_sha256,
            "counts": dict(self.counts),
            "source_distribution": dict(self.source_distribution),
            "snapshot_schema_version": self.snapshot_schema_version,
        }

    def public_summary(self) -> dict[str, Any]:
        return {
            "target_snapshot_fingerprint": self.target_snapshot_fingerprint,
            "schema_fingerprint": self.schema_fingerprint,
            "logical_contract_digest": self.logical_contract_digest,
            "database_content_sha256": self.database_content_sha256,
            "counts": dict(self.counts),
            "source_distribution": dict(self.source_distribution),
            "snapshot_schema_version": self.snapshot_schema_version,
        }


@dataclass(frozen=True)
class HistoricalAudit:
    target_snapshot: HistoricalTargetSnapshot
    evidence: tuple[HistoricalEvidence, ...]
    binding_candidates: tuple[HistoricalBindingCandidate, ...]
    attachment_binding_candidates: tuple[HistoricalAttachmentBindingCandidate, ...]
    conflicts: tuple[HistoricalBackfillConflict, ...]
    unknown_record_digests: tuple[str, ...]
    statistics: Mapping[str, int]
    evidence_digest: str
    reconstruction_basis_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence",
            tuple(sorted(self.evidence, key=lambda item: item.evidence_id)),
        )
        object.__setattr__(
            self,
            "binding_candidates",
            tuple(sorted(
                self.binding_candidates,
                key=lambda item: item.candidate_id,
            )),
        )
        object.__setattr__(
            self,
            "attachment_binding_candidates",
            tuple(sorted(
                self.attachment_binding_candidates,
                key=lambda item: item.candidate_id,
            )),
        )
        object.__setattr__(
            self,
            "conflicts",
            tuple(sorted(self.conflicts, key=lambda item: item.conflict_id)),
        )
        object.__setattr__(
            self,
            "unknown_record_digests",
            tuple(sorted(set(self.unknown_record_digests))),
        )
        object.__setattr__(
            self,
            "statistics",
            _frozen_counts(self.statistics, "invalid_audit_statistics"),
        )
        _sha(self.evidence_digest, "invalid_audit_evidence_digest")
        _sha(
            self.reconstruction_basis_digest,
            "invalid_reconstruction_basis_digest",
        )

    def public_summary(self) -> dict[str, Any]:
        return {
            "statistics": dict(self.statistics),
            "evidence_digest": self.evidence_digest,
            "reconstruction_basis_digest": self.reconstruction_basis_digest,
            "target_snapshot_fingerprint": (
                self.target_snapshot.target_snapshot_fingerprint
            ),
            "evidence_count": len(self.evidence),
            "binding_candidate_count": len(self.binding_candidates),
            "attachment_binding_candidate_count": len(
                self.attachment_binding_candidates
            ),
            "conflict_count": len(self.conflicts),
            "unknown_record_count": len(self.unknown_record_digests),
            "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        }


@dataclass(frozen=True)
class HistoricalBackfillPlan:
    backfill_plan_id: str
    manifest_subject_id: str
    unified_batch_id: str
    target_snapshot_fingerprint: str
    migration_version: int
    migration_digest: str
    evidence_policy_version: str
    reconstruction_basis_digest: str
    binding_candidates: tuple[HistoricalBindingCandidate, ...]
    attachment_binding_candidates: tuple[HistoricalAttachmentBindingCandidate, ...]
    conflicts: tuple[HistoricalBackfillConflict, ...]
    no_action_records: tuple[str, ...]
    unknown_records: tuple[str, ...]
    expected_binding_delta: int
    expected_attachment_binding_delta: int
    expected_conflict_delta: int
    verification_expectations: Mapping[str, Any]
    plan_digest: str
    plan_schema_version: str = PLAN_SCHEMA_VERSION

    approval_plan_kind = "historical_backfill"

    def __post_init__(self) -> None:
        _identity(self.backfill_plan_id, "backfill_plan_", "invalid_backfill_plan")
        _identity(
            self.manifest_subject_id,
            "manifest_subject_",
            "invalid_manifest_subject",
        )
        _identity(self.unified_batch_id, "batch_", "invalid_unified_batch")
        _sha(self.target_snapshot_fingerprint, "invalid_target_snapshot")
        if self.migration_version != 8302:
            raise HistoricalBackfillError("invalid_backfill_migration_version")
        _sha(self.migration_digest, "invalid_backfill_migration_digest")
        _token(self.evidence_policy_version, "invalid_evidence_policy")
        _sha(
            self.reconstruction_basis_digest,
            "invalid_reconstruction_basis",
        )
        candidates = tuple(sorted(
            self.binding_candidates,
            key=lambda item: item.candidate_id,
        ))
        attachments = tuple(sorted(
            self.attachment_binding_candidates,
            key=lambda item: item.candidate_id,
        ))
        conflicts = tuple(sorted(self.conflicts, key=lambda item: item.conflict_id))
        object.__setattr__(self, "binding_candidates", candidates)
        object.__setattr__(self, "attachment_binding_candidates", attachments)
        object.__setattr__(self, "conflicts", conflicts)
        object.__setattr__(
            self,
            "no_action_records",
            tuple(sorted(set(self.no_action_records))),
        )
        object.__setattr__(
            self,
            "unknown_records",
            tuple(sorted(set(self.unknown_records))),
        )
        for value in (
            self.expected_binding_delta,
            self.expected_attachment_binding_delta,
            self.expected_conflict_delta,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HistoricalBackfillError("invalid_expected_backfill_delta")
        if self.expected_binding_delta != sum(
            item.provenance_kind == "deterministic_legacy"
            for item in candidates
        ):
            raise HistoricalBackfillError("binding_delta_mismatch")
        if self.expected_attachment_binding_delta != len(attachments):
            raise HistoricalBackfillError("attachment_delta_mismatch")
        if self.expected_conflict_delta != len(conflicts):
            raise HistoricalBackfillError("conflict_delta_mismatch")
        if self.plan_schema_version != PLAN_SCHEMA_VERSION:
            raise HistoricalBackfillError("backfill_plan_schema_mismatch")
        _sha(self.plan_digest, "invalid_backfill_plan_digest")
        expected = make_plan_digest(self.identity_payload())
        if self.plan_digest != expected:
            raise HistoricalBackfillError("backfill_plan_digest_mismatch")
        if self.backfill_plan_id != "backfill_plan_" + expected:
            raise HistoricalBackfillError("backfill_plan_identity_mismatch")

    @property
    def plan_id(self) -> str:
        return self.backfill_plan_id

    @property
    def manifest_id(self) -> str:
        return self.manifest_subject_id

    @property
    def batch_id(self) -> str:
        return self.unified_batch_id

    def identity_payload(self) -> dict[str, Any]:
        return {
            "manifest_subject_id": self.manifest_subject_id,
            "unified_batch_id": self.unified_batch_id,
            "target_snapshot_fingerprint": self.target_snapshot_fingerprint,
            "migration_version": self.migration_version,
            "migration_digest": self.migration_digest,
            "evidence_policy_version": self.evidence_policy_version,
            "reconstruction_basis_digest": self.reconstruction_basis_digest,
            "binding_candidates": [
                item.summary() for item in self.binding_candidates
            ],
            "attachment_binding_candidates": [
                item.summary() for item in self.attachment_binding_candidates
            ],
            "conflicts": [item.summary() for item in self.conflicts],
            "no_action_records": list(self.no_action_records),
            "unknown_records": list(self.unknown_records),
            "expected_binding_delta": self.expected_binding_delta,
            "expected_attachment_binding_delta": (
                self.expected_attachment_binding_delta
            ),
            "expected_conflict_delta": self.expected_conflict_delta,
            "verification_expectations": dict(self.verification_expectations),
            "plan_schema_version": self.plan_schema_version,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "backfill_plan_id": self.backfill_plan_id,
            "plan_digest": self.plan_digest,
            "target_snapshot_fingerprint": self.target_snapshot_fingerprint,
            "manifest_subject_id_digest": normalized_json_digest({
                "manifest_subject_id": self.manifest_subject_id,
            }),
            "unified_batch_id_digest": normalized_json_digest({
                "unified_batch_id": self.unified_batch_id,
            }),
            "operation_counts": {
                "binding_candidates": len(self.binding_candidates),
                "attachment_binding_candidates": len(
                    self.attachment_binding_candidates
                ),
                "conflicts": len(self.conflicts),
                "unknown_records": len(self.unknown_records),
            },
            "expected_binding_delta": self.expected_binding_delta,
            "expected_attachment_binding_delta": (
                self.expected_attachment_binding_delta
            ),
            "expected_conflict_delta": self.expected_conflict_delta,
            "plan_schema_version": self.plan_schema_version,
        }


@dataclass(frozen=True)
class HistoricalBackfillResult:
    plan_id: str
    execution_id: str
    applied_bindings: int
    skipped_bindings: int
    deferred_bindings: int
    conflict_records: int
    unknown_records: int
    attachment_bindings: int
    verification_digest: str | None
    final_state: str
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    result_schema_version: str = RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.plan_id, "backfill_plan_", "invalid_result_plan")
        _identity(self.execution_id, "execution_", "invalid_result_execution")
        for value in (
            self.applied_bindings,
            self.skipped_bindings,
            self.deferred_bindings,
            self.conflict_records,
            self.unknown_records,
            self.attachment_bindings,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HistoricalBackfillError("invalid_result_count")
        if self.verification_digest is not None:
            _sha(self.verification_digest, "invalid_result_verification")
        if self.final_state not in {
            "completed",
            "completed_with_warnings",
            "failed",
            "recovery_required",
            "rolled_back",
        }:
            raise HistoricalBackfillError("invalid_result_state")
        for reason in (*self.warnings, *self.errors):
            _token(reason, "invalid_result_reason")
        if self.result_schema_version != RESULT_SCHEMA_VERSION:
            raise HistoricalBackfillError("result_schema_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "execution_id": self.execution_id,
            "applied_bindings": self.applied_bindings,
            "skipped_bindings": self.skipped_bindings,
            "deferred_bindings": self.deferred_bindings,
            "conflict_records": self.conflict_records,
            "unknown_records": self.unknown_records,
            "attachment_bindings": self.attachment_bindings,
            "verification_digest": self.verification_digest,
            "final_state": self.final_state,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "result_schema_version": self.result_schema_version,
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_distribution(connection: sqlite3.Connection) -> dict[str, int]:
    result: dict[str, int] = {}
    for kind, table in (("conversation", "conversations"), ("message", "messages")):
        for source, count in connection.execute(
            f"SELECT source_platform,COUNT(*) FROM {table} "
            "GROUP BY source_platform ORDER BY source_platform"
        ):
            label = str(source or "null").lower()
            result[f"{kind}_{label}"] = int(count)
    return result


def build_target_snapshot(db_path: str | Path) -> HistoricalTargetSnapshot:
    target = Path(db_path).resolve(strict=True)
    compatibility = capture_legacy_compatibility(target)
    uri = target.as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        counts = {
            "conversations": int(connection.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0]),
            "messages": int(connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]),
            "assets": int(connection.execute(
                "SELECT COUNT(*) FROM assets"
            ).fetchone()[0]),
            "fts": int(connection.execute(
                "SELECT COUNT(*) FROM messages_fts"
            ).fetchone()[0]),
        }
        distribution = _source_distribution(connection)
    logical_digest = normalized_json_digest({
        key: compatibility[key]
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
    payload = {
        "schema_fingerprint": compatibility["schema_fingerprint"],
        "logical_contract_digest": logical_digest,
        "database_content_sha256": _hash_file(target),
        "counts": counts,
        "source_distribution": distribution,
        "snapshot_schema_version": TARGET_SNAPSHOT_SCHEMA_VERSION,
    }
    return HistoricalTargetSnapshot(
        target_snapshot_fingerprint=normalized_json_digest(payload),
        compatibility=MappingProxyType(dict(compatibility)),
        **payload,
    )


def _record_evidence(
    *,
    kind: str,
    target_id: str,
    source: str | None,
    parent_source: str | None,
    relationship_valid: bool,
    legacy_report_available: bool,
) -> tuple[HistoricalEvidence, HistoricalBindingCandidate | None]:
    normalized_source = str(source or "").strip().lower() or None
    normalized_parent = str(parent_source or "").strip().lower() or None
    classification: str
    reason: str
    candidate_source: str | None
    source_id: str | None
    requires_approval = False
    conflict_state = "clear"
    if not relationship_valid:
        classification = "invalid_relationship"
        reason = "parent_relationship_invalid"
        candidate_source = normalized_source
        source_id = None
        conflict_state = "blocked"
    elif normalized_source and normalized_source not in SUPPORTED_SOURCES:
        classification = "unsupported_source"
        reason = "source_platform_unsupported"
        candidate_source = normalized_source
        source_id = None
        conflict_state = "blocked"
    elif (
        normalized_source
        and normalized_parent
        and normalized_source != normalized_parent
    ):
        classification = "conflicting_evidence"
        reason = "parent_source_conflict"
        candidate_source = normalized_source
        source_id = target_id
        requires_approval = True
        conflict_state = "blocked"
    elif normalized_source:
        classification = "native_authoritative"
        reason = "native_source_identity_valid"
        candidate_source = normalized_source
        source_id = target_id
    elif normalized_parent:
        classification = "deterministic_legacy"
        reason = "parent_source_identity_deterministic"
        candidate_source = normalized_parent
        source_id = target_id
        requires_approval = True
    elif legacy_report_available:
        classification = "deterministic_legacy"
        reason = "initial_import_report_deterministic"
        candidate_source = "chatgpt"
        source_id = target_id
        requires_approval = True
    else:
        classification = "legacy_unknown"
        reason = "source_evidence_unavailable"
        candidate_source = None
        source_id = None
    target_digest = _digest_ref(kind, target_id)
    source_digest = (
        _source_identity_digest(kind, candidate_source, source_id)
        if candidate_source is not None and source_id is not None else None
    )
    evidence = make_historical_evidence(
        evidence_type=(
            "native_record_identity"
            if classification == "native_authoritative"
            else "historical_record_reconstruction"
        ),
        target_record_kind=kind,
        target_record_id_digest=target_digest,
        source_type_candidate=candidate_source,
        source_record_id_digest=source_digest,
        evidence_strength=classification,
        reason_code=reason,
    )
    if candidate_source is None:
        return evidence, None
    candidate = make_binding_candidate(
        target_record_kind=kind,
        target_record_ref=target_id,
        proposed_source_type=candidate_source,
        proposed_source_record_id=source_id,
        provenance_kind=classification,
        evidence_ids=(evidence.evidence_id,),
        requires_approval=requires_approval,
        conflict_state=conflict_state,
    )
    return evidence, candidate


def _attachment_state(record: MediaIndexRecord) -> tuple[str, str]:
    if (
        record.asset_catalog_size is not None
        and record.declared_size is not None
        and record.asset_catalog_size != record.declared_size
    ):
        return "unresolved", "size_conflict_unresolved"
    if record.entity_state is EntityState.ASSET_RECORD_MISSING:
        return "asset_record_missing", "clear"
    if record.entity_state in {
        EntityState.ENTITY_PATH_MISSING,
        EntityState.ENTITY_MISSING,
    }:
        return "entity_missing", "clear"
    if record.entity_state in {
        EntityState.INVALID_RELATIONSHIP,
        EntityState.ENTITY_IDENTITY_CONFLICT,
        EntityState.UNSUPPORTED_SOURCE,
        EntityState.EXTERNAL_URL_BLOCKED,
    }:
        return "unresolved", "clear"
    return "metadata_only", "clear"


def audit_historical_evidence(
    db_path: str | Path,
    *,
    evidence_source_digests: Mapping[str, str],
    legacy_report_available: bool = True,
) -> HistoricalAudit:
    """Audit real records without returning titles, bodies, paths, or URLs."""

    target = Path(db_path).resolve(strict=True)
    for key, value in evidence_source_digests.items():
        _token(key, "invalid_evidence_source_label")
        _sha(value, "invalid_evidence_source_digest")
    snapshot = build_target_snapshot(target)
    uri = target.as_uri() + "?mode=ro&immutable=1"
    evidence: list[HistoricalEvidence] = []
    candidates: list[HistoricalBindingCandidate] = []
    unknown: list[str] = []
    conflicts: list[HistoricalBackfillConflict] = []
    classifications: Counter[str] = Counter()
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        conversations = {
            str(row["conversation_id"]): (
                str(row["source_platform"]).lower()
                if row["source_platform"] not in (None, "") else None
            )
            for row in connection.execute(
                "SELECT conversation_id,source_platform FROM conversations"
            )
        }
        for conversation_id, source in sorted(conversations.items()):
            item, candidate = _record_evidence(
                kind="conversation",
                target_id=conversation_id,
                source=source,
                parent_source=None,
                relationship_valid=True,
                legacy_report_available=legacy_report_available,
            )
            evidence.append(item)
            classifications[item.evidence_strength] += 1
            if candidate is None:
                unknown.append(item.target_record_id_digest)
            else:
                candidates.append(candidate)
        for row in connection.execute(
            "SELECT message_id,conversation_id,source_platform FROM messages "
            "ORDER BY rowid"
        ):
            message_id = str(row["message_id"])
            conversation_id = str(row["conversation_id"])
            source = (
                str(row["source_platform"]).lower()
                if row["source_platform"] not in (None, "") else None
            )
            relationship_valid = conversation_id in conversations
            item, candidate = _record_evidence(
                kind="message",
                target_id=message_id,
                source=source,
                parent_source=conversations.get(conversation_id),
                relationship_valid=relationship_valid,
                legacy_report_available=legacy_report_available,
            )
            evidence.append(item)
            classifications[item.evidence_strength] += 1
            if candidate is None:
                unknown.append(item.target_record_id_digest)
            else:
                candidates.append(candidate)
                if candidate.conflict_state == "blocked":
                    conflicts.append(make_backfill_conflict(
                        conflict_type=item.evidence_strength,
                        severity="high",
                        target_refs=(message_id,),
                        candidate_refs=(candidate.candidate_id,),
                        evidence_digests=(item.evidence_digest,),
                        proposed_actions=("block_binding", "manual_review"),
                        default_action="block_binding",
                        requires_approval=True,
                        reason_code=item.reason_code,
                    ))

    media = build_media_index(
        target,
        inspect_entities=True,
        compute_hash=False,
    )
    attachment_candidates: list[HistoricalAttachmentBindingCandidate] = []
    media_states = Counter(record.entity_state.value for record in media.records)
    for record in media.records:
        target_digest = normalized_json_digest({
            "logical_media_id": record.media_id,
            "message_id_digest": _digest_ref("message", record.message_id),
            "occurrence_index": record.attachment_index,
        })
        relationship_valid = (
            bool(record.media_id)
            and record.entity_state is not EntityState.INVALID_RELATIONSHIP
        )
        strength = (
            "deterministic_legacy"
            if relationship_valid else "invalid_relationship"
        )
        reason = (
            "media_identity_deterministic"
            if relationship_valid else "media_relationship_invalid"
        )
        item = make_historical_evidence(
            evidence_type="media_identity",
            target_record_kind="attachment",
            target_record_id_digest=target_digest,
            source_type_candidate=None,
            source_record_id_digest=(
                normalized_json_digest({
                    "source_attachment_id": record.source_attachment_id,
                    "media_identity_policy_version": MEDIA_IDENTITY_POLICY_VERSION,
                })
                if record.source_attachment_id else None
            ),
            evidence_strength=strength,
            reason_code=reason,
        )
        evidence.append(item)
        classifications[strength] += 1
        if not relationship_valid:
            unknown.append(target_digest)
            continue
        attachment_state, size_state = _attachment_state(record)
        attachment_candidates.append(make_attachment_candidate(
            logical_media_id=record.media_id,
            message_id=record.message_id,
            source_attachment_id=record.source_attachment_id or None,
            occurrence_index=record.attachment_index,
            attachment_state=attachment_state,
            size_conflict_state=size_state,
            provenance_kind="deterministic_legacy",
            evidence_id=item.evidence_id,
            evidence_digest=item.evidence_digest,
        ))

    by_source: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for candidate in candidates:
        if candidate.source_record_id_digest is None:
            continue
        by_source[(
            candidate.target_record_kind,
            candidate.proposed_source_type,
            candidate.source_record_id_digest,
        )].add(_digest_ref(
            candidate.target_record_kind,
            candidate.target_record_ref,
        ))
    duplicate_identities = sum(len(values) > 1 for values in by_source.values())
    evidence_digest = normalized_json_digest({
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "evidence": [item.to_dict() for item in sorted(
            evidence, key=lambda value: value.evidence_id
        )],
    })
    reconstruction_basis = normalized_json_digest({
        "target_snapshot_fingerprint": snapshot.target_snapshot_fingerprint,
        "evidence_sources": dict(sorted(evidence_source_digests.items())),
        "adapter_identity_policy_version": ADAPTER_IDENTITY_POLICY_VERSION,
        "media_identity_policy_version": MEDIA_IDENTITY_POLICY_VERSION,
    })
    conversation_total = snapshot.counts["conversations"]
    message_total = snapshot.counts["messages"]
    attachment_total = len(media.records)
    logical_media = len({
        record.media_id for record in media.records if record.media_id
    })
    statistics = {
        "records_total": conversation_total + message_total + attachment_total,
        "conversations_total": conversation_total,
        "messages_total": message_total,
        "attachments_total": attachment_total,
        "media_occurrences": attachment_total,
        "logical_media_identities": logical_media,
        "entity_available": media_states[EntityState.ENTITY_AVAILABLE.value],
        "native_authoritative": classifications["native_authoritative"],
        "deterministic_legacy": classifications["deterministic_legacy"],
        "probable_legacy": classifications["probable_legacy"],
        "legacy_unknown": classifications["legacy_unknown"],
        "conflicting_evidence": classifications["conflicting_evidence"],
        "unsupported_source": classifications["unsupported_source"],
        "invalid_relationship": classifications["invalid_relationship"],
        "conversation_binding_candidates": sum(
            item.target_record_kind == "conversation" for item in candidates
        ),
        "message_binding_candidates": sum(
            item.target_record_kind == "message" for item in candidates
        ),
        "attachment_binding_candidates": len(attachment_candidates),
        "media_identity_verifications": len(attachment_candidates),
        "source_identity_duplicates": duplicate_identities,
        "source_identity_content_conflicts": 0,
        "legacy_empty_source": sum(
            count for key, count in snapshot.source_distribution.items()
            if key.endswith("_null")
        ),
        "multiple_source_candidates": classifications["probable_legacy"],
        "deterministic_backfill_records": sum(
            item.provenance_kind == "deterministic_legacy"
            for item in candidates
        ),
        "conflicts": len(conflicts),
        "unknown_records": len(unknown),
        "size_conflict_unresolved": sum(
            item.size_conflict_state == "size_conflict_unresolved"
            for item in attachment_candidates
        ),
    }
    return HistoricalAudit(
        target_snapshot=snapshot,
        evidence=tuple(evidence),
        binding_candidates=tuple(candidates),
        attachment_binding_candidates=tuple(attachment_candidates),
        conflicts=tuple(conflicts),
        unknown_record_digests=tuple(unknown),
        statistics=statistics,
        evidence_digest=evidence_digest,
        reconstruction_basis_digest=reconstruction_basis,
    )


def build_historical_manifest_and_subject(
    audit: HistoricalAudit,
    *,
    created_at: str,
) -> tuple[HistoricalSourceManifest, ManifestSubject]:
    _timestamp(created_at, "invalid_manifest_created_at")
    manifest = build_historical_source_manifest(
        source_type="legacy_reconstruction",
        provenance_kind="deterministic_legacy",
        archive_state="unavailable",
        archive_sha256=None,
        archive_size=None,
        adapter_name=None,
        adapter_version=None,
        evidence_policy_version=EVIDENCE_POLICY_VERSION,
        evidence_digest=audit.evidence_digest,
        reconstruction_basis_digest=audit.reconstruction_basis_digest,
        validation_status="evidence_reconstructed",
        record_counts={
            key: audit.statistics[key]
            for key in (
                "conversations_total",
                "messages_total",
                "native_authoritative",
                "deterministic_legacy",
                "probable_legacy",
                "legacy_unknown",
                "conflicting_evidence",
                "unsupported_source",
                "invalid_relationship",
            )
        },
        asset_counts={
            key: audit.statistics[key]
            for key in (
                "attachments_total",
                "media_occurrences",
                "logical_media_identities",
                "entity_available",
                "size_conflict_unresolved",
            )
        },
        warnings=("original_archive_unavailable",),
        created_at=created_at,
    )
    subject = build_manifest_subject(
        historical_manifest=manifest,
        created_at=created_at,
    )
    return manifest, subject


def build_historical_backfill_plan(
    audit: HistoricalAudit,
    subject: ManifestSubject,
    *,
    migration_version: int,
    migration_digest: str,
) -> HistoricalBackfillPlan:
    if subject.manifest_kind != "historical_reconstruction":
        raise HistoricalBackfillError("historical_subject_required")
    _sha(migration_digest, "invalid_migration_digest")
    batch_seed = normalized_json_digest({
        "manifest_subject_id": subject.manifest_subject_id,
        "target_snapshot_fingerprint": (
            audit.target_snapshot.target_snapshot_fingerprint
        ),
        "reconstruction_basis_digest": audit.reconstruction_basis_digest,
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "batch_mode": "historical_backfill",
    })
    batch_id = "batch_" + batch_seed[:32]
    expected_bindings = sum(
        item.provenance_kind == "deterministic_legacy"
        for item in audit.binding_candidates
    )
    verification = {
        "zero_old_table_updates": True,
        "legacy_logical_contract_digest": (
            audit.target_snapshot.logical_contract_digest
        ),
        "expected_binding_delta": expected_bindings,
        "expected_attachment_binding_delta": len(
            audit.attachment_binding_candidates
        ),
        "expected_conflict_delta": len(audit.conflicts),
        "entity_available": audit.statistics["entity_available"],
        "size_conflict_unresolved": audit.statistics[
            "size_conflict_unresolved"
        ],
    }
    payload = {
        "manifest_subject_id": subject.manifest_subject_id,
        "unified_batch_id": batch_id,
        "target_snapshot_fingerprint": (
            audit.target_snapshot.target_snapshot_fingerprint
        ),
        "migration_version": migration_version,
        "migration_digest": migration_digest,
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "reconstruction_basis_digest": audit.reconstruction_basis_digest,
        "binding_candidates": [
            item.summary() for item in sorted(
                audit.binding_candidates,
                key=lambda value: value.candidate_id,
            )
        ],
        "attachment_binding_candidates": [
            item.summary() for item in sorted(
                audit.attachment_binding_candidates,
                key=lambda value: value.candidate_id,
            )
        ],
        "conflicts": [
            item.summary() for item in sorted(
                audit.conflicts,
                key=lambda value: value.conflict_id,
            )
        ],
        "no_action_records": [],
        "unknown_records": list(audit.unknown_record_digests),
        "expected_binding_delta": expected_bindings,
        "expected_attachment_binding_delta": len(
            audit.attachment_binding_candidates
        ),
        "expected_conflict_delta": len(audit.conflicts),
        "verification_expectations": verification,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
    }
    digest = make_plan_digest(payload)
    return HistoricalBackfillPlan(
        backfill_plan_id="backfill_plan_" + digest,
        plan_digest=digest,
        binding_candidates=audit.binding_candidates,
        attachment_binding_candidates=audit.attachment_binding_candidates,
        conflicts=audit.conflicts,
        no_action_records=(),
        unknown_records=audit.unknown_record_digests,
        **{
            key: value for key, value in payload.items()
            if key not in {
                "binding_candidates",
                "attachment_binding_candidates",
                "conflicts",
                "no_action_records",
                "unknown_records",
                "plan_schema_version",
            }
        },
    )


def aggregate_evidence_source_digests(
    sources: Mapping[str, str | Path],
) -> Mapping[str, str]:
    """Hash allowed reports/manifests without returning local paths."""

    result: dict[str, str] = {}
    for label, raw_path in sources.items():
        _token(label, "invalid_evidence_source_label")
        path = Path(raw_path).resolve(strict=True)
        if not path.is_file():
            raise HistoricalBackfillError("evidence_source_unavailable")
        result[label] = _hash_file(path)
    return MappingProxyType(dict(sorted(result.items())))


def operation_type_counts(
    plan: HistoricalBackfillPlan,
) -> Mapping[str, int]:
    counts = Counter()
    for candidate in plan.binding_candidates:
        counts[{
            "native_authoritative": "verify_native_source_binding",
            "deterministic_legacy": "create_legacy_source_binding",
            "probable_legacy": "flag_probable_binding",
            "conflicting_evidence": "record_backfill_conflict",
            "unsupported_source": "record_backfill_conflict",
            "invalid_relationship": "record_backfill_conflict",
        }.get(candidate.provenance_kind, "record_legacy_unknown")] += 1
    counts["create_historical_attachment_binding"] += len(
        plan.attachment_binding_candidates
    )
    counts["verify_media_identity"] += len(
        plan.attachment_binding_candidates
    )
    counts["record_backfill_conflict"] += len(plan.conflicts)
    for operation_type in counts:
        if operation_type not in EXECUTABLE_OPERATION_TYPES:
            raise HistoricalBackfillError("unknown_operation_type")
    return MappingProxyType(dict(sorted(counts.items())))
