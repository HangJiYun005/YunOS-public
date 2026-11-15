"""Pure YunOS v8.3-A source-manifest and import-batch contracts.

The module deliberately has no production database knowledge.  It scans a
single local source archive read-only, derives privacy-safe identities, models
batch state, and validates immutable event replay.  SQL persistence lives in
``memory.ingestion_ledger`` behind the repository protocols declared here.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
import unicodedata
import uuid
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


MANIFEST_SCHEMA_VERSION = "v8.3-a-source-manifest-1"
BATCH_SCHEMA_VERSION = "v8.3-a-import-batch-1"
EVENT_SCHEMA_VERSION = "v8.3-a-batch-event-1"
DIGEST_PROTOCOL_VERSION = "v8.3-a-canonical-json-nfc-1"
APPROVAL_POLICY_VERSION = "v8.3-a-plan-manifest-fingerprint-binding-1"
DATABASE_FINGERPRINT_VERSION = "v8.3-a-database-fingerprint-1"
ASSET_FINGERPRINT_VERSION = "v8.3-a-asset-stage-fingerprint-1"

DEFAULT_STREAM_CHUNK_SIZE = 1024 * 1024
MAX_COUNTER_VALUE = 1_000_000_000
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 1_000_000
MAX_JSON_BYTES = 64 * 1024 * 1024

_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_ID_RE = re.compile(r"[a-z][a-z0-9_:-]{7,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_BATCH_ID_RE = re.compile(r"batch_[0-9a-f]{32}\Z")
_EVENT_ID_RE = re.compile(r"event_[0-9a-f]{64}\Z")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_NONSEMANTIC_PLAN_FIELDS = frozenset({
    "absolute_path",
    "archive_path",
    "created_at",
    "discovered_at",
    "filename",
    "full_path",
    "mtime",
    "mtime_ns",
    "occurred_at",
    "path",
    "scanned_at",
    "source_path",
    "updated_at",
})
_MANIFEST_FIELDS = frozenset({
    "manifest_id", "source_type", "source_version", "adapter_name",
    "adapter_version", "archive_sha256", "archive_size", "archive_kind",
    "discovered_at", "scanned_at", "record_counts", "asset_counts", "warnings",
    "validation_status", "source_locator_kind", "source_locator_fingerprint",
    "manifest_schema_version",
})
_BATCH_FIELDS = frozenset({
    "batch_id", "manifest_id", "source_type", "adapter_name", "adapter_version",
    "batch_mode", "created_at", "updated_at", "current_state", "plan_digest",
    "approval_digest", "database_before_fingerprint", "database_after_fingerprint",
    "asset_stage_fingerprint", "warning_count", "error_count", "conflict_count",
    "event_sequence", "recovery_status", "batch_schema_version",
})
_EVENT_FIELDS = frozenset({
    "event_id", "batch_id", "sequence", "event_type", "previous_state",
    "next_state", "occurred_at", "actor_kind", "reason_code", "payload_digest",
    "previous_event_digest", "event_digest", "event_schema_version",
})


class IngestionCoreError(ValueError):
    """Fail-closed validation error carrying only a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class BatchState(str, Enum):
    DISCOVERED = "discovered"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    COMMITTING = "committing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


class BatchMode(str, Enum):
    FULL = "full"
    INCREMENTAL = "incremental"
    HISTORICAL_BACKFILL = "historical_backfill"
    VERIFICATION_ONLY = "verification_only"


class ReplayDecisionCode(str, Enum):
    EXACT_DUPLICATE = "exact_duplicate"
    RESCAN_ALLOWED = "rescan_allowed"
    RETRY_ALLOWED = "retry_allowed"
    APPROVAL_REQUIRED = "approval_required"
    RECOVERY_REQUIRED = "recovery_required"
    INCOMPATIBLE_MANIFEST = "incompatible_manifest"
    BLOCKED = "blocked"


