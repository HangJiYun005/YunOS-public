"""YunOS v8.3-B normalized records and deterministic planning engine.

Normalized records are private staging objects: they may contain source IDs,
titles, text, and filenames required for a faithful future adapter.  Their
``plan_descriptor`` methods are the only representation accepted by
``ImportPlan`` and deliberately contain hashes, opaque identities, counts, and
reason codes only.  This module performs no filesystem, database, archive, or
network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import PurePosixPath
import re
import unicodedata
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from memory.ingestion_core import (
    AssetStageFingerprint,
    DatabaseFingerprint,
    ImportBatch,
    SourceManifest,
    canonical_json_bytes,
    normalized_json_digest,
    sanitize_reason_code,
)


NORMALIZATION_PROTOCOL_VERSION = "v8.3-b-normalized-records-1"
PLANNING_POLICY_VERSION = "v8.3-b-deterministic-planning-1"
PLAN_SCHEMA_VERSION = "v8.3-b-import-plan-1"
CONFLICT_SCHEMA_VERSION = "v8.3-b-import-conflict-1"
DECISION_SCHEMA_VERSION = "v8.3-b-import-decision-1"
VERIFICATION_SCHEMA_VERSION = "v8.3-b-import-verification-1"
RECORD_LOCATOR_VERSION = "v8.3-b-record-locator-1"

MAX_TEXT_CHARS = 10_000_000
MAX_TITLE_CHARS = 100_000
MAX_METADATA_BYTES = 1_000_000
MAX_RECORDS_PER_PLAN = 1_000_000

_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_:-]{7,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MIME_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z")

RECORD_KINDS = frozenset({"conversation", "message", "attachment", "asset"})
CONFLICT_STATES = frozenset({"clear", "pending", "resolved", "blocked"})
ENTITY_STATES = frozenset({
    "entity_available",
    "metadata_only",
    "entity_missing",
    "entity_blocked",
    "entity_invalid",
})
INTEGRITY_STATES = frozenset({"verified", "unverified", "quarantined", "corrupt"})
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
CONFLICT_STATUSES = frozenset({"pending", "resolved", "dismissed"})
DECISIONS = frozenset({
    "insert",
    "append",
    "no_change",
    "keep_existing",
    "accept_incoming",
    "preserve_both",
    "manual_review",
    "tombstone_candidate",
    "share_entity",
    "metadata_only",
    "reject",
    "link_candidate",
})
ACTION_TYPES = frozenset({
    "insert",
    "append",
    "no_change",
    "conflict",
    "tombstone_candidate",
    "share_entity",
    "metadata_only",
    "reject",
    "link_candidate",
})


class IngestionPlanningError(ValueError):
    """Fail-closed normalized-record/planning validation error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _token(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise IngestionPlanningError(reason_code)
    return value


def _identity(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise IngestionPlanningError(reason_code)
    return value


def _sha256(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IngestionPlanningError(reason_code)
    return value


def _timestamp(value: str | None, reason_code: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IngestionPlanningError(reason_code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise IngestionPlanningError(reason_code) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise IngestionPlanningError(reason_code)
    return value


def _strict_text(value: Any, reason_code: str, *, max_chars: int) -> str:
    if not isinstance(value, str) or len(value) > max_chars or "\x00" in value:
        raise IngestionPlanningError(reason_code)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise IngestionPlanningError(reason_code) from None
    return unicodedata.normalize("NFC", value)


def _optional_private_id(value: Any, reason_code: str) -> str | None:
    if value is None:
        return None
    result = _strict_text(value, reason_code, max_chars=4096)
    if not result:
        raise IngestionPlanningError(reason_code)
    return result


def _private_id(value: Any, reason_code: str) -> str:
    result = _optional_private_id(value, reason_code)
    if result is None:
        raise IngestionPlanningError(reason_code)
    return result


def _warnings(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > 256:
        raise IngestionPlanningError("invalid_warning_codes")
    return tuple(sanitize_reason_code(value) for value in values)


def _missing_reasons(values: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(values, Mapping) or len(values) > 64:
        raise IngestionPlanningError("invalid_missing_reasons")
    result: dict[str, str] = {}
    for field_name, reason in values.items():
        key = _token(field_name, "invalid_missing_field")
        result[key] = sanitize_reason_code(reason)
    return MappingProxyType(dict(sorted(result.items())))


def _normalized_mapping(value: Mapping[str, Any], reason_code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IngestionPlanningError(reason_code)
    try:
        encoded = canonical_json_bytes(value)
    except ValueError:
        raise IngestionPlanningError(reason_code) from None
    if len(encoded) > MAX_METADATA_BYTES:
        raise IngestionPlanningError("metadata_size_limit_exceeded")
    normalized = json.loads(encoded.decode("utf-8"))
    return _freeze_json(normalized)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({
            key: _freeze_json(item) for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _count(value: Any, reason_code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IngestionPlanningError(reason_code)
    return value


def _finite_number(value: Any, reason_code: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IngestionPlanningError(reason_code)
    if isinstance(value, float) and not math.isfinite(value):
        raise IngestionPlanningError(reason_code)
    return value


def _mime(value: str | None, reason_code: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if _MIME_RE.fullmatch(normalized) is None:
        raise IngestionPlanningError(reason_code)
    return normalized


def _relative_path(value: str) -> str:
    text = _strict_text(value, "invalid_relative_entity_path", max_chars=1024).replace("\\", "/")
    path = PurePosixPath(text)
    if (
        not text
        or text.startswith("/")
        or ":" in text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise IngestionPlanningError("invalid_relative_entity_path")
    return path.as_posix()


def make_source_identity(record_kind: str, source_type: str, source_record_id: str) -> str:
    kind = _token(record_kind, "invalid_record_kind")
    if kind not in RECORD_KINDS:
        raise IngestionPlanningError("invalid_record_kind")
    source = _token(source_type, "invalid_source_type")
    raw_id = _private_id(source_record_id, "invalid_source_record_id")
    return f"source_{kind}_" + normalized_json_digest({
        "identity_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "record_kind": kind,
        "source_type": source,
        "source_record_id": raw_id,
    })


def _field_hash(value: Any) -> str:
    return normalized_json_digest({
        "field_hash_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "value": value,
    })


@dataclass(frozen=True)
class RecordLocator:
    locator_kind: str
    ordinal: int
    locator_fingerprint: str
    locator_version: str = RECORD_LOCATOR_VERSION

    def __post_init__(self) -> None:
        _token(self.locator_kind, "invalid_locator_kind")
        _count(self.ordinal, "invalid_locator_ordinal")
        _sha256(self.locator_fingerprint, "invalid_locator_fingerprint")
        if self.locator_version != RECORD_LOCATOR_VERSION:
            raise IngestionPlanningError("invalid_locator_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator_kind": self.locator_kind,
            "ordinal": self.ordinal,
            "locator_fingerprint": self.locator_fingerprint,
            "locator_version": self.locator_version,
        }


def make_record_locator(
    locator_kind: str,
    ordinal: int,
    *,
    private_locator_material: Any,
) -> RecordLocator:
    return RecordLocator(
        locator_kind=locator_kind,
        ordinal=ordinal,
        locator_fingerprint=normalized_json_digest({
            "locator_version": RECORD_LOCATOR_VERSION,
            "locator_kind": locator_kind,
            "ordinal": ordinal,
            "private_locator_material": private_locator_material,
        }),
    )


@dataclass(frozen=True)
class RecordProvenance:
    source_type: str
    manifest_id: str
    source_record_id: str = field(repr=False)
    batch_id: str
    adapter_version: str
    parsed_at: str
    locator: RecordLocator
    record_kind: str

    def __post_init__(self) -> None:
        _token(self.source_type, "invalid_source_type")
        _identity(self.manifest_id, "invalid_manifest_id")
        _private_id(self.source_record_id, "invalid_source_record_id")
        _identity(self.batch_id, "invalid_batch_id")
        _token(self.adapter_version, "invalid_adapter_version")
        _timestamp(self.parsed_at, "invalid_parsed_at")
        if self.record_kind not in RECORD_KINDS:
            raise IngestionPlanningError("invalid_record_kind")

    @property
    def source_identity(self) -> str:
        return make_source_identity(
            self.record_kind, self.source_type, self.source_record_id,
        )

    def plan_descriptor(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "manifest_id": self.manifest_id,
            "source_identity": self.source_identity,
            "batch_id": self.batch_id,
            "adapter_version": self.adapter_version,
            "locator": self.locator.to_dict(),
            "record_kind": self.record_kind,
        }


def make_provenance(
    *,
    record_kind: str,
    source_type: str,
    manifest_id: str,
    source_record_id: str,
    batch_id: str,
    adapter_version: str,
    parsed_at: str,
    locator: RecordLocator,
) -> RecordProvenance:
    return RecordProvenance(
        source_type=source_type,
        manifest_id=manifest_id,
        source_record_id=source_record_id,
        batch_id=batch_id,
        adapter_version=adapter_version,
        parsed_at=parsed_at,
        locator=locator,
        record_kind=record_kind,
    )


@dataclass(frozen=True)
class NormalizedConversation:
    record_id: str
    provenance: RecordProvenance
    title: str = field(repr=False)
    created_at: str | None
    updated_at: str | None
    source_status: Mapping[str, Any] = field(repr=False)
    content_hash: str
    message_identity_digest: str
    warning_codes: tuple[str, ...]
    conflict_state: str
    production_conversation_id: str | None = field(default=None, repr=False)
    missing_reasons: Mapping[str, str] = field(default_factory=dict, repr=False)
    normalization_protocol: str = NORMALIZATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _identity(self.record_id, "invalid_conversation_record_id")
        if self.provenance.record_kind != "conversation":
            raise IngestionPlanningError("conversation_provenance_kind_mismatch")
        _strict_text(self.title, "invalid_conversation_title", max_chars=MAX_TITLE_CHARS)
        _timestamp(self.created_at, "invalid_conversation_created_at", allow_none=True)
        _timestamp(self.updated_at, "invalid_conversation_updated_at", allow_none=True)
        object.__setattr__(self, "source_status", _normalized_mapping(
            self.source_status, "invalid_conversation_status",
        ))
        _sha256(self.content_hash, "invalid_conversation_content_hash")
        _sha256(self.message_identity_digest, "invalid_message_identity_digest")
        object.__setattr__(self, "warning_codes", _warnings(self.warning_codes))
        if self.conflict_state not in CONFLICT_STATES:
            raise IngestionPlanningError("invalid_conflict_state")
        object.__setattr__(self, "production_conversation_id", _optional_private_id(
            self.production_conversation_id, "invalid_production_conversation_id",
        ))
        object.__setattr__(self, "missing_reasons", _missing_reasons(self.missing_reasons))
        if self.created_at is None and "created_at" not in self.missing_reasons:
            raise IngestionPlanningError("missing_created_at_reason")
        if self.updated_at is None and "updated_at" not in self.missing_reasons:
            raise IngestionPlanningError("missing_updated_at_reason")
        if self.normalization_protocol != NORMALIZATION_PROTOCOL_VERSION:
            raise IngestionPlanningError("normalization_protocol_mismatch")

    @property
    def source_identity(self) -> str:
        return self.provenance.source_identity

    def plan_descriptor(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_kind": "conversation",
            "provenance": self.provenance.plan_descriptor(),
            "source_identity": self.source_identity,
            "content_hash": self.content_hash,
            "structure_hash": self.message_identity_digest,
            "field_hashes": {
                "title": _field_hash(self.title),
                "created_at": _field_hash(self.created_at),
                "updated_at": _field_hash(self.updated_at),
                "source_status": _field_hash(dict(self.source_status)),
            },
            "warning_codes": list(self.warning_codes),
            "conflict_state": self.conflict_state,
            "production_identity_digest": (
                _field_hash(self.production_conversation_id)
                if self.production_conversation_id else None
            ),
            "missing_reasons": dict(self.missing_reasons),
            "normalization_protocol": self.normalization_protocol,
        }


def normalize_conversation(
    provenance: RecordProvenance,
    *,
    title: str,
    created_at: str | None,
    updated_at: str | None,
    source_status: Mapping[str, Any],
    message_source_ids: Sequence[str],
    warning_codes: Sequence[str] = (),
    conflict_state: str = "clear",
    production_conversation_id: str | None = None,
    missing_reasons: Mapping[str, str] | None = None,
) -> NormalizedConversation:
    normalized_title = _strict_text(title, "invalid_conversation_title", max_chars=MAX_TITLE_CHARS)
    normalized_status = _normalized_mapping(source_status, "invalid_conversation_status")
    message_identities = [
        make_source_identity("message", provenance.source_type, source_id)
        for source_id in message_source_ids
    ]
    content_hash = normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "title": normalized_title,
        "created_at": created_at,
        "updated_at": updated_at,
        "source_status": dict(normalized_status),
    })
    structure_hash = normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "message_identities": message_identities,
    })
    record_id = "nconv_" + normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "source_identity": provenance.source_identity,
    })
    return NormalizedConversation(
        record_id=record_id,
        provenance=provenance,
        title=normalized_title,
        created_at=created_at,
        updated_at=updated_at,
        source_status=normalized_status,
        content_hash=content_hash,
        message_identity_digest=structure_hash,
        warning_codes=tuple(warning_codes),
        conflict_state=conflict_state,
        production_conversation_id=production_conversation_id,
        missing_reasons=missing_reasons or {},
    )


@dataclass(frozen=True)
class NormalizedMessage:
    record_id: str
    provenance: RecordProvenance
    source_conversation_id: str = field(repr=False)
    source_node_id: str | None = field(repr=False)
    role: str
    author: str = field(repr=False)
    content_type: str
    text: str = field(repr=False)
    status: str
    weight: int | float | None
    created_at: str | None
    updated_at: str | None
    parent_source_message_id: str | None = field(repr=False)
    child_source_message_ids: tuple[str, ...] = field(repr=False)
    normalized_metadata: Mapping[str, Any] = field(repr=False)
    content_hash: str
    structure_hash: str
    warning_codes: tuple[str, ...]
    conflict_state: str
    production_message_id: str | None = field(default=None, repr=False)
    missing_reasons: Mapping[str, str] = field(default_factory=dict, repr=False)
    normalization_protocol: str = NORMALIZATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _identity(self.record_id, "invalid_message_record_id")
        if self.provenance.record_kind != "message":
            raise IngestionPlanningError("message_provenance_kind_mismatch")
        _private_id(self.source_conversation_id, "invalid_source_conversation_id")
        object.__setattr__(self, "source_node_id", _optional_private_id(
            self.source_node_id, "invalid_source_node_id",
        ))
        _token(self.role, "invalid_message_role")
        _strict_text(self.author, "invalid_message_author", max_chars=4096)
        _token(self.content_type, "invalid_content_type")
        _strict_text(self.text, "invalid_message_text", max_chars=MAX_TEXT_CHARS)
        _token(self.status, "invalid_message_status")
        _finite_number(self.weight, "invalid_message_weight")
        _timestamp(self.created_at, "invalid_message_created_at", allow_none=True)
        _timestamp(self.updated_at, "invalid_message_updated_at", allow_none=True)
        object.__setattr__(self, "parent_source_message_id", _optional_private_id(
            self.parent_source_message_id, "invalid_parent_message_id",
        ))
        children = tuple(
            _private_id(value, "invalid_child_message_id")
            for value in self.child_source_message_ids
        )
        object.__setattr__(self, "child_source_message_ids", children)
        object.__setattr__(self, "normalized_metadata", _normalized_mapping(
            self.normalized_metadata, "invalid_message_metadata",
        ))
        _sha256(self.content_hash, "invalid_message_content_hash")
        _sha256(self.structure_hash, "invalid_message_structure_hash")
        object.__setattr__(self, "warning_codes", _warnings(self.warning_codes))
        if self.conflict_state not in CONFLICT_STATES:
            raise IngestionPlanningError("invalid_conflict_state")
        object.__setattr__(self, "production_message_id", _optional_private_id(
            self.production_message_id, "invalid_production_message_id",
        ))
        object.__setattr__(self, "missing_reasons", _missing_reasons(self.missing_reasons))
        for optional_field, optional_value in (
            ("source_node_id", self.source_node_id),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("parent_source_message_id", self.parent_source_message_id),
        ):
            if optional_value is None and optional_field not in self.missing_reasons:
                raise IngestionPlanningError(f"missing_{optional_field}_reason")
        if self.normalization_protocol != NORMALIZATION_PROTOCOL_VERSION:
            raise IngestionPlanningError("normalization_protocol_mismatch")

    @property
    def source_identity(self) -> str:
        return self.provenance.source_identity

    @property
    def source_conversation_identity(self) -> str:
        return make_source_identity(
            "conversation", self.provenance.source_type, self.source_conversation_id,
        )

    @property
    def parent_source_identity(self) -> str | None:
        if self.parent_source_message_id is None:
            return None
        return make_source_identity(
            "message", self.provenance.source_type, self.parent_source_message_id,
        )

    def plan_descriptor(self) -> dict[str, Any]:
        child_identities = [
            make_source_identity("message", self.provenance.source_type, value)
            for value in self.child_source_message_ids
        ]
        return {
            "record_id": self.record_id,
            "record_kind": "message",
            "provenance": self.provenance.plan_descriptor(),
            "source_identity": self.source_identity,
            "source_conversation_identity": self.source_conversation_identity,
            "source_node_identity": (
                _field_hash(self.source_node_id) if self.source_node_id else None
            ),
            "parent_source_identity": self.parent_source_identity,
            "child_source_identities": child_identities,
            "content_hash": self.content_hash,
            "structure_hash": self.structure_hash,
            "field_hashes": {
                "role": _field_hash(self.role),
                "author": _field_hash(self.author),
                "content_type": _field_hash(self.content_type),
                "text": _field_hash(self.text),
                "status": _field_hash(self.status),
                "weight": _field_hash(self.weight),
                "created_at": _field_hash(self.created_at),
                "updated_at": _field_hash(self.updated_at),
                "metadata": _field_hash(dict(self.normalized_metadata)),
            },
            "occurred_at": self.created_at,
            "warning_codes": list(self.warning_codes),
            "conflict_state": self.conflict_state,
            "production_identity_digest": (
                _field_hash(self.production_message_id)
                if self.production_message_id else None
            ),
            "missing_reasons": dict(self.missing_reasons),
            "normalization_protocol": self.normalization_protocol,
        }


def normalize_message(
    provenance: RecordProvenance,
    *,
    source_conversation_id: str,
    source_node_id: str | None,
    role: str,
    author: str,
    content_type: str,
    text: str,
    status: str,
    weight: int | float | None,
    created_at: str | None,
    updated_at: str | None,
    parent_source_message_id: str | None,
    child_source_message_ids: Sequence[str],
    metadata: Mapping[str, Any],
    warning_codes: Sequence[str] = (),
    conflict_state: str = "clear",
    production_message_id: str | None = None,
    missing_reasons: Mapping[str, str] | None = None,
) -> NormalizedMessage:
    normalized_author = _strict_text(author, "invalid_message_author", max_chars=4096)
    normalized_text = _strict_text(text, "invalid_message_text", max_chars=MAX_TEXT_CHARS)
    normalized_metadata = _normalized_mapping(metadata, "invalid_message_metadata")
    _finite_number(weight, "invalid_message_weight")
    source_conversation_identity = make_source_identity(
        "conversation", provenance.source_type, source_conversation_id,
    )
    node_identity = _field_hash(source_node_id) if source_node_id else None
    parent_identity = (
        make_source_identity("message", provenance.source_type, parent_source_message_id)
        if parent_source_message_id else None
    )
    child_identities = [
        make_source_identity("message", provenance.source_type, value)
        for value in child_source_message_ids
    ]
    content_hash = normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "role": role,
        "author": normalized_author,
        "content_type": content_type,
        "text": normalized_text,
        "status": status,
        "weight": weight,
        "created_at": created_at,
        "updated_at": updated_at,
        "metadata": dict(normalized_metadata),
    })
    structure_hash = normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "source_conversation_identity": source_conversation_identity,
        "source_identity": provenance.source_identity,
        "source_node_identity": node_identity,
        "parent_source_identity": parent_identity,
        "child_source_identities": child_identities,
    })
    record_id = "nmsg_" + normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "source_identity": provenance.source_identity,
    })
    return NormalizedMessage(
        record_id=record_id,
        provenance=provenance,
        source_conversation_id=source_conversation_id,
        source_node_id=source_node_id,
        role=role,
        author=normalized_author,
        content_type=content_type,
        text=normalized_text,
        status=status,
        weight=weight,
        created_at=created_at,
        updated_at=updated_at,
        parent_source_message_id=parent_source_message_id,
        child_source_message_ids=tuple(child_source_message_ids),
        normalized_metadata=normalized_metadata,
        content_hash=content_hash,
        structure_hash=structure_hash,
        warning_codes=tuple(warning_codes),
        conflict_state=conflict_state,
        production_message_id=production_message_id,
        missing_reasons=missing_reasons or {},
    )


@dataclass(frozen=True)
class NormalizedAttachment:
    record_id: str
    provenance: RecordProvenance
    source_conversation_id: str = field(repr=False)
    source_message_id: str = field(repr=False)
    original_filename: str = field(repr=False)
    safe_display_filename: str = field(repr=False)
    declared_mime: str | None
    detected_mime: str | None
    declared_size: int | None
    actual_size: int | None
    content_sha256: str | None
    archive_member_locator: RecordLocator
    entity_state: str
    unavailable_reason: str | None
    asset_entity_id: str | None
    content_hash: str
    metadata_digest: str
    warning_codes: tuple[str, ...]
    conflict_state: str
    production_binding_id: str | None = field(default=None, repr=False)
    missing_reasons: Mapping[str, str] = field(default_factory=dict, repr=False)
    normalization_protocol: str = NORMALIZATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _identity(self.record_id, "invalid_attachment_record_id")
        if self.provenance.record_kind != "attachment":
            raise IngestionPlanningError("attachment_provenance_kind_mismatch")
        _private_id(self.source_conversation_id, "invalid_source_conversation_id")
        _private_id(self.source_message_id, "invalid_source_message_id")
        object.__setattr__(self, "original_filename", _strict_text(
            self.original_filename, "invalid_original_filename", max_chars=4096,
        ))
        object.__setattr__(self, "safe_display_filename", _strict_text(
            self.safe_display_filename, "invalid_safe_display_filename", max_chars=512,
        ))
        object.__setattr__(self, "declared_mime", _mime(
            self.declared_mime, "invalid_declared_mime",
        ))
        object.__setattr__(self, "detected_mime", _mime(
            self.detected_mime, "invalid_detected_mime",
        ))
        if self.declared_size is not None:
            _count(self.declared_size, "invalid_declared_size")
        if self.actual_size is not None:
            _count(self.actual_size, "invalid_actual_size")
        if self.content_sha256 is not None:
            _sha256(self.content_sha256, "invalid_attachment_content_sha256")
        if self.entity_state not in ENTITY_STATES:
            raise IngestionPlanningError("invalid_entity_state")
        if self.unavailable_reason is not None:
            object.__setattr__(
                self, "unavailable_reason", sanitize_reason_code(self.unavailable_reason),
            )
        if self.entity_state == "entity_available":
            if (
                self.content_sha256 is None
                or self.actual_size is None
                or self.detected_mime is None
                or self.unavailable_reason is not None
            ):
                raise IngestionPlanningError("available_entity_evidence_incomplete")
        elif self.unavailable_reason is None:
            raise IngestionPlanningError("unavailable_entity_reason_missing")
        if self.asset_entity_id is not None:
            _identity(self.asset_entity_id, "invalid_asset_entity_id")
        _sha256(self.content_hash, "invalid_attachment_content_hash")
        _sha256(self.metadata_digest, "invalid_attachment_metadata_digest")
        object.__setattr__(self, "warning_codes", _warnings(self.warning_codes))
        if self.conflict_state not in CONFLICT_STATES:
            raise IngestionPlanningError("invalid_conflict_state")
        object.__setattr__(self, "production_binding_id", _optional_private_id(
            self.production_binding_id, "invalid_production_binding_id",
        ))
        object.__setattr__(self, "missing_reasons", _missing_reasons(self.missing_reasons))
        for optional_field, optional_value in (
            ("declared_mime", self.declared_mime),
            ("detected_mime", self.detected_mime),
            ("declared_size", self.declared_size),
            ("actual_size", self.actual_size),
            ("content_sha256", self.content_sha256),
        ):
            if optional_value is None and optional_field not in self.missing_reasons:
                raise IngestionPlanningError(f"missing_{optional_field}_reason")
        if self.normalization_protocol != NORMALIZATION_PROTOCOL_VERSION:
            raise IngestionPlanningError("normalization_protocol_mismatch")

    @property
    def source_identity(self) -> str:
        return self.provenance.source_identity

    @property
    def source_conversation_identity(self) -> str:
        return make_source_identity(
            "conversation", self.provenance.source_type, self.source_conversation_id,
        )

    @property
    def source_message_identity(self) -> str:
        return make_source_identity(
            "message", self.provenance.source_type, self.source_message_id,
        )

    def plan_descriptor(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_kind": "attachment",
            "provenance": self.provenance.plan_descriptor(),
            "source_identity": self.source_identity,
            "source_conversation_identity": self.source_conversation_identity,
            "source_message_identity": self.source_message_identity,
            "content_hash": self.content_hash,
            "structure_hash": _field_hash({
                "conversation": self.source_conversation_identity,
                "message": self.source_message_identity,
            }),
            "metadata_digest": self.metadata_digest,
            "content_sha256": self.content_sha256,
            "actual_size": self.actual_size,
            "entity_state": self.entity_state,
            "unavailable_reason": self.unavailable_reason,
            "asset_entity_id": self.asset_entity_id,
            "archive_member_locator": self.archive_member_locator.to_dict(),
            "warning_codes": list(self.warning_codes),
            "conflict_state": self.conflict_state,
            "production_identity_digest": (
                _field_hash(self.production_binding_id)
                if self.production_binding_id else None
            ),
            "missing_reasons": dict(self.missing_reasons),
            "normalization_protocol": self.normalization_protocol,
        }


def normalize_attachment(
    provenance: RecordProvenance,
    *,
    source_conversation_id: str,
    source_message_id: str,
    original_filename: str,
    safe_display_filename: str,
    declared_mime: str | None,
    detected_mime: str | None,
    declared_size: int | None,
    actual_size: int | None,
    content_sha256: str | None,
    archive_member_locator: RecordLocator,
    entity_state: str,
    unavailable_reason: str | None,
    asset_entity_id: str | None = None,
    warning_codes: Sequence[str] = (),
    conflict_state: str = "clear",
    production_binding_id: str | None = None,
    missing_reasons: Mapping[str, str] | None = None,
) -> NormalizedAttachment:
    normalized_original = _strict_text(
        original_filename, "invalid_original_filename", max_chars=4096,
    )
    normalized_display = _strict_text(
        safe_display_filename, "invalid_safe_display_filename", max_chars=512,
    )
    metadata_digest = normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "original_filename": normalized_original,
        "safe_display_filename": normalized_display,
        "declared_mime": declared_mime,
        "detected_mime": detected_mime,
        "declared_size": declared_size,
    })
    content_hash = normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "metadata_digest": metadata_digest,
        "actual_size": actual_size,
        "content_sha256": content_sha256,
        "entity_state": entity_state,
        "unavailable_reason": (
            sanitize_reason_code(unavailable_reason)
            if unavailable_reason is not None else None
        ),
    })
    record_id = "natt_" + normalized_json_digest({
        "normalization_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "source_identity": provenance.source_identity,
    })
    return NormalizedAttachment(
        record_id=record_id,
        provenance=provenance,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
        original_filename=normalized_original,
        safe_display_filename=normalized_display,
        declared_mime=declared_mime,
        detected_mime=detected_mime,
        declared_size=declared_size,
        actual_size=actual_size,
        content_sha256=content_sha256,
        archive_member_locator=archive_member_locator,
        entity_state=entity_state,
        unavailable_reason=unavailable_reason,
        asset_entity_id=asset_entity_id,
        content_hash=content_hash,
        metadata_digest=metadata_digest,
        warning_codes=tuple(warning_codes),
        conflict_state=conflict_state,
        production_binding_id=production_binding_id,
        missing_reasons=missing_reasons or {},
    )


