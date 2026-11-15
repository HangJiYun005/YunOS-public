"""Deterministic, path-free asset staging plans for YunOS v8.3-D.

This module is deliberately planning-only.  It binds the normalized attachment
and asset models from v8.3-B to an immutable staging plan, but it does not open
archives, create directories, write ledgers, or promote bytes into production.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
import re
from typing import Any, Mapping, Sequence

from memory.ingestion_core import (
    AssetFingerprintItem,
    AssetStageFingerprint,
    build_asset_stage_fingerprint,
    make_plan_digest,
    normalized_json_digest,
)
from memory.ingestion_planning import (
    AssetEntity,
    ImportPlan,
    NormalizedAttachment,
)


ASSET_STAGE_PLAN_SCHEMA_VERSION = "v8.3-d-asset-stage-plan-1"
ASSET_STAGE_OPERATION_SCHEMA_VERSION = "v8.3-d-asset-stage-operation-1"
ASSET_OCCURRENCE_SCHEMA_VERSION = "v8.3-d-asset-occurrence-1"
ASSET_ENTITY_DESCRIPTOR_SCHEMA_VERSION = "v8.3-d-asset-entity-1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,191}\Z")
_MIME_RE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\Z"
)
_BATCH_ID_RE = re.compile(r"batch_[0-9a-f]{32}\Z")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


class AssetPlanningError(RuntimeError):
    """A fail-closed, sanitized asset planning error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _sha256(value: str, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AssetPlanningError(reason)
    return value


def _identity(value: str, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise AssetPlanningError(reason)
    return value


def _batch_id(value: str) -> str:
    if not isinstance(value, str) or _BATCH_ID_RE.fullmatch(value) is None:
        raise AssetPlanningError("invalid_batch_id")
    return value


def _mime(value: str, reason: str) -> str:
    if not isinstance(value, str):
        raise AssetPlanningError(reason)
    normalized = value.strip().lower()
    if _MIME_RE.fullmatch(normalized) is None:
        raise AssetPlanningError(reason)
    return normalized


def _count(value: int, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AssetPlanningError(reason)
    return value


def _reason_codes(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or _REASON_RE.fullmatch(value) is None:
            raise AssetPlanningError("invalid_warning_code")
        result.append(value)
    return tuple(sorted(set(result)))


@dataclass(frozen=True)
class AssetStageLimits:
    """Resource limits enforced both while planning and while streaming."""

    max_entity_bytes: int = 512 * 1024 * 1024
    max_batch_physical_bytes: int = 2 * 1024 * 1024 * 1024
    max_batch_logical_bytes: int = 4 * 1024 * 1024 * 1024
    max_entities: int = 10_000
    max_occurrences: int = 100_000
    max_compression_ratio: int = 200
    max_zip_entries: int = 100_000
    max_concurrent_reads: int = 1
    max_operation_seconds: float = 300.0
    min_free_disk_bytes: int = 64 * 1024 * 1024
    chunk_size: int = 1024 * 1024

    def __post_init__(self) -> None:
        integer_fields = (
            "max_entity_bytes",
            "max_batch_physical_bytes",
            "max_batch_logical_bytes",
            "max_entities",
            "max_occurrences",
            "max_compression_ratio",
            "max_zip_entries",
            "max_concurrent_reads",
            "min_free_disk_bytes",
            "chunk_size",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise AssetPlanningError("invalid_asset_stage_limit")
        if (
            isinstance(self.max_operation_seconds, bool)
            or not isinstance(self.max_operation_seconds, (int, float))
            or self.max_operation_seconds <= 0
        ):
            raise AssetPlanningError("invalid_asset_stage_limit")


@dataclass(frozen=True)
class AssetOccurrence:
    """A relationship occurrence; it never owns or names physical bytes."""

    occurrence_id: str
    normalized_attachment_id: str
    source_type: str
    source_attachment_identity_digest: str
    source_message_identity_digest: str
    entity_digest: str | None
    entity_state: str
    declared_size: int | None
    actual_size: int | None
    declared_mime: str | None
    detected_mime: str | None
    locator_kind: str
    locator_fingerprint: str
    warning_codes: tuple[str, ...] = ()
    occurrence_schema_version: str = ASSET_OCCURRENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.occurrence_id, "invalid_occurrence_id")
        _identity(self.normalized_attachment_id, "invalid_attachment_id")
        _identity(self.source_type, "invalid_source_type")
        _sha256(
            self.source_attachment_identity_digest,
            "invalid_source_attachment_identity_digest",
        )
        _sha256(
            self.source_message_identity_digest,
            "invalid_source_message_identity_digest",
        )
        if self.entity_digest is not None:
            _sha256(self.entity_digest, "invalid_entity_digest")
        if self.entity_state not in {
            "entity_available",
            "metadata_only",
            "entity_missing",
            "entity_blocked",
            "entity_invalid",
        }:
            raise AssetPlanningError("invalid_entity_state")
        if self.entity_state == "entity_available" and self.entity_digest is None:
            raise AssetPlanningError("available_occurrence_digest_missing")
        if self.entity_state != "entity_available" and self.entity_digest is not None:
            raise AssetPlanningError("unavailable_occurrence_has_digest")
        for value, reason in (
            (self.declared_size, "invalid_declared_size"),
            (self.actual_size, "invalid_actual_size"),
        ):
            if value is not None:
                _count(value, reason)
        if self.declared_mime is not None:
            object.__setattr__(
                self, "declared_mime", _mime(self.declared_mime, "invalid_declared_mime")
            )
        if self.detected_mime is not None:
            object.__setattr__(
                self, "detected_mime", _mime(self.detected_mime, "invalid_detected_mime")
            )
        _identity(self.locator_kind, "invalid_locator_kind")
        _sha256(self.locator_fingerprint, "invalid_locator_fingerprint")
        object.__setattr__(self, "warning_codes", _reason_codes(self.warning_codes))
        if self.occurrence_schema_version != ASSET_OCCURRENCE_SCHEMA_VERSION:
            raise AssetPlanningError("occurrence_schema_version_mismatch")

    def binding_descriptor(self) -> dict[str, Any]:
        """Return only path-free relationship material used by plan identity."""
        return {
            "occurrence_id": self.occurrence_id,
            "normalized_attachment_id": self.normalized_attachment_id,
            "source_type": self.source_type,
            "source_attachment_identity_digest": (
                self.source_attachment_identity_digest
            ),
            "source_message_identity_digest": self.source_message_identity_digest,
            "entity_digest": self.entity_digest,
            "entity_state": self.entity_state,
            "declared_size": self.declared_size,
            "actual_size": self.actual_size,
            "declared_mime": self.declared_mime,
            "detected_mime": self.detected_mime,
            "warning_codes": list(self.warning_codes),
            "occurrence_schema_version": self.occurrence_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.binding_descriptor(),
            "locator_kind": self.locator_kind,
            "locator_fingerprint": self.locator_fingerprint,
        }


def occurrence_from_normalized_attachment(
    attachment: NormalizedAttachment,
) -> AssetOccurrence:
    """Convert one normalized logical attachment into one generic occurrence."""
    entity_digest = (
        attachment.content_sha256
        if attachment.entity_state == "entity_available"
        else None
    )
    payload = {
        "occurrence_schema_version": ASSET_OCCURRENCE_SCHEMA_VERSION,
        "normalized_attachment_id": attachment.record_id,
        "source_attachment_identity": attachment.source_identity,
        "source_message_identity": attachment.source_message_identity,
    }
    occurrence_id = "occurrence_" + normalized_json_digest(payload)
    return AssetOccurrence(
        occurrence_id=occurrence_id,
        normalized_attachment_id=attachment.record_id,
        source_type=attachment.provenance.source_type,
        source_attachment_identity_digest=normalized_json_digest(
            {"source_identity": attachment.source_identity}
        ),
        source_message_identity_digest=normalized_json_digest(
            {"source_identity": attachment.source_message_identity}
        ),
        entity_digest=entity_digest,
        entity_state=attachment.entity_state,
        declared_size=attachment.declared_size,
        actual_size=attachment.actual_size,
        declared_mime=attachment.declared_mime,
        detected_mime=attachment.detected_mime,
        locator_kind=attachment.archive_member_locator.locator_kind,
        locator_fingerprint=attachment.archive_member_locator.locator_fingerprint,
        warning_codes=attachment.warning_codes,
    )


@dataclass(frozen=True)
class AssetEntityDescriptor:
    """A verified logical entity, grouped strictly by SHA-256."""

    entity_digest: str
    actual_size: int
    detected_mime: str
    normalized_asset_entity_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]
    warning_codes: tuple[str, ...] = ()
    descriptor_schema_version: str = ASSET_ENTITY_DESCRIPTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.entity_digest, "invalid_entity_digest")
        _count(self.actual_size, "invalid_entity_size")
        object.__setattr__(
            self, "detected_mime", _mime(self.detected_mime, "invalid_detected_mime")
        )
        entity_ids = tuple(sorted(set(self.normalized_asset_entity_ids)))
        occurrence_ids = tuple(sorted(set(self.occurrence_ids)))
        if not entity_ids:
            raise AssetPlanningError("normalized_entity_missing")
        for value in entity_ids:
            _identity(value, "invalid_asset_entity_id")
        for value in occurrence_ids:
            _identity(value, "invalid_occurrence_id")
        object.__setattr__(self, "normalized_asset_entity_ids", entity_ids)
        object.__setattr__(self, "occurrence_ids", occurrence_ids)
        object.__setattr__(self, "warning_codes", _reason_codes(self.warning_codes))
        if self.descriptor_schema_version != ASSET_ENTITY_DESCRIPTOR_SCHEMA_VERSION:
            raise AssetPlanningError("entity_descriptor_schema_version_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_digest": self.entity_digest,
            "actual_size": self.actual_size,
            "detected_mime": self.detected_mime,
            "normalized_asset_entity_ids": list(self.normalized_asset_entity_ids),
            "occurrence_ids": list(self.occurrence_ids),
            "warning_codes": list(self.warning_codes),
            "descriptor_schema_version": self.descriptor_schema_version,
        }


@dataclass(frozen=True)
class AssetStageOperation:
    operation_id: str
    entity_digest: str
    actual_size: int
    detected_mime: str
    occurrence_ids: tuple[str, ...]
    normalized_asset_entity_ids: tuple[str, ...]
    dependency_operation_ids: tuple[str, ...] = ()
    operation_schema_version: str = ASSET_STAGE_OPERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.operation_id, "invalid_operation_id")
        _sha256(self.entity_digest, "invalid_entity_digest")
        _count(self.actual_size, "invalid_entity_size")
        object.__setattr__(
            self, "detected_mime", _mime(self.detected_mime, "invalid_detected_mime")
        )
        occurrence_ids = tuple(sorted(set(self.occurrence_ids)))
        entity_ids = tuple(sorted(set(self.normalized_asset_entity_ids)))
        dependencies = tuple(sorted(set(self.dependency_operation_ids)))
        for value in occurrence_ids:
            _identity(value, "invalid_occurrence_id")
        for value in entity_ids:
            _identity(value, "invalid_asset_entity_id")
        for value in dependencies:
            _identity(value, "invalid_dependency_operation_id")
        if self.operation_id in dependencies:
            raise AssetPlanningError("asset_stage_dependency_cycle")
        object.__setattr__(self, "occurrence_ids", occurrence_ids)
        object.__setattr__(self, "normalized_asset_entity_ids", entity_ids)
        object.__setattr__(self, "dependency_operation_ids", dependencies)
        if self.operation_schema_version != ASSET_STAGE_OPERATION_SCHEMA_VERSION:
            raise AssetPlanningError("operation_schema_version_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "entity_digest": self.entity_digest,
            "actual_size": self.actual_size,
            "detected_mime": self.detected_mime,
            "normalized_asset_entity_ids": list(self.normalized_asset_entity_ids),
            "dependency_operation_ids": list(self.dependency_operation_ids),
            "operation_schema_version": self.operation_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            **self.identity_payload(),
            "occurrence_ids": list(self.occurrence_ids),
        }


@dataclass(frozen=True)
class AssetStagePlan:
    stage_plan_id: str
    stage_plan_digest: str
    batch_id: str
    manifest_id: str
    import_plan_id: str
    import_plan_digest: str
    asset_inventory_digest: str
    operations: tuple[AssetStageOperation, ...]
    occurrences: tuple[AssetOccurrence, ...]
    entity_descriptors: tuple[AssetEntityDescriptor, ...]
    occurrence_bindings: Mapping[str, str]
    expected_asset_delta: Mapping[str, int]
    asset_stage_fingerprint: AssetStageFingerprint
    warning_codes: tuple[str, ...] = ()
    plan_schema_version: str = ASSET_STAGE_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.stage_plan_id, "invalid_stage_plan_id")
        _sha256(self.stage_plan_digest, "invalid_stage_plan_digest")
        _batch_id(self.batch_id)
        _identity(self.manifest_id, "invalid_manifest_id")
        _identity(self.import_plan_id, "invalid_import_plan_id")
        _sha256(self.import_plan_digest, "invalid_import_plan_digest")
        _sha256(self.asset_inventory_digest, "invalid_asset_inventory_digest")
        operations = tuple(sorted(self.operations, key=lambda item: item.operation_id))
        occurrences = tuple(sorted(self.occurrences, key=lambda item: item.occurrence_id))
        entities = tuple(
            sorted(self.entity_descriptors, key=lambda item: item.entity_digest)
        )
        if len({item.operation_id for item in operations}) != len(operations):
            raise AssetPlanningError("duplicate_stage_operation")
        if len({item.occurrence_id for item in occurrences}) != len(occurrences):
            raise AssetPlanningError("duplicate_asset_occurrence")
        if len({item.entity_digest for item in entities}) != len(entities):
            raise AssetPlanningError("duplicate_asset_entity_descriptor")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "occurrences", occurrences)
        object.__setattr__(self, "entity_descriptors", entities)
        bindings = dict(sorted(self.occurrence_bindings.items()))
        occurrence_ids = {item.occurrence_id for item in occurrences}
        operation_ids = {item.operation_id for item in operations}
        if set(bindings) != {
            item.occurrence_id
            for item in occurrences
            if item.entity_state == "entity_available"
        }:
            raise AssetPlanningError("occurrence_bindings_incomplete")
        if not set(bindings.values()).issubset(operation_ids):
            raise AssetPlanningError("occurrence_binding_operation_missing")
        if not set(bindings).issubset(occurrence_ids):
            raise AssetPlanningError("occurrence_binding_unknown")
        object.__setattr__(self, "occurrence_bindings", MappingProxyType(bindings))
        delta = dict(sorted(self.expected_asset_delta.items()))
        required_delta = {
            "entity_count",
            "occurrence_count",
            "physical_bytes",
            "logical_bytes",
        }
        if set(delta) != required_delta:
            raise AssetPlanningError("invalid_expected_asset_delta")
        for value in delta.values():
            _count(value, "invalid_expected_asset_delta")
        if delta["entity_count"] != len(operations):
            raise AssetPlanningError("expected_asset_delta_mismatch")
        if delta["occurrence_count"] != len(bindings):
            raise AssetPlanningError("expected_asset_delta_mismatch")
        if delta["physical_bytes"] != sum(item.actual_size for item in operations):
            raise AssetPlanningError("expected_asset_delta_mismatch")
        logical = sum(
            next(
                operation.actual_size
                for operation in operations
                if operation.operation_id == bindings[occurrence_id]
            )
            for occurrence_id in bindings
        )
        if delta["logical_bytes"] != logical:
            raise AssetPlanningError("expected_asset_delta_mismatch")
        object.__setattr__(self, "expected_asset_delta", MappingProxyType(delta))
        if not isinstance(self.asset_stage_fingerprint, AssetStageFingerprint):
            raise AssetPlanningError("invalid_asset_stage_fingerprint")
        expected_fingerprint = _stage_fingerprint(operations)
        if self.asset_stage_fingerprint != expected_fingerprint:
            raise AssetPlanningError("asset_stage_fingerprint_mismatch")
        object.__setattr__(self, "warning_codes", _reason_codes(self.warning_codes))
        if self.plan_schema_version != ASSET_STAGE_PLAN_SCHEMA_VERSION:
            raise AssetPlanningError("stage_plan_schema_version_mismatch")
        _validate_acyclic(operations)
        expected_digest = make_plan_digest(self.identity_payload())
        if self.stage_plan_digest != expected_digest:
            raise AssetPlanningError("stage_plan_digest_mismatch")
        if self.stage_plan_id != "astage_" + expected_digest:
            raise AssetPlanningError("stage_plan_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "manifest_id": self.manifest_id,
            "import_plan_id": self.import_plan_id,
            "import_plan_digest": self.import_plan_digest,
            "asset_inventory_digest": self.asset_inventory_digest,
            "operations": [item.to_dict() for item in self.operations],
            "occurrence_bindings": dict(self.occurrence_bindings),
            "expected_asset_delta": dict(self.expected_asset_delta),
            "asset_stage_fingerprint": self.asset_stage_fingerprint.to_dict(),
            "warning_codes": list(self.warning_codes),
            "plan_schema_version": self.plan_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_plan_id": self.stage_plan_id,
            "stage_plan_digest": self.stage_plan_digest,
            **self.identity_payload(),
            "occurrences": [item.to_dict() for item in self.occurrences],
            "entity_descriptors": [
                item.to_dict() for item in self.entity_descriptors
            ],
        }