LEGAL_TRANSITIONS: Mapping[BatchState, frozenset[BatchState]] = {
    BatchState.DISCOVERED: frozenset({
        BatchState.VALIDATING, BatchState.CANCELLED,
    }),
    BatchState.VALIDATING: frozenset({
        BatchState.VALIDATION_FAILED, BatchState.PLANNED, BatchState.CANCELLED,
    }),
    BatchState.VALIDATION_FAILED: frozenset({
        BatchState.VALIDATING, BatchState.FAILED, BatchState.CANCELLED,
    }),
    BatchState.PLANNED: frozenset({
        BatchState.AWAITING_APPROVAL, BatchState.CANCELLED,
    }),
    BatchState.AWAITING_APPROVAL: frozenset({
        BatchState.APPROVED, BatchState.PLANNED, BatchState.CANCELLED,
    }),
    BatchState.APPROVED: frozenset({
        BatchState.COMMITTING, BatchState.PLANNED, BatchState.CANCELLED,
    }),
    BatchState.COMMITTING: frozenset({
        BatchState.VERIFYING, BatchState.FAILED, BatchState.RECOVERY_REQUIRED,
    }),
    BatchState.VERIFYING: frozenset({
        BatchState.COMPLETED, BatchState.COMPLETED_WITH_WARNINGS,
        BatchState.FAILED, BatchState.RECOVERY_REQUIRED,
    }),
    BatchState.FAILED: frozenset({
        BatchState.VALIDATING, BatchState.ROLLING_BACK, BatchState.CANCELLED,
    }),
    BatchState.RECOVERY_REQUIRED: frozenset({BatchState.ROLLING_BACK}),
    BatchState.ROLLING_BACK: frozenset({
        BatchState.ROLLED_BACK, BatchState.RECOVERY_REQUIRED, BatchState.FAILED,
    }),
    BatchState.COMPLETED: frozenset(),
    BatchState.COMPLETED_WITH_WARNINGS: frozenset(),
    BatchState.ROLLED_BACK: frozenset(),
    BatchState.CANCELLED: frozenset(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IngestionCoreError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise IngestionCoreError("invalid_timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise IngestionCoreError("invalid_timestamp")
    return value


def _token(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise IngestionCoreError(reason_code)
    return value


def _identity(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise IngestionCoreError(reason_code)
    return value


def _sha256(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IngestionCoreError(reason_code)
    return value


def _strict_unicode(value: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise IngestionCoreError("invalid_unicode") from None
    return unicodedata.normalize("NFC", value)


def _normalize_json(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
    excluded_fields: frozenset[str] = frozenset(),
) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise IngestionCoreError("json_depth_limit_exceeded")
    if budget is None:
        budget = [MAX_JSON_NODES]
    budget[0] -= 1
    if budget[0] < 0:
        raise IngestionCoreError("json_record_limit_exceeded")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IngestionCoreError("non_finite_number")
        # JSON's finite shortest round-trip representation is deterministic in
        # supported Python versions.  Negative zero is intentionally preserved.
        return value
    if isinstance(value, str):
        return _strict_unicode(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise IngestionCoreError("json_key_not_string")
            key = _strict_unicode(raw_key)
            if key in excluded_fields:
                continue
            if key in result:
                raise IngestionCoreError("duplicate_json_field")
            result[key] = _normalize_json(
                item,
                depth=depth + 1,
                budget=budget,
                excluded_fields=excluded_fields,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _normalize_json(
                item,
                depth=depth + 1,
                budget=budget,
                excluded_fields=excluded_fields,
            )
            for item in value
        ]
    raise IngestionCoreError("unsupported_json_type")


def canonical_json_bytes(
    value: Any, *, excluded_fields: frozenset[str] = frozenset(),
) -> bytes:
    normalized = _normalize_json(value, excluded_fields=excluded_fields)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise IngestionCoreError("canonical_json_failed") from None


def normalized_json_digest(
    value: Any, *, excluded_fields: frozenset[str] = frozenset(),
) -> str:
    return hashlib.sha256(canonical_json_bytes(value, excluded_fields=excluded_fields)).hexdigest()


def load_json_strict(
    raw: str | bytes,
    *,
    max_bytes: int = MAX_JSON_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
) -> Any:
    if isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise IngestionCoreError("invalid_unicode") from None
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise IngestionCoreError("invalid_json_input")
    if len(encoded) > max_bytes:
        raise IngestionCoreError("json_size_limit_exceeded")
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise IngestionCoreError("invalid_unicode") from None

    def strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for raw_key, item in pairs:
            key = _strict_unicode(raw_key)
            if key in result:
                raise IngestionCoreError("duplicate_json_field")
            result[key] = item
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=strict_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                IngestionCoreError("non_finite_number")
            ),
        )
    except IngestionCoreError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise IngestionCoreError("invalid_json") from None
    budget = [max_nodes]

    def validate(item: Any, depth: int) -> None:
        if depth > max_depth:
            raise IngestionCoreError("json_depth_limit_exceeded")
        budget[0] -= 1
        if budget[0] < 0:
            raise IngestionCoreError("json_record_limit_exceeded")
        if isinstance(item, str):
            _strict_unicode(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise IngestionCoreError("non_finite_number")
        elif isinstance(item, dict):
            for key, child in item.items():
                _strict_unicode(key)
                validate(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise IngestionCoreError("unsupported_json_type")

    validate(value, 0)
    return value


def sanitize_reason_code(value: Any) -> str:
    if isinstance(value, str) and _REASON_RE.fullmatch(value):
        return value
    return "redacted_error_detail"


def make_plan_digest(plan: Mapping[str, Any]) -> str:
    if not isinstance(plan, Mapping):
        raise IngestionCoreError("invalid_plan")
    return normalized_json_digest(
        {
            "digest_protocol_version": DIGEST_PROTOCOL_VERSION,
            "plan": plan,
        },
        excluded_fields=_NONSEMANTIC_PLAN_FIELDS,
    )


@dataclass(frozen=True)
class DatabaseFingerprint:
    content_sha256: str
    size_bytes: int
    schema_digest: str
    logical_digest: str
    fingerprint_version: str = DATABASE_FINGERPRINT_VERSION

    def __post_init__(self) -> None:
        _sha256(self.content_sha256, "invalid_database_content_digest")
        _sha256(self.schema_digest, "invalid_database_schema_digest")
        _sha256(self.logical_digest, "invalid_database_logical_digest")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise IngestionCoreError("invalid_database_size")
        if self.fingerprint_version != DATABASE_FINGERPRINT_VERSION:
            raise IngestionCoreError("invalid_database_fingerprint_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint_version": self.fingerprint_version,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "schema_digest": self.schema_digest,
            "logical_digest": self.logical_digest,
        }


def build_database_fingerprint(
    *,
    content_sha256: str,
    size_bytes: int,
    schema_descriptor: Any,
    logical_descriptor: Any,
) -> DatabaseFingerprint:
    """Build a path-free database fingerprint from reviewed descriptors."""
    return DatabaseFingerprint(
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        schema_digest=normalized_json_digest({
            "descriptor_protocol": DATABASE_FINGERPRINT_VERSION,
            "schema": schema_descriptor,
        }),
        logical_digest=normalized_json_digest({
            "descriptor_protocol": DATABASE_FINGERPRINT_VERSION,
            "logical_state": logical_descriptor,
        }),
    )


@dataclass(frozen=True)
class AssetStageFingerprint:
    item_count: int
    total_size_bytes: int
    content_digest: str
    layout_digest: str
    fingerprint_version: str = ASSET_FINGERPRINT_VERSION

    def __post_init__(self) -> None:
        for value in (self.item_count, self.total_size_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IngestionCoreError("invalid_asset_stage_count")
        _sha256(self.content_digest, "invalid_asset_content_digest")
        _sha256(self.layout_digest, "invalid_asset_layout_digest")
        if self.fingerprint_version != ASSET_FINGERPRINT_VERSION:
            raise IngestionCoreError("invalid_asset_fingerprint_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint_version": self.fingerprint_version,
            "item_count": self.item_count,
            "total_size_bytes": self.total_size_bytes,
            "content_digest": self.content_digest,
            "layout_digest": self.layout_digest,
        }


@dataclass(frozen=True)
class AssetFingerprintItem:
    logical_key_digest: str
    content_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _sha256(self.logical_key_digest, "invalid_asset_logical_key_digest")
        _sha256(self.content_sha256, "invalid_asset_content_digest")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise IngestionCoreError("invalid_asset_size")


def build_asset_stage_fingerprint(
    items: Iterable[AssetFingerprintItem],
) -> AssetStageFingerprint:
    """Aggregate path-free asset metadata deterministically and boundedly."""
    normalized: list[dict[str, Any]] = []
    total_size = 0
    seen_keys: set[str] = set()
    for item in items:
        if len(normalized) >= MAX_COUNTER_VALUE:
            raise IngestionCoreError("asset_record_limit_exceeded")
        if item.logical_key_digest in seen_keys:
            raise IngestionCoreError("duplicate_asset_logical_key")
        seen_keys.add(item.logical_key_digest)
        total_size += item.size_bytes
        if total_size > 2**63 - 1:
            raise IngestionCoreError("asset_total_size_overflow")
        normalized.append({
            "logical_key_digest": item.logical_key_digest,
            "content_sha256": item.content_sha256,
            "size_bytes": item.size_bytes,
        })
    normalized.sort(key=lambda item: item["logical_key_digest"])
    content_rows = sorted(
        (
            {
                "content_sha256": item["content_sha256"],
                "size_bytes": item["size_bytes"],
            }
            for item in normalized
        ),
        key=lambda item: (item["content_sha256"], item["size_bytes"]),
    )
    return AssetStageFingerprint(
        item_count=len(normalized),
        total_size_bytes=total_size,
        content_digest=normalized_json_digest({
            "fingerprint_version": ASSET_FINGERPRINT_VERSION,
            "content_rows": content_rows,
        }),
        layout_digest=normalized_json_digest({
            "fingerprint_version": ASSET_FINGERPRINT_VERSION,
            "layout_rows": normalized,
        }),
    )


def make_approval_digest(
    *,
    manifest_id: str,
    plan_digest: str,
    database_before_fingerprint: DatabaseFingerprint | None,
    asset_stage_fingerprint: AssetStageFingerprint | None,
) -> str:
    _identity(manifest_id, "invalid_manifest_id")
    _sha256(plan_digest, "invalid_plan_digest")
    return normalized_json_digest({
        "approval_policy_version": APPROVAL_POLICY_VERSION,
        "manifest_id": manifest_id,
        "plan_digest": plan_digest,
        "database_before_fingerprint": (
            database_before_fingerprint.to_dict()
            if database_before_fingerprint is not None else None
        ),
        "asset_stage_fingerprint": (
            asset_stage_fingerprint.to_dict()
            if asset_stage_fingerprint is not None else None
        ),
    })


def _counts(value: Mapping[str, int], reason_code: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or len(value) > 128:
        raise IngestionCoreError(reason_code)
    normalized: dict[str, int] = {}
    for raw_key, count in value.items():
        key = _token(raw_key, reason_code)
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= MAX_COUNTER_VALUE
        ):
            raise IngestionCoreError(reason_code)
        normalized[key] = count
    return dict(sorted(normalized.items()))


def _warnings(value: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 256:
        raise IngestionCoreError("invalid_manifest_warnings")
    return tuple(sanitize_reason_code(item) for item in value)


def _manifest_identity_payload(
    *,
    source_type: str,
    source_version: str,
    adapter_name: str,
    adapter_version: str,
    archive_sha256: str,
    archive_size: int,
    archive_kind: str,
    source_locator_kind: str,
    source_locator_fingerprint: str,
    manifest_schema_version: str,
) -> dict[str, Any]:
    return {
        "identity_protocol": MANIFEST_SCHEMA_VERSION,
        "source_type": source_type,
        "source_version": source_version,
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "archive_sha256": archive_sha256,
        "archive_size": archive_size,
        "archive_kind": archive_kind,
        "source_locator_kind": source_locator_kind,
        "source_locator_fingerprint": source_locator_fingerprint,
        "manifest_schema_version": manifest_schema_version,
    }


@dataclass(frozen=True)
class SourceManifest:
    manifest_id: str
    source_type: str
    source_version: str
    adapter_name: str
    adapter_version: str
    archive_sha256: str
    archive_size: int
    archive_kind: str
    discovered_at: str
    scanned_at: str
    record_counts: Mapping[str, int]
    asset_counts: Mapping[str, int]
    warnings: tuple[str, ...]
    validation_status: str
    source_locator_kind: str
    source_locator_fingerprint: str
    manifest_schema_version: str = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.manifest_id, "invalid_manifest_id")
        source_type = _token(self.source_type, "invalid_source_type")
        source_version = _token(self.source_version, "invalid_source_version")
        adapter_name = _token(self.adapter_name, "invalid_adapter_name")
        adapter_version = _token(self.adapter_version, "invalid_adapter_version")
        archive_kind = _token(self.archive_kind, "invalid_archive_kind")
        locator_kind = _token(self.source_locator_kind, "invalid_locator_kind")
        _sha256(self.archive_sha256, "invalid_archive_digest")
        _sha256(self.source_locator_fingerprint, "invalid_locator_fingerprint")
        if (
            isinstance(self.archive_size, bool)
            or not isinstance(self.archive_size, int)
            or self.archive_size < 0
        ):
            raise IngestionCoreError("invalid_archive_size")
        _canonical_timestamp(self.discovered_at)
        _canonical_timestamp(self.scanned_at)
        record_counts = _counts(self.record_counts, "invalid_record_counts")
        asset_counts = _counts(self.asset_counts, "invalid_asset_counts")
        warnings = _warnings(self.warnings)
        validation_status = _token(self.validation_status, "invalid_validation_status")
        _token(self.manifest_schema_version, "invalid_manifest_schema_version")
        payload = _manifest_identity_payload(
            source_type=source_type,
            source_version=source_version,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            archive_sha256=self.archive_sha256,
            archive_size=self.archive_size,
            archive_kind=archive_kind,
            source_locator_kind=locator_kind,
            source_locator_fingerprint=self.source_locator_fingerprint,
            manifest_schema_version=self.manifest_schema_version,
        )
        expected = "manifest_" + normalized_json_digest(payload)
        if self.manifest_id != expected:
            raise IngestionCoreError("manifest_identity_mismatch")
        object.__setattr__(self, "record_counts", MappingProxyType(record_counts))
        object.__setattr__(self, "asset_counts", MappingProxyType(asset_counts))
        object.__setattr__(self, "warnings", warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "source_type": self.source_type,
            "source_version": self.source_version,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "archive_sha256": self.archive_sha256,
            "archive_size": self.archive_size,
            "archive_kind": self.archive_kind,
            "discovered_at": self.discovered_at,
            "scanned_at": self.scanned_at,
            "record_counts": dict(self.record_counts),
            "asset_counts": dict(self.asset_counts),
            "warnings": list(self.warnings),
            "validation_status": self.validation_status,
            "source_locator_kind": self.source_locator_kind,
            "source_locator_fingerprint": self.source_locator_fingerprint,
            "manifest_schema_version": self.manifest_schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceManifest":
        if not isinstance(value, Mapping) or set(value) != _MANIFEST_FIELDS:
            raise IngestionCoreError("invalid_manifest_record")
        try:
            return cls(
                manifest_id=value["manifest_id"],
                source_type=value["source_type"],
                source_version=value["source_version"],
                adapter_name=value["adapter_name"],
                adapter_version=value["adapter_version"],
                archive_sha256=value["archive_sha256"],
                archive_size=value["archive_size"],
                archive_kind=value["archive_kind"],
                discovered_at=value["discovered_at"],
                scanned_at=value["scanned_at"],
                record_counts=value["record_counts"],
                asset_counts=value["asset_counts"],
                warnings=tuple(value["warnings"]),
                validation_status=value["validation_status"],
                source_locator_kind=value["source_locator_kind"],
                source_locator_fingerprint=value["source_locator_fingerprint"],
                manifest_schema_version=value["manifest_schema_version"],
            )
        except (KeyError, TypeError):
            raise IngestionCoreError("invalid_manifest_record") from None


def build_source_manifest(
    *,
    source_type: str,
    source_version: str,
    adapter_name: str,
    adapter_version: str,
    archive_sha256: str,
    archive_size: int,
    archive_kind: str,
    discovered_at: str,
    scanned_at: str,
    record_counts: Mapping[str, int] | None = None,
    asset_counts: Mapping[str, int] | None = None,
    warnings: Sequence[str] = (),
    validation_status: str = "scanned",
    source_locator_kind: str = "local_file",
    manifest_schema_version: str = MANIFEST_SCHEMA_VERSION,
) -> SourceManifest:
    _token(source_type, "invalid_source_type")
    _token(source_version, "invalid_source_version")
    _token(adapter_name, "invalid_adapter_name")
    _token(adapter_version, "invalid_adapter_version")
    _sha256(archive_sha256, "invalid_archive_digest")
    locator_fingerprint = normalized_json_digest({
        "locator_protocol": "content-addressed-source-locator-1",
        "source_locator_kind": source_locator_kind,
        "archive_sha256": archive_sha256,
    })
    payload = _manifest_identity_payload(
        source_type=source_type,
        source_version=source_version,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        archive_kind=archive_kind,
        source_locator_kind=source_locator_kind,
        source_locator_fingerprint=locator_fingerprint,
        manifest_schema_version=manifest_schema_version,
    )
    manifest_id = "manifest_" + normalized_json_digest(payload)
    return SourceManifest(
        manifest_id=manifest_id,
        source_type=source_type,
        source_version=source_version,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        archive_kind=archive_kind,
        discovered_at=discovered_at,
        scanned_at=scanned_at,
        record_counts=record_counts or {},
        asset_counts=asset_counts or {},
        warnings=tuple(warnings),
        validation_status=validation_status,
        source_locator_kind=source_locator_kind,
        source_locator_fingerprint=locator_fingerprint,
        manifest_schema_version=manifest_schema_version,
    )


def _reject_unsafe_source_path(path: Path) -> None:
    raw = str(path)
    normalized = raw.replace("/", "\\")
    if (
        normalized.startswith("\\\\")
        or normalized.startswith("\\\\?\\")
        or normalized.startswith("\\\\.\\")
    ):
        raise IngestionCoreError("unsafe_source_path")
    if any(part == ".." for part in path.parts):
        raise IngestionCoreError("path_traversal")


def _stable_file_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_dev", 0),
        getattr(info, "st_ino", 0),
    )


def _open_source_readonly(source: Path):
    if os.name != "nt":
        return source.open("rb", buffering=0)
    # Python's normal Windows file open allows other writers.  Open the archive
    # through CreateFileW with FILE_SHARE_READ only, so a concurrent writer or
    # replacement is denied for the duration of the scan.
    import ctypes
    import msvcrt

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(source.resolve(strict=True)),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ (no write/delete sharing)
        None,
        3,  # OPEN_EXISTING
        0x08000000,  # FILE_FLAG_SEQUENTIAL_SCAN
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb", buffering=0)


def stream_file_sha256(
    path: str | os.PathLike[str],
    *,
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[str, int]:
    source = Path(path)
    _reject_unsafe_source_path(source)
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 4096:
        raise IngestionCoreError("invalid_stream_chunk_size")
    try:
        before = source.lstat()
    except OSError:
        raise IngestionCoreError("source_unavailable") from None
    file_attributes = getattr(before, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(before.st_mode)
        or file_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise IngestionCoreError("source_link_not_allowed")
    if not stat.S_ISREG(before.st_mode):
        raise IngestionCoreError("source_not_regular_file")
    if cancel_check is not None and cancel_check():
        raise IngestionCoreError("scan_cancelled")

    digest = hashlib.sha256()
    total = 0
    try:
        with _open_source_readonly(source) as handle:
            handle_info = os.fstat(handle.fileno())
            if _stable_file_signature(handle_info) != _stable_file_signature(before):
                raise IngestionCoreError("source_changed_during_scan")
            while True:
                if cancel_check is not None and cancel_check():
                    raise IngestionCoreError("scan_cancelled")
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
                if progress_callback is not None:
                    try:
                        progress_callback(total, before.st_size)
                    except OSError:
                        raise IngestionCoreError("source_changed_during_scan") from None
            handle_after = os.fstat(handle.fileno())
    except IngestionCoreError:
        raise
    except OSError:
        raise IngestionCoreError("source_read_failed") from None
    try:
        after = source.lstat()
    except OSError:
        raise IngestionCoreError("source_changed_during_scan") from None
    signatures = {
        _stable_file_signature(before),
        _stable_file_signature(handle_info),
        _stable_file_signature(handle_after),
        _stable_file_signature(after),
    }
    lstat_ctime_changed = (
        getattr(before, "st_ctime_ns", 0) != getattr(after, "st_ctime_ns", 0)
    )
    if len(signatures) != 1 or lstat_ctime_changed or total != before.st_size:
        raise IngestionCoreError("source_changed_during_scan")
    if cancel_check is not None and cancel_check():
        raise IngestionCoreError("scan_cancelled")
    return digest.hexdigest(), total


def scan_source_archive(
    path: str | os.PathLike[str],
    *,
    source_type: str,
    source_version: str,
    adapter_name: str,
    adapter_version: str,
    archive_kind: str,
    discovered_at: str | None = None,
    scanned_at: str | None = None,
    record_counts: Mapping[str, int] | None = None,
    asset_counts: Mapping[str, int] | None = None,
    warnings: Sequence[str] = (),
    validation_status: str = "scanned",
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
) -> SourceManifest:
    digest, size = stream_file_sha256(
        path,
        chunk_size=chunk_size,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    timestamp = utc_now()
    return build_source_manifest(
        source_type=source_type,
        source_version=source_version,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        archive_sha256=digest,
        archive_size=size,
        archive_kind=archive_kind,
        discovered_at=discovered_at or timestamp,
        scanned_at=scanned_at or timestamp,
        record_counts=record_counts,
        asset_counts=asset_counts,
        warnings=warnings,
        validation_status=validation_status,
    )


@dataclass(frozen=True)
class ArchiveEntryMetadata:
    member_name: str
    compressed_size: int
    uncompressed_size: int
    is_link: bool = False


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 100_000
    max_member_size: int = 2 * 1024 * 1024 * 1024
    max_total_uncompressed_size: int = 20 * 1024 * 1024 * 1024
    max_compression_ratio: int = 1000
    max_path_depth: int = 32


@dataclass(frozen=True)
class ArchiveMetadataSummary:
    entry_count: int
    total_compressed_size: int
    total_uncompressed_size: int


def validate_archive_metadata(
    entries: Iterable[ArchiveEntryMetadata],
    limits: ArchiveLimits = ArchiveLimits(),
) -> ArchiveMetadataSummary:
    count = 0
    compressed_total = 0
    uncompressed_total = 0
    for entry in entries:
        count += 1
        if count > limits.max_entries:
            raise IngestionCoreError("archive_entry_limit_exceeded")
        if not isinstance(entry.member_name, str):
            raise IngestionCoreError("unsafe_archive_member")
        name = _strict_unicode(entry.member_name).replace("\\", "/")
        pure = PurePosixPath(name)
        if (
            not name
            or name.startswith("/")
            or _WINDOWS_DRIVE_RE.match(name)
            or any(part in {"", ".", ".."} for part in pure.parts)
            or len(pure.parts) > limits.max_path_depth
            or entry.is_link
        ):
            raise IngestionCoreError("unsafe_archive_member")
        for value in (entry.compressed_size, entry.uncompressed_size):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IngestionCoreError("invalid_archive_metadata")
        if entry.uncompressed_size > limits.max_member_size:
            raise IngestionCoreError("archive_member_size_limit_exceeded")
        if (
            entry.uncompressed_size > 0
            and entry.uncompressed_size > max(1, entry.compressed_size) * limits.max_compression_ratio
        ):
            raise IngestionCoreError("archive_compression_ratio_exceeded")
        compressed_total += entry.compressed_size
        uncompressed_total += entry.uncompressed_size
        if uncompressed_total > limits.max_total_uncompressed_size:
            raise IngestionCoreError("archive_total_size_limit_exceeded")
    return ArchiveMetadataSummary(count, compressed_total, uncompressed_total)


@dataclass(frozen=True)
class ImportBatch:
    batch_id: str
    manifest_id: str
    source_type: str
    adapter_name: str
    adapter_version: str
    batch_mode: str
    created_at: str
    updated_at: str
    current_state: str
    plan_digest: str
    approval_digest: str
    database_before_fingerprint: DatabaseFingerprint | None
    database_after_fingerprint: DatabaseFingerprint | None
    asset_stage_fingerprint: AssetStageFingerprint | None
    warning_count: int
    error_count: int
    conflict_count: int
    event_sequence: int
    recovery_status: str
    batch_schema_version: str = BATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if _BATCH_ID_RE.fullmatch(self.batch_id) is None:
            raise IngestionCoreError("invalid_batch_id")
        _identity(self.manifest_id, "invalid_manifest_id")
        _token(self.source_type, "invalid_source_type")
        _token(self.adapter_name, "invalid_adapter_name")
        _token(self.adapter_version, "invalid_adapter_version")
        try:
            BatchMode(self.batch_mode)
        except ValueError:
            raise IngestionCoreError("invalid_batch_mode") from None
        _canonical_timestamp(self.created_at)
        _canonical_timestamp(self.updated_at)
        try:
            BatchState(self.current_state)
        except ValueError:
            raise IngestionCoreError("invalid_batch_state") from None
        _sha256(self.plan_digest, "invalid_plan_digest")
        if self.approval_digest:
            _sha256(self.approval_digest, "invalid_approval_digest")
        if (
            self.database_before_fingerprint is not None
            and not isinstance(self.database_before_fingerprint, DatabaseFingerprint)
        ) or (
            self.database_after_fingerprint is not None
            and not isinstance(self.database_after_fingerprint, DatabaseFingerprint)
        ) or (
            self.asset_stage_fingerprint is not None
            and not isinstance(self.asset_stage_fingerprint, AssetStageFingerprint)
        ):
            raise IngestionCoreError("invalid_batch_fingerprint")
        for value in (
            self.warning_count,
            self.error_count,
            self.conflict_count,
            self.event_sequence,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IngestionCoreError("invalid_batch_count")
        if self.recovery_status not in {
            "not_required", "required", "rolling_back", "rolled_back",
        }:
            raise IngestionCoreError("invalid_recovery_status")
        if self.batch_schema_version != BATCH_SCHEMA_VERSION:
            raise IngestionCoreError("invalid_batch_schema_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "manifest_id": self.manifest_id,
            "source_type": self.source_type,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "batch_mode": self.batch_mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_state": self.current_state,
            "plan_digest": self.plan_digest,
            "approval_digest": self.approval_digest,
            "database_before_fingerprint": (
                self.database_before_fingerprint.to_dict()
                if self.database_before_fingerprint else None
            ),
            "database_after_fingerprint": (
                self.database_after_fingerprint.to_dict()
                if self.database_after_fingerprint else None
            ),
            "asset_stage_fingerprint": (
                self.asset_stage_fingerprint.to_dict()
                if self.asset_stage_fingerprint else None
            ),
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "conflict_count": self.conflict_count,
            "event_sequence": self.event_sequence,
            "recovery_status": self.recovery_status,
            "batch_schema_version": self.batch_schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ImportBatch":
        if not isinstance(value, Mapping) or set(value) != _BATCH_FIELDS:
            raise IngestionCoreError("invalid_batch_record")
        def database(raw: Any) -> DatabaseFingerprint | None:
            return DatabaseFingerprint(**raw) if raw is not None else None

        def assets(raw: Any) -> AssetStageFingerprint | None:
            return AssetStageFingerprint(**raw) if raw is not None else None

        try:
            return cls(
                batch_id=value["batch_id"],
                manifest_id=value["manifest_id"],
                source_type=value["source_type"],
                adapter_name=value["adapter_name"],
                adapter_version=value["adapter_version"],
                batch_mode=value["batch_mode"],
                created_at=value["created_at"],
                updated_at=value["updated_at"],
                current_state=value["current_state"],
                plan_digest=value["plan_digest"],
                approval_digest=value["approval_digest"],
                database_before_fingerprint=database(value["database_before_fingerprint"]),
                database_after_fingerprint=database(value["database_after_fingerprint"]),
                asset_stage_fingerprint=assets(value["asset_stage_fingerprint"]),
                warning_count=value["warning_count"],
                error_count=value["error_count"],
                conflict_count=value["conflict_count"],
                event_sequence=value["event_sequence"],
                recovery_status=value["recovery_status"],
                batch_schema_version=value["batch_schema_version"],
            )
        except (KeyError, TypeError):
            raise IngestionCoreError("invalid_batch_record") from None


def create_import_batch(
    manifest: SourceManifest,
    *,
    batch_mode: str,
    plan: Mapping[str, Any],
    database_before_fingerprint: DatabaseFingerprint | None = None,
    asset_stage_fingerprint: AssetStageFingerprint | None = None,
    timestamp: str | None = None,
    batch_id_factory: Callable[[], str] | None = None,
) -> ImportBatch:
    created_at = timestamp or utc_now()
    plan_value = make_plan_digest(plan)
    identifier = (
        batch_id_factory()
        if batch_id_factory is not None
        else "batch_" + uuid.uuid4().hex
    )
    return ImportBatch(
        batch_id=identifier,
        manifest_id=manifest.manifest_id,
        source_type=manifest.source_type,
        adapter_name=manifest.adapter_name,
        adapter_version=manifest.adapter_version,
        batch_mode=batch_mode,
        created_at=created_at,
        updated_at=created_at,
        current_state=BatchState.DISCOVERED.value,
        plan_digest=plan_value,
        approval_digest="",
        database_before_fingerprint=database_before_fingerprint,
        database_after_fingerprint=None,
        asset_stage_fingerprint=asset_stage_fingerprint,
        warning_count=0,
        error_count=0,
        conflict_count=0,
        event_sequence=0,
        recovery_status="not_required",
    )


def bind_batch_approval(batch: ImportBatch, *, timestamp: str | None = None) -> ImportBatch:
    if BatchState(batch.current_state) is not BatchState.AWAITING_APPROVAL:
        raise IngestionCoreError("approval_not_awaiting")
    digest = make_approval_digest(
        manifest_id=batch.manifest_id,
        plan_digest=batch.plan_digest,
        database_before_fingerprint=batch.database_before_fingerprint,
        asset_stage_fingerprint=batch.asset_stage_fingerprint,
    )
    return replace(batch, approval_digest=digest, updated_at=timestamp or utc_now())


@dataclass(frozen=True)
class BatchEvent:
    event_id: str
    batch_id: str
    sequence: int
    event_type: str
    previous_state: str
    next_state: str
    occurred_at: str
    actor_kind: str
    reason_code: str
    payload_digest: str
    previous_event_digest: str
    event_digest: str
    event_schema_version: str = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise IngestionCoreError("invalid_event_id")
        if _BATCH_ID_RE.fullmatch(self.batch_id) is None:
            raise IngestionCoreError("invalid_batch_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise IngestionCoreError("invalid_event_sequence")
        _token(self.event_type, "invalid_event_type")
        try:
            BatchState(self.previous_state)
            BatchState(self.next_state)
        except ValueError:
            raise IngestionCoreError("invalid_event_state") from None
        _canonical_timestamp(self.occurred_at)
        _token(self.actor_kind, "invalid_actor_kind")
        if sanitize_reason_code(self.reason_code) != self.reason_code:
            raise IngestionCoreError("invalid_reason_code")
        _sha256(self.payload_digest, "invalid_payload_digest")
        if self.previous_event_digest:
            _sha256(self.previous_event_digest, "invalid_previous_event_digest")
        _sha256(self.event_digest, "invalid_event_digest")
        if self.event_schema_version != EVENT_SCHEMA_VERSION:
            raise IngestionCoreError("invalid_event_schema_version")
        expected_digest = normalized_json_digest(self.digest_payload())
        if self.event_digest != expected_digest:
            raise IngestionCoreError("event_digest_mismatch")
        if self.event_id != "event_" + expected_digest:
            raise IngestionCoreError("event_identity_mismatch")

    def digest_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "previous_state": self.previous_state,
            "next_state": self.next_state,
            "occurred_at": self.occurred_at,
            "actor_kind": self.actor_kind,
            "reason_code": self.reason_code,
            "payload_digest": self.payload_digest,
            "previous_event_digest": self.previous_event_digest,
            "event_schema_version": self.event_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            **self.digest_payload(),
            "event_digest": self.event_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BatchEvent":
        if not isinstance(value, Mapping) or set(value) != _EVENT_FIELDS:
            raise IngestionCoreError("invalid_event_record")
        try:
            return cls(
                event_id=value["event_id"],
                batch_id=value["batch_id"],
                sequence=value["sequence"],
                event_type=value["event_type"],
                previous_state=value["previous_state"],
                next_state=value["next_state"],
                occurred_at=value["occurred_at"],
                actor_kind=value["actor_kind"],
                reason_code=value["reason_code"],
                payload_digest=value["payload_digest"],
                previous_event_digest=value["previous_event_digest"],
                event_digest=value["event_digest"],
                event_schema_version=value["event_schema_version"],
            )
        except (KeyError, TypeError):
            raise IngestionCoreError("invalid_event_record") from None


def transition_batch(
    batch: ImportBatch,
    next_state: BatchState | str,
    *,
    actor_kind: str,
    reason_code: str,
    payload: Any = None,
    occurred_at: str | None = None,
    previous_event_digest: str = "",
    supplied_approval_digest: str = "",
    current_manifest_id: str | None = None,
) -> tuple[ImportBatch, BatchEvent]:
    previous = BatchState(batch.current_state)
    try:
        target = next_state if isinstance(next_state, BatchState) else BatchState(next_state)
    except ValueError:
        raise IngestionCoreError("invalid_batch_state") from None
    if target not in LEGAL_TRANSITIONS[previous]:
        raise IngestionCoreError("illegal_state_transition")
    if batch.event_sequence > 0:
        _sha256(previous_event_digest, "missing_previous_event_digest")
    elif previous_event_digest:
        raise IngestionCoreError("unexpected_previous_event_digest")
    if target is BatchState.COMMITTING:
        expected_approval = make_approval_digest(
            manifest_id=batch.manifest_id,
            plan_digest=batch.plan_digest,
            database_before_fingerprint=batch.database_before_fingerprint,
            asset_stage_fingerprint=batch.asset_stage_fingerprint,
        )
        if (
            not batch.approval_digest
            or batch.approval_digest != expected_approval
            or supplied_approval_digest != expected_approval
        ):
            raise IngestionCoreError("approval_digest_mismatch")
        if current_manifest_id != batch.manifest_id:
            raise IngestionCoreError("manifest_approval_binding_mismatch")
    if target is BatchState.COMPLETED_WITH_WARNINGS and batch.warning_count < 1:
        raise IngestionCoreError("completed_warning_count_missing")
    timestamp = occurred_at or utc_now()
    sequence = batch.event_sequence + 1
    payload_digest = normalized_json_digest({
        "payload_protocol": EVENT_SCHEMA_VERSION,
        "payload": payload,
    })
    event_type = f"state_{previous.value}_to_{target.value}"
    safe_reason = sanitize_reason_code(reason_code)
    digest_payload = {
        "batch_id": batch.batch_id,
        "sequence": sequence,
        "event_type": event_type,
        "previous_state": previous.value,
        "next_state": target.value,
        "occurred_at": timestamp,
        "actor_kind": _token(actor_kind, "invalid_actor_kind"),
        "reason_code": safe_reason,
        "payload_digest": payload_digest,
        "previous_event_digest": previous_event_digest,
        "event_schema_version": EVENT_SCHEMA_VERSION,
    }
    event_digest = normalized_json_digest(digest_payload)
    event = BatchEvent(
        event_id="event_" + event_digest,
        event_digest=event_digest,
        **digest_payload,
    )
    recovery_status = batch.recovery_status
    if target is BatchState.RECOVERY_REQUIRED:
        recovery_status = "required"
    elif target is BatchState.ROLLING_BACK:
        recovery_status = "rolling_back"
    elif target is BatchState.ROLLED_BACK:
        recovery_status = "rolled_back"
    updated = replace(
        batch,
        current_state=target.value,
        updated_at=timestamp,
        event_sequence=sequence,
        recovery_status=recovery_status,
    )
    return updated, event


@dataclass(frozen=True)
class EventReplayResult:
    batch_id: str
    current_state: str
    event_sequence: int
    last_event_digest: str


def replay_batch_events(
    batch_id: str,
    events: Sequence[BatchEvent],
) -> EventReplayResult:
    if _BATCH_ID_RE.fullmatch(batch_id) is None:
        raise IngestionCoreError("invalid_batch_id")
    current = BatchState.DISCOVERED
    last_digest = ""
    for expected_sequence, event in enumerate(events, start=1):
        # Reconstructing validates event and digest even if the caller mutated
        # an object through unsafe means or loaded it from untrusted storage.
        event = BatchEvent.from_dict(event.to_dict())
        if event.batch_id != batch_id:
            raise IngestionCoreError("event_batch_mismatch")
        if event.sequence != expected_sequence:
            raise IngestionCoreError("event_sequence_gap")
        if event.previous_event_digest != last_digest:
            raise IngestionCoreError("event_chain_mismatch")
        if event.previous_state != current.value:
            raise IngestionCoreError("event_snapshot_mismatch")
        next_state = BatchState(event.next_state)
        if next_state not in LEGAL_TRANSITIONS[current]:
            raise IngestionCoreError("illegal_state_transition")
        current = next_state
        last_digest = event.event_digest
    return EventReplayResult(batch_id, current.value, len(events), last_digest)


def verify_batch_snapshot(batch: ImportBatch, events: Sequence[BatchEvent]) -> None:
    replay = replay_batch_events(batch.batch_id, events)
    if (
        replay.current_state != batch.current_state
        or replay.event_sequence != batch.event_sequence
    ):
        raise IngestionCoreError("batch_snapshot_mismatch")


@dataclass(frozen=True)
class ReplayDecision:
    code: ReplayDecisionCode
    reason_code: str


def decide_source_replay(
    candidate: SourceManifest,
    existing_manifests: Sequence[SourceManifest],
    existing_batches: Sequence[ImportBatch],
) -> ReplayDecision:
    exact = [item for item in existing_manifests if item.manifest_id == candidate.manifest_id]
    same_archive = [
        item for item in existing_manifests
        if item.archive_sha256 == candidate.archive_sha256
    ]
    if not exact and same_archive:
        return ReplayDecision(
            ReplayDecisionCode.INCOMPATIBLE_MANIFEST,
            "archive_seen_with_incompatible_manifest",
        )
    if not exact:
        return ReplayDecision(ReplayDecisionCode.RESCAN_ALLOWED, "new_archive_content")
    matching_batches = [
        batch for batch in existing_batches
        if batch.manifest_id == candidate.manifest_id
    ]
    states = {BatchState(batch.current_state) for batch in matching_batches}
    if states & {
        BatchState.COMMITTING,
        BatchState.VERIFYING,
        BatchState.RECOVERY_REQUIRED,
        BatchState.ROLLING_BACK,
    }:
        return ReplayDecision(ReplayDecisionCode.RECOVERY_REQUIRED, "unfinished_commit_exists")
    if states & {BatchState.COMPLETED, BatchState.COMPLETED_WITH_WARNINGS}:
        return ReplayDecision(ReplayDecisionCode.EXACT_DUPLICATE, "completed_batch_exists")
    if states & {
        BatchState.PLANNED, BatchState.AWAITING_APPROVAL, BatchState.APPROVED,
    }:
        return ReplayDecision(ReplayDecisionCode.APPROVAL_REQUIRED, "existing_plan_requires_review")
    if states & {BatchState.FAILED, BatchState.VALIDATION_FAILED}:
        return ReplayDecision(ReplayDecisionCode.RETRY_ALLOWED, "prior_attempt_failed")
    if states & {BatchState.DISCOVERED, BatchState.VALIDATING}:
        return ReplayDecision(ReplayDecisionCode.BLOCKED, "scan_or_validation_in_progress")
    return ReplayDecision(ReplayDecisionCode.RESCAN_ALLOWED, "no_active_or_completed_batch")


class ManifestRepository(Protocol):
    def add_manifest(self, manifest: SourceManifest) -> None: ...
    def get_manifest(self, manifest_id: str) -> SourceManifest | None: ...
    def find_manifests_by_archive_sha256(self, archive_sha256: str) -> list[SourceManifest]: ...


class BatchRepository(Protocol):
    def add_batch(self, batch: ImportBatch) -> None: ...
    def get_batch(self, batch_id: str) -> ImportBatch | None: ...
    def list_batches_for_manifest(self, manifest_id: str) -> list[ImportBatch]: ...


class EventRepository(Protocol):
    def append_transition(self, batch: ImportBatch, event: BatchEvent) -> None: ...
    def list_events(self, batch_id: str) -> list[BatchEvent]: ...