@dataclass(frozen=True)
class AssetEntity:
    asset_entity_id: str
    content_sha256: str
    actual_size: int
    detected_mime: str
    storage_root_id: str
    relative_entity_path: str = field(repr=False)
    first_seen_batch_id: str
    integrity_state: str
    created_at: str

    def __post_init__(self) -> None:
        _identity(self.asset_entity_id, "invalid_asset_entity_id")
        _sha256(self.content_sha256, "invalid_asset_content_sha256")
        _count(self.actual_size, "invalid_asset_size")
        object.__setattr__(self, "detected_mime", _mime(
            self.detected_mime, "invalid_detected_mime",
        ))
        _token(self.storage_root_id, "invalid_storage_root_id")
        object.__setattr__(self, "relative_entity_path", _relative_path(
            self.relative_entity_path,
        ))
        _identity(self.first_seen_batch_id, "invalid_batch_id")
        if self.integrity_state not in INTEGRITY_STATES:
            raise IngestionPlanningError("invalid_integrity_state")
        _timestamp(self.created_at, "invalid_asset_created_at")
        expected = "asset_" + normalized_json_digest({
            "asset_identity_protocol": NORMALIZATION_PROTOCOL_VERSION,
            "content_sha256": self.content_sha256,
            "actual_size": self.actual_size,
            "detected_mime": self.detected_mime,
        })
        if self.asset_entity_id != expected:
            raise IngestionPlanningError("asset_entity_identity_mismatch")

    @property
    def source_identity(self) -> str:
        return self.asset_entity_id

    def plan_descriptor(self) -> dict[str, Any]:
        return {
            "record_kind": "asset",
            "source_identity": self.source_identity,
            "content_hash": _field_hash({
                "content_sha256": self.content_sha256,
                "actual_size": self.actual_size,
                "detected_mime": self.detected_mime,
            }),
            "structure_hash": _field_hash({
                "storage_root_id": self.storage_root_id,
                "relative_entity_path_digest": _field_hash(self.relative_entity_path),
            }),
            "content_sha256": self.content_sha256,
            "actual_size": self.actual_size,
            "detected_mime": self.detected_mime,
            "integrity_state": self.integrity_state,
            "first_seen_batch_id": self.first_seen_batch_id,
        }