def _validate_acyclic(operations: Sequence[AssetStageOperation]) -> None:
    by_id = {item.operation_id: item for item in operations}
    for operation in operations:
        if not set(operation.dependency_operation_ids).issubset(by_id):
            raise AssetPlanningError("asset_stage_dependency_missing")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(operation_id: str) -> None:
        if operation_id in visiting:
            raise AssetPlanningError("asset_stage_dependency_cycle")
        if operation_id in visited:
            return
        visiting.add(operation_id)
        for dependency in by_id[operation_id].dependency_operation_ids:
            visit(dependency)
        visiting.remove(operation_id)
        visited.add(operation_id)

    for operation_id in sorted(by_id):
        visit(operation_id)


def _stage_fingerprint(
    operations: Sequence[AssetStageOperation],
) -> AssetStageFingerprint:
    return build_asset_stage_fingerprint(
        AssetFingerprintItem(
            logical_key_digest=normalized_json_digest(
                {
                    "asset_stage_operation": item.operation_id,
                    "entity_digest": item.entity_digest,
                }
            ),
            content_sha256=item.entity_digest,
            size_bytes=item.actual_size,
        )
        for item in operations
    )


def _operation_id(
    *,
    batch_id: str,
    manifest_id: str,
    import_plan_digest: str,
    descriptor: AssetEntityDescriptor,
) -> str:
    payload = {
        "batch_id": batch_id,
        "manifest_id": manifest_id,
        "import_plan_digest": import_plan_digest,
        "entity_digest": descriptor.entity_digest,
        "actual_size": descriptor.actual_size,
        "detected_mime": descriptor.detected_mime,
        "normalized_asset_entity_ids": list(
            descriptor.normalized_asset_entity_ids
        ),
        "operation_schema_version": ASSET_STAGE_OPERATION_SCHEMA_VERSION,
    }
    return "astageop_" + normalized_json_digest(payload)