def make_asset_entity(
    *,
    content_sha256: str,
    actual_size: int,
    detected_mime: str,
    storage_root_id: str,
    relative_entity_path: str,
    first_seen_batch_id: str,
    integrity_state: str,
    created_at: str,
) -> AssetEntity:
    asset_id = "asset_" + normalized_json_digest({
        "asset_identity_protocol": NORMALIZATION_PROTOCOL_VERSION,
        "content_sha256": content_sha256,
        "actual_size": actual_size,
        "detected_mime": detected_mime,
    })
    return AssetEntity(
        asset_entity_id=asset_id,
        content_sha256=content_sha256,
        actual_size=actual_size,
        detected_mime=detected_mime,
        storage_root_id=storage_root_id,
        relative_entity_path=relative_entity_path,
        first_seen_batch_id=first_seen_batch_id,
        integrity_state=integrity_state,
        created_at=created_at,
    )


@dataclass(frozen=True)
class ExistingRecord:
    record_kind: str
    source_type: str
    source_identity: str
    production_identity: str = field(repr=False)
    content_hash: str
    structure_hash: str
    field_hashes: Mapping[str, str]
    parent_source_identity: str | None = None
    occurred_at: str | None = None
    metadata_digest: str | None = None
    content_sha256: str | None = None
    actual_size: int | None = None

    def __post_init__(self) -> None:
        if self.record_kind not in RECORD_KINDS:
            raise IngestionPlanningError("invalid_record_kind")
        _token(self.source_type, "invalid_source_type")
        _identity(self.source_identity, "invalid_source_identity")
        _private_id(self.production_identity, "invalid_production_identity")
        _sha256(self.content_hash, "invalid_existing_content_hash")
        _sha256(self.structure_hash, "invalid_existing_structure_hash")
        hashes: dict[str, str] = {}
        for key, value in self.field_hashes.items():
            hashes[_token(key, "invalid_field_hash_name")] = _sha256(
                value, "invalid_field_hash",
            )
        object.__setattr__(self, "field_hashes", MappingProxyType(dict(sorted(hashes.items()))))
        if self.parent_source_identity is not None:
            _identity(self.parent_source_identity, "invalid_parent_source_identity")
        _timestamp(self.occurred_at, "invalid_existing_occurred_at", allow_none=True)
        if self.metadata_digest is not None:
            _sha256(self.metadata_digest, "invalid_metadata_digest")
        if self.content_sha256 is not None:
            _sha256(self.content_sha256, "invalid_content_sha256")
        if self.actual_size is not None:
            _count(self.actual_size, "invalid_actual_size")

    @property
    def production_identity_digest(self) -> str:
        return _field_hash(self.production_identity)

    def snapshot_descriptor(self) -> dict[str, Any]:
        return {
            "record_kind": self.record_kind,
            "source_type": self.source_type,
            "source_identity": self.source_identity,
            "production_identity_digest": self.production_identity_digest,
            "content_hash": self.content_hash,
            "structure_hash": self.structure_hash,
            "field_hashes": dict(self.field_hashes),
            "parent_source_identity": self.parent_source_identity,
            "occurred_at": self.occurred_at,
            "metadata_digest": self.metadata_digest,
            "content_sha256": self.content_sha256,
            "actual_size": self.actual_size,
        }