def build_asset_stage_plan(
    *,
    import_plan: ImportPlan,
    attachments: Sequence[NormalizedAttachment],
    asset_entities: Sequence[AssetEntity],
    occurrences: Sequence[AssetOccurrence] | None = None,
    limits: AssetStageLimits = AssetStageLimits(),
    dependencies_by_entity: Mapping[str, Sequence[str]] | None = None,
) -> AssetStagePlan:
    """Build a deterministic plan for verified, Planner-authorized entities."""
    if not isinstance(import_plan, ImportPlan):
        raise AssetPlanningError("invalid_import_plan")
    if not isinstance(limits, AssetStageLimits):
        raise AssetPlanningError("invalid_asset_stage_limits")
    _batch_id(import_plan.batch_id)
    dependency_map = dependencies_by_entity or {}
    if not isinstance(dependency_map, Mapping):
        raise AssetPlanningError("invalid_dependency_map")

    normalized_occurrences = tuple(
        occurrences
        if occurrences is not None
        else (
            occurrence_from_normalized_attachment(item)
            for item in attachments
        )
    )
    if len(normalized_occurrences) > limits.max_occurrences:
        raise AssetPlanningError("asset_occurrence_limit_exceeded")
    occurrence_ids: set[str] = set()
    for item in normalized_occurrences:
        if not isinstance(item, AssetOccurrence):
            raise AssetPlanningError("invalid_asset_occurrence")
        if item.occurrence_id in occurrence_ids:
            raise AssetPlanningError("duplicate_asset_occurrence")
        occurrence_ids.add(item.occurrence_id)

    attachments_by_id = {item.record_id: item for item in attachments}
    if len(attachments_by_id) != len(attachments):
        raise AssetPlanningError("duplicate_normalized_attachment")
    for occurrence in normalized_occurrences:
        attachment = attachments_by_id.get(occurrence.normalized_attachment_id)
        if attachment is None:
            raise AssetPlanningError("occurrence_attachment_missing")
        expected_digest = (
            attachment.content_sha256
            if attachment.entity_state == "entity_available"
            else None
        )
        if (
            occurrence.entity_digest != expected_digest
            or occurrence.entity_state != attachment.entity_state
        ):
            raise AssetPlanningError("occurrence_attachment_mismatch")

    entities_by_sha: dict[str, list[AssetEntity]] = {}
    entities_by_id: dict[str, AssetEntity] = {}
    for entity in asset_entities:
        if not isinstance(entity, AssetEntity):
            raise AssetPlanningError("invalid_normalized_asset_entity")
        if entity.first_seen_batch_id != import_plan.batch_id:
            raise AssetPlanningError("asset_batch_binding_mismatch")
        if entity.asset_entity_id in entities_by_id:
            raise AssetPlanningError("duplicate_normalized_asset_entity")
        entities_by_id[entity.asset_entity_id] = entity
        entities_by_sha.setdefault(entity.content_sha256, []).append(entity)

    planned_asset_actions = {
        action.source_identity: action
        for action in import_plan.actions
        if action.record_kind == "asset"
    }
    for action in planned_asset_actions.values():
        if (
            action.action_type == "insert"
            and action.source_identity not in entities_by_id
        ):
            raise AssetPlanningError("normalized_entity_missing")

    stageable_entity_ids = {
        source_identity
        for source_identity, action in planned_asset_actions.items()
        if action.action_type == "insert"
    }
    unexpected_entities = set(entities_by_id) - set(planned_asset_actions)
    if unexpected_entities:
        raise AssetPlanningError("unplanned_normalized_entity")

    descriptors: list[AssetEntityDescriptor] = []
    warning_codes: set[str] = set()
    available_occurrences = [
        item
        for item in normalized_occurrences
        if item.entity_state == "entity_available"
    ]
    for digest, group in sorted(entities_by_sha.items()):
        sizes = {item.actual_size for item in group}
        mimes = {item.detected_mime for item in group}
        if len(sizes) != 1 or len(mimes) != 1:
            raise AssetPlanningError("entity_digest_conflict")
        group_ids = {item.asset_entity_id for item in group}
        if not group_ids.intersection(stageable_entity_ids):
            continue
        if not group_ids.issubset(stageable_entity_ids):
            raise AssetPlanningError("entity_digest_action_conflict")
        bound = tuple(
            item.occurrence_id
            for item in available_occurrences
            if item.entity_digest == digest
        )
        if not bound:
            raise AssetPlanningError("entity_occurrence_missing")
        group_warnings: set[str] = set()
        actual_size = next(iter(sizes))
        detected_mime = next(iter(mimes))
        for occurrence in available_occurrences:
            if occurrence.entity_digest != digest:
                continue
            if (
                occurrence.actual_size is not None
                and occurrence.actual_size != actual_size
            ):
                raise AssetPlanningError("entity_digest_size_conflict")
            if (
                occurrence.detected_mime is not None
                and occurrence.detected_mime != detected_mime
            ):
                raise AssetPlanningError("entity_digest_mime_conflict")
            if (
                occurrence.declared_size is not None
                and occurrence.declared_size != actual_size
            ):
                group_warnings.add("declared_size_mismatch")
            if (
                occurrence.declared_mime is not None
                and occurrence.declared_mime != detected_mime
            ):
                group_warnings.add("declared_mime_mismatch")
        warning_codes.update(group_warnings)
        descriptors.append(
            AssetEntityDescriptor(
                entity_digest=digest,
                actual_size=actual_size,
                detected_mime=detected_mime,
                normalized_asset_entity_ids=tuple(sorted(group_ids)),
                occurrence_ids=bound,
                warning_codes=tuple(group_warnings),
            )
        )

    stageable_digests = {item.entity_digest for item in descriptors}
    for occurrence in available_occurrences:
        if occurrence.entity_digest not in stageable_digests:
            matching = entities_by_sha.get(occurrence.entity_digest or "", [])
            if not matching:
                raise AssetPlanningError("normalized_entity_missing")
            matching_ids = {item.asset_entity_id for item in matching}
            if matching_ids.intersection(stageable_entity_ids):
                raise AssetPlanningError("entity_occurrence_binding_missing")

    if len(descriptors) > limits.max_entities:
        raise AssetPlanningError("asset_entity_limit_exceeded")
    physical_bytes = sum(item.actual_size for item in descriptors)
    logical_bytes = sum(
        item.actual_size
        for item in available_occurrences
        if item.entity_digest in stageable_digests
    )
    if any(item.actual_size > limits.max_entity_bytes for item in descriptors):
        raise AssetPlanningError("asset_entity_size_limit_exceeded")
    if physical_bytes > limits.max_batch_physical_bytes:
        raise AssetPlanningError("asset_batch_physical_limit_exceeded")
    if logical_bytes > limits.max_batch_logical_bytes:
        raise AssetPlanningError("asset_batch_logical_limit_exceeded")

    import_plan_digest = normalized_json_digest(import_plan.identity_payload())
    provisional_ids = {
        descriptor.entity_digest: _operation_id(
            batch_id=import_plan.batch_id,
            manifest_id=import_plan.manifest_id,
            import_plan_digest=import_plan_digest,
            descriptor=descriptor,
        )
        for descriptor in descriptors
    }
    operations: list[AssetStageOperation] = []
    for descriptor in descriptors:
        dependency_digests = tuple(
            sorted(set(dependency_map.get(descriptor.entity_digest, ())))
        )
        unknown_dependencies = set(dependency_digests) - set(provisional_ids)
        if unknown_dependencies:
            raise AssetPlanningError("asset_stage_dependency_missing")
        dependency_ids = tuple(
            provisional_ids[value] for value in dependency_digests
        )
        operation_id = _operation_id(
            batch_id=import_plan.batch_id,
            manifest_id=import_plan.manifest_id,
            import_plan_digest=import_plan_digest,
            descriptor=descriptor,
        )
        operations.append(
            AssetStageOperation(
                operation_id=operation_id,
                entity_digest=descriptor.entity_digest,
                actual_size=descriptor.actual_size,
                detected_mime=descriptor.detected_mime,
                occurrence_ids=descriptor.occurrence_ids,
                normalized_asset_entity_ids=(
                    descriptor.normalized_asset_entity_ids
                ),
                dependency_operation_ids=dependency_ids,
            )
        )
    _validate_acyclic(operations)
    operation_by_digest = {
        item.entity_digest: item.operation_id for item in operations
    }
    occurrence_bindings = {
        occurrence.occurrence_id: operation_by_digest[occurrence.entity_digest]
        for occurrence in available_occurrences
        if occurrence.entity_digest in operation_by_digest
    }
    expected_delta = {
        "entity_count": len(operations),
        "occurrence_count": len(occurrence_bindings),
        "physical_bytes": physical_bytes,
        "logical_bytes": logical_bytes,
    }
    fingerprint = _stage_fingerprint(operations)
    identity_payload = {
        "batch_id": import_plan.batch_id,
        "manifest_id": import_plan.manifest_id,
        "import_plan_id": import_plan.plan_id,
        "import_plan_digest": import_plan_digest,
        "asset_inventory_digest": import_plan.asset_inventory_digest,
        "operations": [item.to_dict() for item in sorted(
            operations, key=lambda item: item.operation_id
        )],
        "occurrence_bindings": dict(sorted(occurrence_bindings.items())),
        "expected_asset_delta": expected_delta,
        "asset_stage_fingerprint": fingerprint.to_dict(),
        "warning_codes": sorted(warning_codes),
        "plan_schema_version": ASSET_STAGE_PLAN_SCHEMA_VERSION,
    }
    digest = make_plan_digest(identity_payload)
    return AssetStagePlan(
        stage_plan_id="astage_" + digest,
        stage_plan_digest=digest,
        batch_id=import_plan.batch_id,
        manifest_id=import_plan.manifest_id,
        import_plan_id=import_plan.plan_id,
        import_plan_digest=import_plan_digest,
        asset_inventory_digest=import_plan.asset_inventory_digest,
        operations=tuple(operations),
        occurrences=tuple(normalized_occurrences),
        entity_descriptors=tuple(descriptors),
        occurrence_bindings=occurrence_bindings,
        expected_asset_delta=expected_delta,
        asset_stage_fingerprint=fingerprint,
        warning_codes=tuple(warning_codes),
    )