@dataclass(frozen=True)
class ImportConflict:
    conflict_id: str
    batch_id: str
    record_kind: str
    source_identity: str
    production_identity_digest: str | None
    conflict_type: str
    evidence_hashes: tuple[str, ...]
    risk_level: str
    allowed_decisions: tuple[str, ...]
    status: str = "pending"
    conflict_schema_version: str = CONFLICT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.conflict_id, "invalid_conflict_id")
        _identity(self.batch_id, "invalid_batch_id")
        if self.record_kind not in RECORD_KINDS:
            raise IngestionPlanningError("invalid_record_kind")
        _identity(self.source_identity, "invalid_source_identity")
        if self.production_identity_digest is not None:
            _sha256(self.production_identity_digest, "invalid_production_identity_digest")
        _token(self.conflict_type, "invalid_conflict_type")
        evidence = tuple(sorted(_sha256(value, "invalid_evidence_hash") for value in self.evidence_hashes))
        object.__setattr__(self, "evidence_hashes", evidence)
        if self.risk_level not in RISK_LEVELS:
            raise IngestionPlanningError("invalid_risk_level")
        decisions = tuple(sorted(set(self.allowed_decisions)))
        if not decisions or any(value not in DECISIONS for value in decisions):
            raise IngestionPlanningError("invalid_allowed_decisions")
        object.__setattr__(self, "allowed_decisions", decisions)
        if self.status not in CONFLICT_STATUSES:
            raise IngestionPlanningError("invalid_conflict_status")
        if self.conflict_schema_version != CONFLICT_SCHEMA_VERSION:
            raise IngestionPlanningError("conflict_schema_version_mismatch")
        expected = "conflict_" + normalized_json_digest(self.identity_payload())
        if self.conflict_id != expected:
            raise IngestionPlanningError("conflict_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "record_kind": self.record_kind,
            "source_identity": self.source_identity,
            "production_identity_digest": self.production_identity_digest,
            "conflict_type": self.conflict_type,
            "evidence_hashes": list(self.evidence_hashes),
            "risk_level": self.risk_level,
            "allowed_decisions": list(self.allowed_decisions),
            "status": self.status,
            "conflict_schema_version": self.conflict_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"conflict_id": self.conflict_id, **self.identity_payload()}


def make_import_conflict(
    *,
    batch_id: str,
    record_kind: str,
    source_identity: str,
    production_identity_digest: str | None,
    conflict_type: str,
    evidence_hashes: Sequence[str],
    risk_level: str,
    allowed_decisions: Sequence[str],
) -> ImportConflict:
    payload = {
        "batch_id": batch_id,
        "record_kind": record_kind,
        "source_identity": source_identity,
        "production_identity_digest": production_identity_digest,
        "conflict_type": conflict_type,
        "evidence_hashes": sorted(evidence_hashes),
        "risk_level": risk_level,
        "allowed_decisions": sorted(set(allowed_decisions)),
        "status": "pending",
        "conflict_schema_version": CONFLICT_SCHEMA_VERSION,
    }
    return ImportConflict(
        conflict_id="conflict_" + normalized_json_digest(payload),
        **payload,
    )


@dataclass(frozen=True)
class ImportDecision:
    decision_id: str
    conflict_id: str | None
    plan_scope: str
    decision: str
    actor_kind: str
    policy_version: str
    evidence_hash: str
    decided_at: str
    supersedes_decision_id: str | None = None
    decision_schema_version: str = DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.decision_id, "invalid_decision_id")
        if self.conflict_id is not None:
            _identity(self.conflict_id, "invalid_conflict_id")
        _identity(self.plan_scope, "invalid_plan_scope")
        if self.decision not in DECISIONS:
            raise IngestionPlanningError("invalid_decision")
        _token(self.actor_kind, "invalid_actor_kind")
        _token(self.policy_version, "invalid_policy_version")
        _sha256(self.evidence_hash, "invalid_evidence_hash")
        _timestamp(self.decided_at, "invalid_decided_at")
        if self.supersedes_decision_id is not None:
            _identity(self.supersedes_decision_id, "invalid_superseded_decision_id")
        if self.decision_schema_version != DECISION_SCHEMA_VERSION:
            raise IngestionPlanningError("decision_schema_version_mismatch")
        expected = "decision_" + normalized_json_digest(self.identity_payload())
        if self.decision_id != expected:
            raise IngestionPlanningError("decision_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "plan_scope": self.plan_scope,
            "decision": self.decision,
            "actor_kind": self.actor_kind,
            "policy_version": self.policy_version,
            "evidence_hash": self.evidence_hash,
            "supersedes_decision_id": self.supersedes_decision_id,
            "decision_schema_version": self.decision_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            **self.identity_payload(),
            "decided_at": self.decided_at,
        }


def make_import_decision(
    *,
    conflict_id: str | None,
    plan_scope: str,
    decision: str,
    actor_kind: str,
    policy_version: str,
    evidence_hash: str,
    decided_at: str,
    supersedes_decision_id: str | None = None,
) -> ImportDecision:
    payload = {
        "conflict_id": conflict_id,
        "plan_scope": plan_scope,
        "decision": decision,
        "actor_kind": actor_kind,
        "policy_version": policy_version,
        "evidence_hash": evidence_hash,
        "supersedes_decision_id": supersedes_decision_id,
        "decision_schema_version": DECISION_SCHEMA_VERSION,
    }
    return ImportDecision(
        decision_id="decision_" + normalized_json_digest(payload),
        decided_at=decided_at,
        **payload,
    )


@dataclass(frozen=True)
class ImportVerification:
    verification_id: str
    batch_id: str
    phase: str
    database_snapshot: DatabaseFingerprint | None
    asset_snapshot: AssetStageFingerprint | None
    expected_counts: Mapping[str, int]
    actual_counts: Mapping[str, int]
    integrity_check: str
    foreign_key_check: str
    index_checks: Mapping[str, str]
    source_binding_checks: Mapping[str, str]
    completed_state_check: str
    result: str
    verified_at: str
    verification_schema_version: str = VERIFICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.verification_id, "invalid_verification_id")
        _identity(self.batch_id, "invalid_batch_id")
        if self.phase not in {"precommit", "postcommit", "restore"}:
            raise IngestionPlanningError("invalid_verification_phase")
        expected = _count_mapping(self.expected_counts, "invalid_expected_counts")
        actual = _count_mapping(self.actual_counts, "invalid_actual_counts")
        object.__setattr__(self, "expected_counts", MappingProxyType(expected))
        object.__setattr__(self, "actual_counts", MappingProxyType(actual))
        for name in (
            "integrity_check", "foreign_key_check", "completed_state_check", "result",
        ):
            _token(getattr(self, name), f"invalid_{name}")
        object.__setattr__(self, "index_checks", MappingProxyType(
            _token_mapping(self.index_checks, "invalid_index_checks"),
        ))
        object.__setattr__(self, "source_binding_checks", MappingProxyType(
            _token_mapping(self.source_binding_checks, "invalid_source_binding_checks"),
        ))
        _timestamp(self.verified_at, "invalid_verified_at")
        if self.verification_schema_version != VERIFICATION_SCHEMA_VERSION:
            raise IngestionPlanningError("verification_schema_version_mismatch")
        expected_id = "verification_" + normalized_json_digest(self.identity_payload())
        if self.verification_id != expected_id:
            raise IngestionPlanningError("verification_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "phase": self.phase,
            "database_snapshot": (
                self.database_snapshot.to_dict() if self.database_snapshot else None
            ),
            "asset_snapshot": (
                self.asset_snapshot.to_dict() if self.asset_snapshot else None
            ),
            "expected_counts": dict(self.expected_counts),
            "actual_counts": dict(self.actual_counts),
            "integrity_check": self.integrity_check,
            "foreign_key_check": self.foreign_key_check,
            "index_checks": dict(self.index_checks),
            "source_binding_checks": dict(self.source_binding_checks),
            "completed_state_check": self.completed_state_check,
            "result": self.result,
            "verification_schema_version": self.verification_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            **self.identity_payload(),
            "verified_at": self.verified_at,
        }


def _count_mapping(value: Mapping[str, int], reason_code: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or len(value) > 256:
        raise IngestionPlanningError(reason_code)
    result: dict[str, int] = {}
    for key, count in value.items():
        result[_token(key, reason_code)] = _count(count, reason_code)
    return dict(sorted(result.items()))


def _token_mapping(value: Mapping[str, str], reason_code: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 256:
        raise IngestionPlanningError(reason_code)
    return dict(sorted(
        (
            _token(key, reason_code),
            _token(item, reason_code),
        )
        for key, item in value.items()
    ))


def make_import_verification(
    *,
    batch_id: str,
    phase: str,
    database_snapshot: DatabaseFingerprint | None,
    asset_snapshot: AssetStageFingerprint | None,
    expected_counts: Mapping[str, int],
    actual_counts: Mapping[str, int],
    integrity_check: str,
    foreign_key_check: str,
    index_checks: Mapping[str, str],
    source_binding_checks: Mapping[str, str],
    completed_state_check: str,
    result: str,
    verified_at: str,
) -> ImportVerification:
    payload = {
        "batch_id": batch_id,
        "phase": phase,
        "database_snapshot": (
            database_snapshot.to_dict() if database_snapshot else None
        ),
        "asset_snapshot": asset_snapshot.to_dict() if asset_snapshot else None,
        "expected_counts": dict(expected_counts),
        "actual_counts": dict(actual_counts),
        "integrity_check": integrity_check,
        "foreign_key_check": foreign_key_check,
        "index_checks": dict(index_checks),
        "source_binding_checks": dict(source_binding_checks),
        "completed_state_check": completed_state_check,
        "result": result,
        "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
    }
    verification_id = "verification_" + normalized_json_digest(payload)
    return ImportVerification(
        verification_id=verification_id,
        batch_id=batch_id,
        phase=phase,
        database_snapshot=database_snapshot,
        asset_snapshot=asset_snapshot,
        expected_counts=expected_counts,
        actual_counts=actual_counts,
        integrity_check=integrity_check,
        foreign_key_check=foreign_key_check,
        index_checks=index_checks,
        source_binding_checks=source_binding_checks,
        completed_state_check=completed_state_check,
        result=result,
        verified_at=verified_at,
    )


@dataclass(frozen=True)
class PlanAction:
    action_id: str
    action_type: str
    record_kind: str
    source_identity: str
    production_identity_digest: str | None
    conflict_id: str | None
    evidence_codes: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    requires_approval: bool

    def __post_init__(self) -> None:
        _identity(self.action_id, "invalid_action_id")
        if self.action_type not in ACTION_TYPES:
            raise IngestionPlanningError("invalid_action_type")
        if self.record_kind not in RECORD_KINDS:
            raise IngestionPlanningError("invalid_record_kind")
        _identity(self.source_identity, "invalid_source_identity")
        if self.production_identity_digest is not None:
            _sha256(self.production_identity_digest, "invalid_production_identity_digest")
        if self.conflict_id is not None:
            _identity(self.conflict_id, "invalid_conflict_id")
        codes = tuple(sorted(_token(code, "invalid_evidence_code") for code in self.evidence_codes))
        hashes = tuple(sorted(_sha256(value, "invalid_evidence_hash") for value in self.evidence_hashes))
        object.__setattr__(self, "evidence_codes", codes)
        object.__setattr__(self, "evidence_hashes", hashes)
        if not isinstance(self.requires_approval, bool):
            raise IngestionPlanningError("invalid_approval_requirement")
        expected = "action_" + normalized_json_digest(self.identity_payload())
        if self.action_id != expected:
            raise IngestionPlanningError("action_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "record_kind": self.record_kind,
            "source_identity": self.source_identity,
            "production_identity_digest": self.production_identity_digest,
            "conflict_id": self.conflict_id,
            "evidence_codes": list(self.evidence_codes),
            "evidence_hashes": list(self.evidence_hashes),
            "requires_approval": self.requires_approval,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"action_id": self.action_id, **self.identity_payload()}


def _make_action(
    *,
    action_type: str,
    record_kind: str,
    source_identity: str,
    production_identity_digest: str | None = None,
    conflict_id: str | None = None,
    evidence_codes: Sequence[str] = (),
    evidence_hashes: Sequence[str] = (),
    requires_approval: bool = False,
) -> PlanAction:
    payload = {
        "action_type": action_type,
        "record_kind": record_kind,
        "source_identity": source_identity,
        "production_identity_digest": production_identity_digest,
        "conflict_id": conflict_id,
        "evidence_codes": sorted(evidence_codes),
        "evidence_hashes": sorted(evidence_hashes),
        "requires_approval": requires_approval,
    }
    return PlanAction(
        action_id="action_" + normalized_json_digest(payload),
        **payload,
    )


@dataclass(frozen=True)
class ImportPlan:
    plan_id: str
    batch_id: str
    manifest_id: str
    archive_sha256: str
    adapter_version: str
    normalization_protocol: str
    policy_version: str
    production_snapshot_digest: str
    normalized_records_digest: str
    asset_inventory_digest: str
    actions: tuple[PlanAction, ...]
    conflicts: tuple[ImportConflict, ...]
    automatic_decisions: tuple[ImportDecision, ...]
    counts: Mapping[str, int]
    approval_required: bool
    commit_prerequisites: tuple[str, ...]
    source_snapshot_complete: bool
    created_at: str
    plan_schema_version: str = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.plan_id, "invalid_plan_id")
        _identity(self.batch_id, "invalid_batch_id")
        _identity(self.manifest_id, "invalid_manifest_id")
        _sha256(self.archive_sha256, "invalid_archive_sha256")
        _token(self.adapter_version, "invalid_adapter_version")
        _token(self.normalization_protocol, "invalid_normalization_protocol")
        _token(self.policy_version, "invalid_policy_version")
        _sha256(self.production_snapshot_digest, "invalid_production_snapshot_digest")
        _sha256(self.normalized_records_digest, "invalid_normalized_records_digest")
        _sha256(self.asset_inventory_digest, "invalid_asset_inventory_digest")
        actions = tuple(sorted(self.actions, key=lambda item: item.action_id))
        conflicts = tuple(sorted(self.conflicts, key=lambda item: item.conflict_id))
        decisions = tuple(sorted(self.automatic_decisions, key=lambda item: item.decision_id))
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "conflicts", conflicts)
        object.__setattr__(self, "automatic_decisions", decisions)
        object.__setattr__(self, "counts", MappingProxyType(
            _count_mapping(self.counts, "invalid_plan_counts"),
        ))
        if not isinstance(self.approval_required, bool):
            raise IngestionPlanningError("invalid_approval_requirement")
        prerequisites = tuple(sorted(set(
            _token(value, "invalid_commit_prerequisite")
            for value in self.commit_prerequisites
        )))
        object.__setattr__(self, "commit_prerequisites", prerequisites)
        if not isinstance(self.source_snapshot_complete, bool):
            raise IngestionPlanningError("invalid_snapshot_completeness")
        _timestamp(self.created_at, "invalid_plan_created_at")
        if self.plan_schema_version != PLAN_SCHEMA_VERSION:
            raise IngestionPlanningError("plan_schema_version_mismatch")
        expected = "plan_" + normalized_json_digest(self.identity_payload())
        if self.plan_id != expected:
            raise IngestionPlanningError("plan_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "manifest_id": self.manifest_id,
            "archive_sha256": self.archive_sha256,
            "adapter_version": self.adapter_version,
            "normalization_protocol": self.normalization_protocol,
            "policy_version": self.policy_version,
            "production_snapshot_digest": self.production_snapshot_digest,
            "normalized_records_digest": self.normalized_records_digest,
            "asset_inventory_digest": self.asset_inventory_digest,
            "actions": [item.to_dict() for item in self.actions],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "automatic_decisions": [
                {
                    key: value
                    for key, value in item.to_dict().items()
                    if key != "decided_at"
                }
                for item in self.automatic_decisions
            ],
            "counts": dict(self.counts),
            "approval_required": self.approval_required,
            "commit_prerequisites": list(self.commit_prerequisites),
            "source_snapshot_complete": self.source_snapshot_complete,
            "plan_schema_version": self.plan_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            **self.identity_payload(),
            "automatic_decisions": [
                item.to_dict() for item in self.automatic_decisions
            ],
            "created_at": self.created_at,
        }