@dataclass(frozen=True)
class AssetStageResult:
    batch_id: str
    stage_plan_digest: str
    status: str
    entities_staged: int
    entities_reused: int
    entities_quarantined: int
    bytes_processed: int
    asset_stage_fingerprint: AssetStageFingerprint | None
    warning_codes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    ledger_digest: str | None
    verification_digest: str

    def __post_init__(self) -> None:
        _batch_id(self.batch_id)
        _sha256(self.stage_plan_digest, "invalid_stage_plan_digest")
        if self.status not in {
            "staged",
            "staged_with_warnings",
            "failed",
            "cancelled",
            "recovery_required",
            "rolled_back",
        }:
            raise AssetPlanningError("invalid_asset_stage_result")
        for value in (
            self.entities_staged,
            self.entities_reused,
            self.entities_quarantined,
            self.bytes_processed,
        ):
            _count(value, "invalid_asset_stage_result_count")
        if (
            self.asset_stage_fingerprint is not None
            and not isinstance(self.asset_stage_fingerprint, AssetStageFingerprint)
        ):
            raise AssetPlanningError("invalid_asset_stage_fingerprint")
        object.__setattr__(self, "warning_codes", _reason_codes(self.warning_codes))
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        if self.ledger_digest is not None:
            _sha256(self.ledger_digest, "invalid_ledger_digest")
        _sha256(self.verification_digest, "invalid_verification_digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "stage_plan_digest": self.stage_plan_digest,
            "status": self.status,
            "entities_staged": self.entities_staged,
            "entities_reused": self.entities_reused,
            "entities_quarantined": self.entities_quarantined,
            "bytes_processed": self.bytes_processed,
            "asset_stage_fingerprint": (
                self.asset_stage_fingerprint.to_dict()
                if self.asset_stage_fingerprint else None
            ),
            "warning_codes": list(self.warning_codes),
            "reason_codes": list(self.reason_codes),
            "ledger_digest": self.ledger_digest,
            "verification_digest": self.verification_digest,
        }