NormalizedRecord = NormalizedConversation | NormalizedMessage | NormalizedAttachment | AssetEntity


def _record_descriptor(record: NormalizedRecord) -> dict[str, Any]:
    return record.plan_descriptor()


def _record_kind(record: NormalizedRecord) -> str:
    return _record_descriptor(record)["record_kind"]


def _conflict_for(
    batch_id: str,
    descriptor: Mapping[str, Any],
    existing: ExistingRecord | None,
    conflict_type: str,
    *,
    risk: str = "high",
    evidence: Sequence[Any] = (),
    decisions: Sequence[str] = ("keep_existing", "accept_incoming", "manual_review"),
) -> ImportConflict:
    evidence_hashes = [
        normalized_json_digest({
            "evidence_protocol": PLANNING_POLICY_VERSION,
            "value": value,
        })
        for value in evidence
    ]
    return make_import_conflict(
        batch_id=batch_id,
        record_kind=descriptor["record_kind"],
        source_identity=descriptor["source_identity"],
        production_identity_digest=(
            existing.production_identity_digest if existing else None
        ),
        conflict_type=conflict_type,
        evidence_hashes=evidence_hashes,
        risk_level=risk,
        allowed_decisions=decisions,
    )


def _decision_for_action(
    action: PlanAction,
    *,
    policy_version: str,
    decided_at: str,
) -> ImportDecision:
    decision = {
        "insert": "insert",
        "append": "append",
        "no_change": "no_change",
        "share_entity": "share_entity",
        "metadata_only": "metadata_only",
        "link_candidate": "link_candidate",
        "tombstone_candidate": "tombstone_candidate",
        "reject": "reject",
    }.get(action.action_type)
    if decision is None:
        raise IngestionPlanningError("conflict_action_cannot_auto_decide")
    evidence_hash = normalized_json_digest({
        "policy_version": policy_version,
        "action": action.to_dict(),
    })
    return make_import_decision(
        conflict_id=None,
        plan_scope=action.action_id,
        decision=decision,
        actor_kind="policy_engine",
        policy_version=policy_version,
        evidence_hash=evidence_hash,
        decided_at=decided_at,
    )


def plan_import(
    *,
    manifest: SourceManifest,
    batch: ImportBatch,
    conversations: Sequence[NormalizedConversation],
    messages: Sequence[NormalizedMessage],
    attachments: Sequence[NormalizedAttachment],
    asset_entities: Sequence[AssetEntity],
    existing_records: Sequence[ExistingRecord],
    production_fingerprint: DatabaseFingerprint,
    asset_stage_fingerprint: AssetStageFingerprint | None,
    created_at: str,
    source_snapshot_complete: bool = True,
    normalization_protocol: str = NORMALIZATION_PROTOCOL_VERSION,
    policy_version: str = PLANNING_POLICY_VERSION,
) -> ImportPlan:
    if (
        batch.manifest_id != manifest.manifest_id
        or batch.source_type != manifest.source_type
        or batch.adapter_name != manifest.adapter_name
        or batch.adapter_version != manifest.adapter_version
    ):
        raise IngestionPlanningError("batch_manifest_binding_mismatch")
    if normalization_protocol != NORMALIZATION_PROTOCOL_VERSION:
        raise IngestionPlanningError("normalization_protocol_mismatch")
    _token(policy_version, "invalid_policy_version")
    _timestamp(created_at, "invalid_plan_created_at")
    if not isinstance(source_snapshot_complete, bool):
        raise IngestionPlanningError("invalid_snapshot_completeness")

    records: list[NormalizedRecord] = [
        *conversations, *messages, *attachments, *asset_entities,
    ]
    if len(records) > MAX_RECORDS_PER_PLAN:
        raise IngestionPlanningError("plan_record_limit_exceeded")
    descriptors = [_record_descriptor(record) for record in records]
    for record in records:
        if isinstance(record, AssetEntity):
            if record.first_seen_batch_id != batch.batch_id:
                raise IngestionPlanningError("asset_batch_binding_mismatch")
            continue
        provenance = record.provenance
        if (
            provenance.manifest_id != manifest.manifest_id
            or provenance.batch_id != batch.batch_id
            or provenance.source_type != manifest.source_type
            or provenance.adapter_version != manifest.adapter_version
        ):
            raise IngestionPlanningError("record_provenance_binding_mismatch")

    existing_by_key: dict[tuple[str, str], ExistingRecord] = {}
    for existing in existing_records:
        key = (existing.record_kind, existing.source_identity)
        if key in existing_by_key:
            raise IngestionPlanningError("ambiguous_existing_binding")
        existing_by_key[key] = existing

    descriptor_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for descriptor in descriptors:
        key = (descriptor["record_kind"], descriptor["source_identity"])
        descriptor_groups.setdefault(key, []).append(descriptor)

    actions: list[PlanAction] = []
    conflicts: list[ImportConflict] = []
    duplicate_keys: set[tuple[str, str]] = set()

    for key, group in sorted(descriptor_groups.items()):
        if len(group) < 2:
            continue
        duplicate_keys.add(key)
        parent_values = {
            item.get("source_conversation_identity")
            or item.get("source_message_identity")
            for item in group
        }
        conflict_type = (
            "duplicate_source_id_cross_parent"
            if len(parent_values) > 1 else "duplicate_source_identity"
        )
        conflict = _conflict_for(
            batch.batch_id,
            group[0],
            existing_by_key.get(key),
            conflict_type,
            evidence=[
                {"content_hash": item["content_hash"], "structure_hash": item["structure_hash"]}
                for item in group
            ],
            decisions=("keep_existing", "preserve_both", "manual_review", "reject"),
        )
        conflicts.append(conflict)
        actions.append(_make_action(
            action_type="conflict",
            record_kind=key[0],
            source_identity=key[1],
            production_identity_digest=(
                existing_by_key[key].production_identity_digest
                if key in existing_by_key else None
            ),
            conflict_id=conflict.conflict_id,
            evidence_codes=(conflict_type,),
            evidence_hashes=conflict.evidence_hashes,
            requires_approval=True,
        ))

    conversation_identities = {
        descriptor["source_identity"]
        for descriptor in descriptors
        if descriptor["record_kind"] == "conversation"
    } | {
        existing.source_identity
        for existing in existing_records
        if existing.record_kind == "conversation"
    }
    message_identities = {
        descriptor["source_identity"]
        for descriptor in descriptors
        if descriptor["record_kind"] == "message"
    } | {
        existing.source_identity
        for existing in existing_records
        if existing.record_kind == "message"
    }
    latest_message_time: dict[str, str] = {}
    for existing in existing_records:
        if (
            existing.record_kind == "message"
            and existing.parent_source_identity
            and existing.occurred_at
        ):
            latest_message_time[existing.parent_source_identity] = max(
                existing.occurred_at,
                latest_message_time.get(existing.parent_source_identity, existing.occurred_at),
            )

    existing_content: dict[tuple[str, str], list[ExistingRecord]] = {}
    existing_message_text: dict[str, list[ExistingRecord]] = {}
    existing_attachment_metadata: dict[str, list[ExistingRecord]] = {}
    existing_assets_by_sha: dict[str, list[ExistingRecord]] = {}
    for existing in existing_records:
        existing_content.setdefault(
            (existing.record_kind, existing.content_hash), [],
        ).append(existing)
        if (
            existing.record_kind == "message"
            and "text" in existing.field_hashes
        ):
            existing_message_text.setdefault(
                existing.field_hashes["text"], [],
            ).append(existing)
        if existing.record_kind == "attachment" and existing.metadata_digest:
            existing_attachment_metadata.setdefault(existing.metadata_digest, []).append(existing)
        if existing.record_kind == "asset" and existing.content_sha256:
            existing_assets_by_sha.setdefault(existing.content_sha256, []).append(existing)

    for descriptor in sorted(
        descriptors, key=lambda item: (item["record_kind"], item["source_identity"]),
    ):
        key = (descriptor["record_kind"], descriptor["source_identity"])
        if key in duplicate_keys:
            continue
        existing = existing_by_key.get(key)
        if existing is not None:
            if (
                descriptor["content_hash"] == existing.content_hash
                and descriptor["structure_hash"] == existing.structure_hash
            ):
                actions.append(_make_action(
                    action_type="no_change",
                    record_kind=key[0],
                    source_identity=key[1],
                    production_identity_digest=existing.production_identity_digest,
                    evidence_codes=("stable_source_and_content_match",),
                    evidence_hashes=(descriptor["content_hash"], descriptor["structure_hash"]),
                ))
                continue
            if descriptor["content_hash"] == existing.content_hash:
                conflict_type = "relationship_changed"
            elif key[0] == "conversation":
                conflict_type = "conversation_fields_changed"
            elif key[0] == "message":
                conflict_type = "message_content_changed"
            elif key[0] == "attachment":
                conflict_type = "attachment_content_changed"
            else:
                conflict_type = "asset_content_changed"
            conflict = _conflict_for(
                batch.batch_id,
                descriptor,
                existing,
                conflict_type,
                evidence=(
                    descriptor["content_hash"],
                    descriptor["structure_hash"],
                    existing.content_hash,
                    existing.structure_hash,
                    descriptor.get("field_hashes", {}),
                    dict(existing.field_hashes),
                ),
                decisions=("keep_existing", "accept_incoming", "manual_review"),
            )
            conflicts.append(conflict)
            actions.append(_make_action(
                action_type="conflict",
                record_kind=key[0],
                source_identity=key[1],
                production_identity_digest=existing.production_identity_digest,
                conflict_id=conflict.conflict_id,
                evidence_codes=(conflict_type,),
                evidence_hashes=conflict.evidence_hashes,
                requires_approval=True,
            ))
            continue

        if descriptor["record_kind"] == "message":
            parent = descriptor["source_conversation_identity"]
            if parent not in conversation_identities:
                conflict = _conflict_for(
                    batch.batch_id,
                    descriptor,
                    None,
                    "missing_conversation_parent",
                    evidence=(parent,),
                    decisions=("reject", "manual_review"),
                )
                conflicts.append(conflict)
                actions.append(_make_action(
                    action_type="conflict",
                    record_kind="message",
                    source_identity=descriptor["source_identity"],
                    conflict_id=conflict.conflict_id,
                    evidence_codes=("missing_conversation_parent",),
                    evidence_hashes=conflict.evidence_hashes,
                    requires_approval=True,
                ))
                continue
            parent_message = descriptor.get("parent_source_identity")
            if parent_message is not None and parent_message not in message_identities:
                conflict = _conflict_for(
                    batch.batch_id,
                    descriptor,
                    None,
                    "missing_message_parent",
                    evidence=(parent_message,),
                    decisions=("append", "reject", "manual_review"),
                )
                conflicts.append(conflict)
                actions.append(_make_action(
                    action_type="conflict",
                    record_kind="message",
                    source_identity=descriptor["source_identity"],
                    conflict_id=conflict.conflict_id,
                    evidence_codes=("missing_message_parent",),
                    evidence_hashes=conflict.evidence_hashes,
                    requires_approval=True,
                ))
                continue
            latest = latest_message_time.get(parent)
            if (
                latest is not None
                and descriptor.get("occurred_at") is not None
                and descriptor["occurred_at"] <= latest
            ):
                conflict = _conflict_for(
                    batch.batch_id,
                    descriptor,
                    None,
                    "old_time_new_id",
                    evidence=(descriptor["occurred_at"], latest),
                    decisions=("append", "reject", "manual_review"),
                )
                conflicts.append(conflict)
                actions.append(_make_action(
                    action_type="conflict",
                    record_kind="message",
                    source_identity=descriptor["source_identity"],
                    conflict_id=conflict.conflict_id,
                    evidence_codes=("old_time_new_id",),
                    evidence_hashes=conflict.evidence_hashes,
                    requires_approval=True,
                ))
                continue

        if descriptor["record_kind"] == "asset":
            content_sha = descriptor["content_sha256"]
            asset_matches = existing_assets_by_sha.get(content_sha, [])
            size_mismatch = any(
                item.actual_size is not None
                and item.actual_size != descriptor["actual_size"]
                for item in asset_matches
            )
            if size_mismatch:
                conflict = _conflict_for(
                    batch.batch_id,
                    descriptor,
                    None,
                    "hash_collision_or_corruption",
                    risk="critical",
                    evidence=(content_sha, descriptor["actual_size"]),
                    decisions=("reject", "manual_review"),
                )
                conflicts.append(conflict)
                actions.append(_make_action(
                    action_type="conflict",
                    record_kind="asset",
                    source_identity=descriptor["source_identity"],
                    conflict_id=conflict.conflict_id,
                    evidence_codes=("hash_collision_or_corruption",),
                    evidence_hashes=conflict.evidence_hashes,
                    requires_approval=True,
                ))
                continue
            if asset_matches:
                actions.append(_make_action(
                    action_type="share_entity",
                    record_kind="asset",
                    source_identity=descriptor["source_identity"],
                    production_identity_digest=asset_matches[0].production_identity_digest,
                    evidence_codes=("content_hash_matches_existing_entity",),
                    evidence_hashes=(content_sha,),
                ))
                continue

        if descriptor["record_kind"] == "attachment":
            if (
                descriptor["source_conversation_identity"] not in conversation_identities
                or descriptor["source_message_identity"] not in message_identities
            ):
                conflict = _conflict_for(
                    batch.batch_id,
                    descriptor,
                    None,
                    "attachment_relationship_missing",
                    evidence=(
                        descriptor["source_conversation_identity"],
                        descriptor["source_message_identity"],
                    ),
                    decisions=("reject", "manual_review"),
                )
                conflicts.append(conflict)
                actions.append(_make_action(
                    action_type="conflict",
                    record_kind="attachment",
                    source_identity=descriptor["source_identity"],
                    conflict_id=conflict.conflict_id,
                    evidence_codes=("attachment_relationship_missing",),
                    evidence_hashes=conflict.evidence_hashes,
                    requires_approval=True,
                ))
                continue
            content_sha = descriptor.get("content_sha256")
            if content_sha:
                asset_matches = existing_assets_by_sha.get(content_sha, [])
                size_mismatch = any(
                    item.actual_size is not None
                    and descriptor.get("actual_size") is not None
                    and item.actual_size != descriptor["actual_size"]
                    for item in asset_matches
                )
                if size_mismatch:
                    conflict = _conflict_for(
                        batch.batch_id,
                        descriptor,
                        None,
                        "hash_collision_or_corruption",
                        risk="critical",
                        evidence=(content_sha, descriptor.get("actual_size")),
                        decisions=("reject", "manual_review"),
                    )
                    conflicts.append(conflict)
                    actions.append(_make_action(
                        action_type="conflict",
                        record_kind="attachment",
                        source_identity=descriptor["source_identity"],
                        conflict_id=conflict.conflict_id,
                        evidence_codes=("hash_collision_or_corruption",),
                        evidence_hashes=conflict.evidence_hashes,
                        requires_approval=True,
                    ))
                    continue
                if asset_matches:
                    actions.append(_make_action(
                        action_type="share_entity",
                        record_kind="attachment",
                        source_identity=descriptor["source_identity"],
                        production_identity_digest=asset_matches[0].production_identity_digest,
                        evidence_codes=("content_hash_matches_existing_entity",),
                        evidence_hashes=(content_sha,),
                    ))
                    continue
            metadata_matches = existing_attachment_metadata.get(
                descriptor["metadata_digest"], [],
            )
            if metadata_matches and any(
                item.content_sha256 != descriptor.get("content_sha256")
                for item in metadata_matches
            ):
                conflict = _conflict_for(
                    batch.batch_id,
                    descriptor,
                    None,
                    "asset_metadata_content_mismatch",
                    evidence=(
                        descriptor["metadata_digest"],
                        descriptor.get("content_sha256"),
                        [item.content_sha256 for item in metadata_matches],
                    ),
                    decisions=("preserve_both", "reject", "manual_review"),
                )
                conflicts.append(conflict)
                actions.append(_make_action(
                    action_type="conflict",
                    record_kind="attachment",
                    source_identity=descriptor["source_identity"],
                    conflict_id=conflict.conflict_id,
                    evidence_codes=("asset_metadata_content_mismatch",),
                    evidence_hashes=conflict.evidence_hashes,
                    requires_approval=True,
                ))
                continue
            if descriptor["entity_state"] != "entity_available":
                if (
                    descriptor["entity_state"] == "entity_blocked"
                    and descriptor["unavailable_reason"]
                    == "ambiguous_asset_locator"
                ):
                    conflict = _conflict_for(
                        batch.batch_id,
                        descriptor,
                        None,
                        "asset_locator_ambiguous",
                        risk="high",
                        evidence=(
                            descriptor["content_hash"],
                            descriptor["metadata_digest"],
                        ),
                        decisions=("metadata_only", "reject", "manual_review"),
                    )
                    conflicts.append(conflict)
                    actions.append(_make_action(
                        action_type="conflict",
                        record_kind="attachment",
                        source_identity=descriptor["source_identity"],
                        conflict_id=conflict.conflict_id,
                        evidence_codes=("asset_locator_ambiguous",),
                        evidence_hashes=conflict.evidence_hashes,
                        requires_approval=True,
                    ))
                    continue
                actions.append(_make_action(
                    action_type="metadata_only",
                    record_kind="attachment",
                    source_identity=descriptor["source_identity"],
                    evidence_codes=(descriptor["unavailable_reason"],),
                    evidence_hashes=(descriptor["content_hash"],),
                    requires_approval=True,
                ))
                continue

        matches = existing_content.get(
            (descriptor["record_kind"], descriptor["content_hash"]), [],
        )
        if (
            not matches
            and descriptor["record_kind"] == "message"
            and "text" in descriptor.get("field_hashes", {})
        ):
            matches = existing_message_text.get(
                descriptor["field_hashes"]["text"], [],
            )
        evidence_codes: tuple[str, ...] = ()
        evidence_hashes: tuple[str, ...] = ()
        if matches:
            evidence_codes = ("similar_content_different_identity",)
            evidence_hashes = tuple(
                sorted(item.production_identity_digest for item in matches)
            )
            if descriptor["record_kind"] == "conversation":
                actions.append(_make_action(
                    action_type="link_candidate",
                    record_kind="conversation",
                    source_identity=descriptor["source_identity"],
                    evidence_codes=("logical_conversation_similarity_candidate",),
                    evidence_hashes=evidence_hashes,
                ))
        action_type = {
            "conversation": "insert",
            "message": "append",
            "attachment": "insert",
            "asset": "insert",
        }[descriptor["record_kind"]]
        actions.append(_make_action(
            action_type=action_type,
            record_kind=descriptor["record_kind"],
            source_identity=descriptor["source_identity"],
            evidence_codes=evidence_codes,
            evidence_hashes=evidence_hashes,
        ))

    if source_snapshot_complete:
        current_keys = set(descriptor_groups)
        for key, existing in sorted(existing_by_key.items()):
            if (
                existing.source_type != manifest.source_type
                or existing.record_kind == "asset"
                or key in current_keys
            ):
                continue
            descriptor = {
                "record_kind": existing.record_kind,
                "source_identity": existing.source_identity,
            }
            conflict = _conflict_for(
                batch.batch_id,
                descriptor,
                existing,
                "source_record_missing_tombstone_candidate",
                evidence=(existing.content_hash, existing.structure_hash),
                decisions=("keep_existing", "tombstone_candidate", "manual_review"),
            )
            conflicts.append(conflict)
            actions.append(_make_action(
                action_type="tombstone_candidate",
                record_kind=existing.record_kind,
                source_identity=existing.source_identity,
                production_identity_digest=existing.production_identity_digest,
                conflict_id=conflict.conflict_id,
                evidence_codes=("source_record_missing_tombstone_candidate",),
                evidence_hashes=conflict.evidence_hashes,
                requires_approval=True,
            ))

    actions = sorted(actions, key=lambda item: item.action_id)
    conflicts = sorted(
        {item.conflict_id: item for item in conflicts}.values(),
        key=lambda item: item.conflict_id,
    )
    automatic_decisions = sorted([
        _decision_for_action(
            action,
            policy_version=policy_version,
            decided_at=created_at,
        )
        for action in actions
        if action.action_type != "conflict" and not action.requires_approval
    ], key=lambda item: item.decision_id)
    counts_raw: dict[str, int] = {
        "normalized_records": len(records),
        "actions": len(actions),
        "conflicts": len(conflicts),
        "automatic_decisions": len(automatic_decisions),
        "warnings": sum(
            len(descriptor.get("warning_codes", [])) for descriptor in descriptors
        ),
    }
    for action_type in ACTION_TYPES:
        counts_raw[action_type] = sum(
            action.action_type == action_type for action in actions
        )
    counts = _count_mapping(counts_raw, "invalid_plan_counts")

    production_snapshot_digest = normalized_json_digest({
        "database_fingerprint": production_fingerprint.to_dict(),
        "existing_records": sorted(
            (item.snapshot_descriptor() for item in existing_records),
            key=lambda item: (item["record_kind"], item["source_identity"]),
        ),
    })
    normalized_records_digest = normalized_json_digest({
        "normalization_protocol": normalization_protocol,
        "records": sorted(
            descriptors,
            key=lambda item: (item["record_kind"], item["source_identity"]),
        ),
    })
    asset_inventory_digest = normalized_json_digest({
        "asset_stage_fingerprint": (
            asset_stage_fingerprint.to_dict()
            if asset_stage_fingerprint else None
        ),
        "attachment_descriptors": sorted(
            (
                descriptor
                for descriptor in descriptors
                if descriptor["record_kind"] in {"attachment", "asset"}
            ),
            key=lambda item: (item["record_kind"], item["source_identity"]),
        ),
    })
    prerequisites = (
        "approval_digest_matches_plan",
        "asset_stage_revalidated",
        "database_snapshot_revalidated",
        "single_writer_lease_active",
    )
    approval_required = any(
        action.requires_approval for action in actions
    ) or bool(conflicts)
    payload = {
        "batch_id": batch.batch_id,
        "manifest_id": manifest.manifest_id,
        "archive_sha256": manifest.archive_sha256,
        "adapter_version": manifest.adapter_version,
        "normalization_protocol": normalization_protocol,
        "policy_version": policy_version,
        "production_snapshot_digest": production_snapshot_digest,
        "normalized_records_digest": normalized_records_digest,
        "asset_inventory_digest": asset_inventory_digest,
        "actions": [item.to_dict() for item in actions],
        "conflicts": [item.to_dict() for item in conflicts],
        "automatic_decisions": [
            {
                key: value
                for key, value in item.to_dict().items()
                if key != "decided_at"
            }
            for item in automatic_decisions
        ],
        "counts": counts,
        "approval_required": approval_required,
        "commit_prerequisites": list(prerequisites),
        "source_snapshot_complete": source_snapshot_complete,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
    }
    return ImportPlan(
        plan_id="plan_" + normalized_json_digest(payload),
        batch_id=batch.batch_id,
        manifest_id=manifest.manifest_id,
        archive_sha256=manifest.archive_sha256,
        adapter_version=manifest.adapter_version,
        normalization_protocol=normalization_protocol,
        policy_version=policy_version,
        production_snapshot_digest=production_snapshot_digest,
        normalized_records_digest=normalized_records_digest,
        asset_inventory_digest=asset_inventory_digest,
        actions=tuple(actions),
        conflicts=tuple(conflicts),
        automatic_decisions=tuple(automatic_decisions),
        counts=counts,
        approval_required=approval_required,
        commit_prerequisites=prerequisites,
        source_snapshot_complete=source_snapshot_complete,
        created_at=created_at,
    )


NORMALIZED_MODEL_TYPES = (
    SourceManifest,
    ImportBatch,
    NormalizedConversation,
    NormalizedMessage,
    NormalizedAttachment,
    AssetEntity,
    ImportConflict,
    ImportDecision,
    ImportVerification,
)
