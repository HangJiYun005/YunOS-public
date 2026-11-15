from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
from typing import Any, Callable, Mapping, Sequence
import unicodedata
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEDIA_DB_PATH = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
DEFAULT_ALLOWED_ENTITY_ROOTS = (PROJECT_ROOT / "imports" / "chatgpt",)

_MEDIA_ID_DOMAIN = b"yunos-media-id-v1\0"
_MEDIA_ID_RE = re.compile(r"^ym1_[a-z2-7]{52}$")
_SYNTHETIC_ATTACHMENT_ID_DOMAIN = b"yunos-source-attachment-v1\0"
_SYNTHETIC_ATTACHMENT_ID_RE = re.compile(r"^ysa1_[a-z2-7]{52}$")
_SYNTHETIC_ATTACHMENT_SCHEMA_VERSION = "claude-attachment-v1"
_MISSING_ATTACHMENT_FIELD = "yunos-missing-attachment-field-v1"
_APPROVED_SYNTHETIC_ATTACHMENT_SOURCES = frozenset({"chatgpt", "claude"})
_HEX_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "con",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
    "nul",
    "prn",
}
_ACTIVE_MIME_TYPES = {
    "application/javascript",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/html",
    "text/javascript",
}
_STREAMING_MIME_PREFIXES = ("audio/", "video/")
_STREAMING_MIME_TYPES = {"application/pdf"}


class EntityState(str, Enum):
    METADATA_ONLY = "metadata_only"
    ASSET_RECORD_MISSING = "asset_record_missing"
    ASSET_RECORD_PRESENT = "asset_record_present"
    ENTITY_PATH_MISSING = "entity_path_missing"
    ENTITY_MISSING = "entity_missing"
    ENTITY_OUTSIDE_ALLOWED_ROOT = "entity_outside_allowed_root"
    ENTITY_SYMLINK_ESCAPE = "entity_symlink_escape"
    ENTITY_NOT_REGULAR_FILE = "entity_not_regular_file"
    ENTITY_EMPTY = "entity_empty"
    ENTITY_SIZE_MISMATCH = "entity_size_mismatch"
    ENTITY_HASH_MISMATCH = "entity_hash_mismatch"
    ENTITY_IDENTITY_CONFLICT = "entity_identity_conflict"
    ENTITY_MIME_MISMATCH = "entity_mime_mismatch"
    ENTITY_CHANGED_DURING_CHECK = "entity_changed_during_check"
    ENTITY_AVAILABLE = "entity_available"
    EXTERNAL_URL_BLOCKED = "external_url_blocked"
    UNSUPPORTED_SOURCE = "unsupported_source"
    INVALID_RELATIONSHIP = "invalid_relationship"


class MediaIndexError(Exception):
    """Base error whose message is safe to expose to a local API caller."""


class MediaIndexSchemaError(MediaIndexError):
    pass


class MediaIndexConflict(MediaIndexError):
    pass


class MediaIndexCancelled(MediaIndexError):
    pass


@dataclass(frozen=True)
class PathValidationResult:
    state: EntityState
    safe_reason: str
    canonical_path: Path | None = field(default=None, repr=False)

    @property
    def accepted(self) -> bool:
        return self.state in {
            EntityState.ASSET_RECORD_PRESENT,
            EntityState.ENTITY_MISSING,
            EntityState.ENTITY_AVAILABLE,
        }


@dataclass(frozen=True)
class MediaDescriptor:
    media_id: str
    media_kind: str
    safe_filename: str
    declared_mime_type: str
    detected_mime_type: str
    total_size: int | None
    etag_candidate: str
    last_modified: str
    supports_range_candidate: bool
    entity_state: EntityState
    can_open: bool
    can_stream: bool
    unavailable_reason: str

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "mediaId": self.media_id,
            "mediaKind": self.media_kind,
            "safeFilename": self.safe_filename,
            "declaredMimeType": self.declared_mime_type,
            "detectedMimeType": self.detected_mime_type,
            "totalSize": self.total_size,
            "etagCandidate": self.etag_candidate,
            "lastModified": self.last_modified,
            "supportsRangeCandidate": self.supports_range_candidate,
            "entityState": self.entity_state.value,
            "canOpen": self.can_open,
            "canStream": self.can_stream,
            "unavailableReason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class MediaResolution:
    ok: bool
    descriptor: MediaDescriptor | None = None
    error_code: str = ""
    entity_state: EntityState | None = None
    safe_message: str = ""

    def as_public_dict(self) -> dict[str, Any]:
        if self.ok and self.descriptor is not None:
            return self.descriptor.as_public_dict()
        return {
            "errorCode": self.error_code or "media_unavailable",
            "entityState": (
                self.entity_state.value
                if self.entity_state is not None
                else EntityState.INVALID_RELATIONSHIP.value
            ),
            "safeMessage": self.safe_message or "Media is unavailable",
        }


@dataclass(frozen=True)
class MediaIndexRecord:
    media_id: str
    source_attachment_id: str = field(repr=False)
    conversation_id: str = field(repr=False)
    message_id: str = field(repr=False)
    attachment_index: int
    source_kind: str
    declared_filename: str = field(repr=False)
    safe_display_filename: str
    declared_mime_type: str
    declared_size: int | None
    entity_state: EntityState
    entity_locator_kind: str
    expected_hash: str
    actual_size: int | None
    detected_mime_type: str
    actual_hash: str = field(default="", repr=False)
    last_modified: str = ""
    can_open: bool = False
    can_stream: bool = False
    unavailable_reason: str = ""
    asset_file: str = field(default="", repr=False)
    asset_catalog_size: int | None = field(default=None, repr=False)
    canonical_path: Path | None = field(default=None, repr=False)
    source_identity_kind: str = "missing"
    logical_occurrence_count: int = 1
    indistinguishable_duplicate_group: bool = False
    canonical_attachment_digest: str = field(default="", repr=False)

    def descriptor(self) -> MediaDescriptor:
        normalized_mime = self.detected_mime_type or self.declared_mime_type
        return MediaDescriptor(
            media_id=self.media_id,
            media_kind=_media_kind(self.declared_filename, normalized_mime),
            safe_filename=self.safe_display_filename,
            declared_mime_type=self.declared_mime_type,
            detected_mime_type=self.detected_mime_type,
            total_size=self.actual_size if self.actual_size is not None else self.declared_size,
            etag_candidate=(
                f'"sha256-{self.actual_hash}"' if self.actual_hash else ""
            ),
            last_modified=self.last_modified,
            supports_range_candidate=(
                self.entity_state is EntityState.ENTITY_AVAILABLE
                and self.actual_size is not None
                and self.actual_size > 0
                and self.can_stream
            ),
            entity_state=self.entity_state,
            can_open=self.can_open,
            can_stream=self.can_stream,
            unavailable_reason=self.unavailable_reason,
        )


@dataclass(frozen=True)
class MediaIndex:
    records: tuple[MediaIndexRecord, ...]
    allowed_roots: tuple[Path, ...] = field(repr=False)
    _by_media_id: Mapping[str, MediaIndexRecord] = field(repr=False)
    _logical_occurrence_counts: Mapping[str, int] = field(repr=False)
    _logical_identity_kinds: Mapping[str, str] = field(repr=False)

    def get_media_descriptor(self, media_id: str) -> MediaResolution:
        invalid = _validate_media_id(media_id)
        if invalid:
            return invalid
        record = self._by_media_id.get(media_id)
        if record is None:
            return _failure(
                "media_not_found",
                EntityState.INVALID_RELATIONSHIP,
                "Media is unavailable",
            )
        if record.entity_state is EntityState.INVALID_RELATIONSHIP:
            return _failure(
                "invalid_media_relationship",
                record.entity_state,
                "Media relationship is invalid",
            )
        if record.entity_state is EntityState.ENTITY_IDENTITY_CONFLICT:
            return _failure(
                "entity_identity_conflict",
                record.entity_state,
                "Media entity identity is conflicted",
            )
        return MediaResolution(ok=True, descriptor=record.descriptor())

    def resolve_media_relationship(
        self,
        media_id: str,
        *,
        expected_message_id: str,
        expected_conversation_id: str,
    ) -> MediaResolution:
        base = self.get_media_descriptor(media_id)
        if not base.ok:
            return base
        record = self._by_media_id[media_id]
        if (
            not expected_message_id
            or not expected_conversation_id
            or record.message_id != expected_message_id
            or record.conversation_id != expected_conversation_id
        ):
            return _failure(
                "media_relationship_mismatch",
                EntityState.INVALID_RELATIONSHIP,
                "Media relationship is invalid",
            )
        return base

    def inspect_media_entity(self, media_id: str) -> MediaResolution:
        base = self.get_media_descriptor(media_id)
        if not base.ok:
            return base
        record = self._by_media_id[media_id]
        if record.entity_state is not EntityState.ENTITY_AVAILABLE:
            return _failure(
                "media_entity_unavailable",
                record.entity_state,
                record.unavailable_reason or "Media entity is unavailable",
            )
        return base

    def list_recovery_candidates(self) -> dict[str, Any]:
        state_counts = Counter(record.entity_state.value for record in self.records)
        mime_counts = Counter(
            record.declared_mime_type or "(empty)" for record in self.records
        )
        size_bins = Counter(_size_bin(record.declared_size) for record in self.records)
        logical_occurrence_distribution = Counter(
            self._logical_occurrence_counts.values()
        )
        native_identity_count = sum(
            kind == "native" for kind in self._logical_identity_kinds.values()
        )
        synthetic_identity_count = sum(
            kind == "synthetic" for kind in self._logical_identity_kinds.values()
        )
        return {
            "privacy": (
                "aggregate-only; no attachment ids, filenames, message bodies, "
                "paths, or URLs"
            ),
            "records": len(self.records),
            "occurrenceCount": len(self.records),
            "logicalIdentityCount": len(self._logical_occurrence_counts),
            "nativeIdentityCount": native_identity_count,
            "syntheticIdentityCount": synthetic_identity_count,
            "nativeOccurrenceCount": sum(
                record.source_identity_kind == "native"
                and bool(record.media_id)
                for record in self.records
            ),
            "syntheticOccurrenceCount": sum(
                record.source_identity_kind == "synthetic"
                and bool(record.media_id)
                for record in self.records
            ),
            "uniqueMediaIds": len(self._logical_occurrence_counts),
            "logicalOccurrenceCountDistribution": {
                str(count): groups
                for count, groups in sorted(logical_occurrence_distribution.items())
            },
            "indistinguishableDuplicateGroups": sum(
                count > 1
                and self._logical_identity_kinds.get(media_id) == "synthetic"
                for media_id, count in self._logical_occurrence_counts.items()
            ),
            "indistinguishableDuplicateOccurrences": sum(
                count
                for media_id, count in self._logical_occurrence_counts.items()
                if count > 1
                and self._logical_identity_kinds.get(media_id) == "synthetic"
            ),
            "unboundOccurrenceCount": sum(
                not record.media_id
                or record.entity_state is EntityState.INVALID_RELATIONSHIP
                for record in self.records
            ),
            "invalidRelationships": state_counts.get(
                EntityState.INVALID_RELATIONSHIP.value, 0
            ),
            "entityIdentityConflicts": sum(
                record.entity_state is EntityState.ENTITY_IDENTITY_CONFLICT
                and record.logical_occurrence_count >= 1
                for record in self._by_media_id.values()
            ),
            "collisions": 0,
            "entityStates": dict(sorted(state_counts.items())),
            "declaredMimeTypes": dict(
                sorted(mime_counts.items(), key=lambda item: (-item[1], item[0]))
            ),
            "declaredSizeBins": dict(sorted(size_bins.items())),
            "stableSourceIds": sum(
                bool(record.source_attachment_id) for record in self.records
            ),
            "stableMediaIds": sum(
                bool(record.media_id) and _MEDIA_ID_RE.fullmatch(record.media_id) is not None
                for record in self.records
            ),
            "assetCatalogMappings": sum(
                bool(record.asset_file) for record in self.records
            ),
            "assetCatalogDeclaredSizeMismatches": sum(
                record.asset_catalog_size is not None
                and record.declared_size is not None
                and record.asset_catalog_size != record.declared_size
                for record in self.records
            ),
            "controlledImportRecoveryCandidates": sum(
                record.entity_state
                in {
                    EntityState.ASSET_RECORD_PRESENT,
                    EntityState.ENTITY_MISSING,
                    EntityState.ENTITY_EMPTY,
                    EntityState.ENTITY_SIZE_MISMATCH,
                    EntityState.ENTITY_HASH_MISMATCH,
                    EntityState.ENTITY_MIME_MISMATCH,
                }
                for record in self.records
            ),
            "entityAvailable": state_counts.get(
                EntityState.ENTITY_AVAILABLE.value, 0
            ),
        }


@dataclass(frozen=True)
class _AttachmentCandidate:
    source_attachment_id: str
    conversation_id: str
    message_id: str
    attachment_index: int
    source_platform: str
    source_kind: str
    declared_filename: str
    declared_mime_type: str
    declared_size: int | None
    expected_hash: str
    external_url_present: bool
    relationship_valid: bool
    source_identity_kind: str
    canonical_attachment_digest: str


def derive_media_id(source_attachment_id: str, source_platform: str = "chatgpt") -> str:
    attachment_id = _clean_identity(source_attachment_id)
    platform = _clean_identity(source_platform or "chatgpt").lower()
    if not attachment_id or not platform:
        raise MediaIndexError("Media identity is invalid")
    digest = hashlib.sha256(
        _MEDIA_ID_DOMAIN
        + platform.encode("utf-8")
        + b"\0"
        + attachment_id.encode("utf-8")
    ).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"ym1_{encoded}"


def derive_source_attachment_identity(
    source_platform: str,
    conversation_id: str,
    message_id: str,
    attachment: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Expose the existing native/``ysa1_`` source identity protocol.

    Adapters call this boundary instead of copying the private deterministic
    attachment algorithm.  The tuple contains the source attachment ID, its
    identity kind, and the canonical descriptor digest.
    """

    return _source_attachment_identity(
        source_platform,
        conversation_id,
        message_id,
        attachment,
    )


def build_media_index(
    db_path: str | Path = DEFAULT_MEDIA_DB_PATH,
    *,
    inspect_entities: bool = True,
    compute_hash: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    _allowed_roots: Sequence[str | Path] | None = None,
) -> MediaIndex:
    """
    Build an in-memory, read-only media index.

    ``_allowed_roots`` is an internal test/configuration seam. Production callers
    must use the fixed repository root in ``DEFAULT_ALLOWED_ENTITY_ROOTS`` and
    must never populate it from HTTP or other user input.
    """

    roots = _normalize_allowed_roots(
        DEFAULT_ALLOWED_ENTITY_ROOTS if _allowed_roots is None else _allowed_roots
    )
    database = Path(db_path).resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        candidates, asset_rows = _load_candidates(connection)
    finally:
        connection.close()

    identity_counts = Counter(
        (candidate.source_platform, candidate.source_attachment_id)
        for candidate in candidates
        if candidate.source_attachment_id
    )
    derived_sources: dict[str, tuple[str, str]] = {}
    candidate_media_ids: list[str] = []
    for candidate in candidates:
        if not candidate.source_attachment_id:
            candidate_media_ids.append("")
            continue
        media_id = derive_media_id(
            candidate.source_attachment_id,
            candidate.source_platform,
        )
        source_identity = (
            candidate.source_platform,
            candidate.source_attachment_id,
        )
        prior = derived_sources.get(media_id)
        if prior is not None and prior != source_identity:
            raise MediaIndexConflict("Media identity collision")
        derived_sources[media_id] = source_identity
        candidate_media_ids.append(media_id)

    records: list[MediaIndexRecord] = []
    for candidate, media_id in zip(candidates, candidate_media_ids):
        duplicated_identity = (
            candidate.source_identity_kind == "native"
            and identity_counts[
                (candidate.source_platform, candidate.source_attachment_id)
            ]
            > 1
        )
        relationship_valid = (
            candidate.relationship_valid
            and bool(media_id)
            and not duplicated_identity
        )
        record = _build_record(
            candidate,
            media_id,
            asset_rows,
            roots,
            relationship_valid=relationship_valid,
            inspect_entities=inspect_entities,
            compute_hash=compute_hash,
            cancel_check=cancel_check,
        )
        records.append(record)

    grouped_record_indexes: dict[str, list[int]] = {}
    for record_index, record in enumerate(records):
        if (
            record.media_id
            and record.entity_state is not EntityState.INVALID_RELATIONSHIP
        ):
            grouped_record_indexes.setdefault(record.media_id, []).append(record_index)

    for media_id, indexes in grouped_record_indexes.items():
        group = [records[index] for index in indexes]
        occurrence_count = len(group)
        duplicate_group = (
            occurrence_count > 1
            and all(record.source_identity_kind == "synthetic" for record in group)
            and len({record.source_attachment_id for record in group}) == 1
            and len({record.conversation_id for record in group}) == 1
            and len({record.message_id for record in group}) == 1
            and len({record.canonical_attachment_digest for record in group}) == 1
        )
        if occurrence_count > 1 and not duplicate_group:
            raise MediaIndexConflict("Media relationship conflict")

        expected_hashes = {record.expected_hash for record in group if record.expected_hash}
        actual_hashes = {record.actual_hash for record in group if record.actual_hash}
        identity_conflict = len(expected_hashes) > 1 or len(actual_hashes) > 1
        for index in indexes:
            updated = replace(
                records[index],
                logical_occurrence_count=occurrence_count,
                indistinguishable_duplicate_group=duplicate_group,
            )
            if identity_conflict:
                updated = replace(
                    updated,
                    entity_state=EntityState.ENTITY_IDENTITY_CONFLICT,
                    can_open=False,
                    can_stream=False,
                    unavailable_reason="Media entity identity is conflicted",
                )
            records[index] = updated

    by_media_id: dict[str, MediaIndexRecord] = {}
    logical_occurrence_counts: dict[str, int] = {}
    logical_identity_kinds: dict[str, str] = {}
    for media_id, indexes in grouped_record_indexes.items():
        representative = records[indexes[0]]
        by_media_id[media_id] = representative
        logical_occurrence_counts[media_id] = len(indexes)
        logical_identity_kinds[media_id] = representative.source_identity_kind
    return MediaIndex(
        tuple(records),
        roots,
        by_media_id,
        logical_occurrence_counts,
        logical_identity_kinds,
    )


def get_media_descriptor(index: MediaIndex, media_id: str) -> MediaResolution:
    return index.get_media_descriptor(media_id)


def resolve_media_relationship(
    index: MediaIndex,
    media_id: str,
    *,
    expected_message_id: str,
    expected_conversation_id: str,
) -> MediaResolution:
    return index.resolve_media_relationship(
        media_id,
        expected_message_id=expected_message_id,
        expected_conversation_id=expected_conversation_id,
    )


def inspect_media_entity(index: MediaIndex, media_id: str) -> MediaResolution:
    return index.inspect_media_entity(media_id)


def list_recovery_candidates(index: MediaIndex) -> dict[str, Any]:
    return index.list_recovery_candidates()


def validate_allowed_entity_path(
    locator: str,
    *,
    _allowed_roots: Sequence[str | Path] | None = None,
) -> PathValidationResult:
    roots = _normalize_allowed_roots(
        DEFAULT_ALLOWED_ENTITY_ROOTS if _allowed_roots is None else _allowed_roots
    )
    if not isinstance(locator, str) or not locator:
        return PathValidationResult(
            EntityState.ENTITY_PATH_MISSING,
            "Entity locator is missing",
        )
    if locator != locator.strip() or "\x00" in locator or _has_control(locator):
        return _unsafe_path("Entity locator is invalid")

    lowered = locator.lower()
    if lowered.startswith(("http:", "https:", "file:", "data:", "blob:")):
        return PathValidationResult(
            EntityState.EXTERNAL_URL_BLOCKED,
            "External entity locations are blocked",
        )
    if _PERCENT_ESCAPE_RE.search(locator):
        return _unsafe_path("Encoded entity locators are blocked")
    if locator.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        return _unsafe_path("Device entity paths are blocked")

    windows_path = PureWindowsPath(locator)
    if windows_path.drive or windows_path.root or windows_path.is_absolute():
        return _unsafe_path("Absolute entity paths are blocked")
    if locator.startswith(("\\\\", "//")):
        return _unsafe_path("Network entity paths are blocked")

    normalized = locator.replace("\\", "/")
    if normalized.startswith("/") or "//" in normalized:
        return _unsafe_path("Entity locator is invalid")
    parts = normalized.split("/")
    if not parts or any(
        not part
        or part in {".", ".."}
        or part.endswith((".", " "))
        or ":" in part
        for part in parts
    ):
        return _unsafe_path("Entity locator is unsafe")

    existing: list[Path] = []
    missing: list[Path] = []
    for root in roots:
        if _path_has_reparse_component(root):
            return PathValidationResult(
                EntityState.ENTITY_SYMLINK_ESCAPE,
                "Allowed entity root is linked",
            )
        candidate = root.joinpath(*parts)
        component = root
        for part in parts:
            component = component / part
            if os.path.lexists(component) and _is_reparse_point(component):
                return PathValidationResult(
                    EntityState.ENTITY_SYMLINK_ESCAPE,
                    "Linked entity paths are blocked",
                )
        canonical_root = root.resolve(strict=False)
        canonical_candidate = candidate.resolve(strict=False)
        if not _is_within(canonical_candidate, canonical_root):
            return _unsafe_path("Entity path escapes the allowed root")
        if candidate.exists() or candidate.is_symlink():
            existing.append(canonical_candidate)
        else:
            missing.append(canonical_candidate)

    if len(existing) > 1:
        return PathValidationResult(
            EntityState.INVALID_RELATIONSHIP,
            "Entity locator is ambiguous",
        )
    if existing:
        return PathValidationResult(
            EntityState.ASSET_RECORD_PRESENT,
            "",
            existing[0],
        )
    if missing:
        return PathValidationResult(
            EntityState.ENTITY_MISSING,
            "Entity file is unavailable",
            missing[0],
        )
    return PathValidationResult(
        EntityState.ENTITY_PATH_MISSING,
        "No allowed entity root is configured",
    )


def _load_candidates(
    connection: sqlite3.Connection,
) -> tuple[list[_AttachmentCandidate], dict[str, sqlite3.Row]]:
    required_tables = {"assets", "conversations", "messages"}
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not required_tables <= tables:
        raise MediaIndexSchemaError("Media source schema is unavailable")

    message_columns = _table_columns(connection, "messages")
    required_message_columns = {
        "message_id",
        "conversation_id",
        "metadata_json",
    }
    if not required_message_columns <= message_columns:
        raise MediaIndexSchemaError("Media message schema is unavailable")
    asset_columns = _table_columns(connection, "assets")
    if not {"asset_file", "original_name", "size_bytes"} <= asset_columns:
        raise MediaIndexSchemaError("Media asset schema is unavailable")

    conversations = {
        str(row[0])
        for row in connection.execute(
            "SELECT conversation_id FROM conversations"
        )
        if row[0] is not None
    }
    message_counts = Counter(
        str(row[0] or "")
        for row in connection.execute("SELECT message_id FROM messages")
    )
    source_platform_sql = (
        "source_platform" if "source_platform" in message_columns else "NULL"
    )
    rows = connection.execute(
        "SELECT message_id, conversation_id, metadata_json, "
        f"{source_platform_sql} AS source_platform FROM messages"
    )
    candidates: list[_AttachmentCandidate] = []
    for row in rows:
        metadata = _parse_metadata(row["metadata_json"])
        attachments = metadata.get("attachments")
        if not isinstance(attachments, list):
            continue
        message_id = _clean_identity(row["message_id"])
        conversation_id = _clean_identity(row["conversation_id"])
        source_platform = (
            _clean_identity(row["source_platform"]).lower() or "chatgpt"
        )
        relationship_valid = (
            bool(message_id)
            and bool(conversation_id)
            and message_counts[message_id] == 1
            and conversation_id in conversations
        )
        for attachment_index, raw_attachment in enumerate(attachments):
            if not isinstance(raw_attachment, dict):
                candidates.append(
                    _AttachmentCandidate(
                        source_attachment_id="",
                        conversation_id=conversation_id,
                        message_id=message_id,
                        attachment_index=attachment_index,
                        source_platform=source_platform,
                        source_kind="unsupported",
                        declared_filename="",
                        declared_mime_type="",
                        declared_size=None,
                        expected_hash="",
                        external_url_present=False,
                        relationship_valid=False,
                        source_identity_kind="missing",
                        canonical_attachment_digest="",
                    )
                )
                continue
            (
                source_attachment_id,
                source_identity_kind,
                canonical_attachment_digest,
            ) = _source_attachment_identity(
                source_platform,
                conversation_id,
                message_id,
                raw_attachment,
            )
            synthetic = source_identity_kind == "synthetic"
            candidates.append(
                _AttachmentCandidate(
                    source_attachment_id=source_attachment_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    attachment_index=attachment_index,
                    source_platform=source_platform,
                    source_kind=(
                        "claude_attachment"
                        if synthetic
                        else _source_kind(raw_attachment)
                    ),
                    declared_filename=_clean_text(
                        raw_attachment.get("file_name" if synthetic else "name")
                    ),
                    declared_mime_type=_normalize_mime(
                        raw_attachment.get(
                            "file_type" if synthetic else "mime_type"
                        )
                    ),
                    declared_size=_declared_size(
                        raw_attachment.get("file_size" if synthetic else "size")
                    ),
                    expected_hash=_expected_hash(raw_attachment),
                    external_url_present=_contains_external_url(raw_attachment),
                    relationship_valid=relationship_valid,
                    source_identity_kind=source_identity_kind,
                    canonical_attachment_digest=canonical_attachment_digest,
                )
            )

    assets = {
        str(row["asset_file"]): row
        for row in connection.execute(
            "SELECT asset_file, original_name, size_bytes FROM assets"
        )
        if isinstance(row["asset_file"], str) and row["asset_file"]
    }
    return candidates, assets


def _build_record(
    candidate: _AttachmentCandidate,
    media_id: str,
    assets: Mapping[str, sqlite3.Row],
    roots: tuple[Path, ...],
    *,
    relationship_valid: bool,
    inspect_entities: bool,
    compute_hash: bool,
    cancel_check: Callable[[], bool] | None,
) -> MediaIndexRecord:
    base = MediaIndexRecord(
        media_id=media_id,
        source_attachment_id=candidate.source_attachment_id,
        conversation_id=candidate.conversation_id,
        message_id=candidate.message_id,
        attachment_index=candidate.attachment_index,
        source_kind=candidate.source_kind,
        declared_filename=candidate.declared_filename,
        safe_display_filename=_safe_display_filename(
            candidate.declared_filename
        ),
        declared_mime_type=candidate.declared_mime_type,
        declared_size=candidate.declared_size,
        entity_state=EntityState.METADATA_ONLY,
        entity_locator_kind="none",
        expected_hash=candidate.expected_hash,
        actual_size=None,
        detected_mime_type="",
        source_identity_kind=candidate.source_identity_kind,
        canonical_attachment_digest=candidate.canonical_attachment_digest,
    )
    if not relationship_valid:
        return replace(
            base,
            entity_state=EntityState.INVALID_RELATIONSHIP,
            unavailable_reason="Media relationship is invalid",
        )
    if candidate.external_url_present:
        return replace(
            base,
            entity_state=EntityState.EXTERNAL_URL_BLOCKED,
            entity_locator_kind="external_url",
            unavailable_reason="External media locations are blocked",
        )

    expected_asset_file = f"{candidate.source_attachment_id}.dat"
    asset = assets.get(expected_asset_file)
    if asset is None:
        return replace(
            base,
            entity_state=EntityState.ASSET_RECORD_MISSING,
            unavailable_reason="Asset catalog record is unavailable",
        )

    record = replace(
        base,
        entity_state=EntityState.ASSET_RECORD_PRESENT,
        entity_locator_kind="asset_catalog_relative",
        asset_file=expected_asset_file,
        asset_catalog_size=_declared_size(asset["size_bytes"]),
    )
    if not inspect_entities:
        return record
    validation = validate_allowed_entity_path(
        expected_asset_file,
        _allowed_roots=roots,
    )
    if validation.state is not EntityState.ASSET_RECORD_PRESENT:
        return replace(
            record,
            entity_state=validation.state,
            unavailable_reason=validation.safe_reason,
            canonical_path=validation.canonical_path,
        )
    return _inspect_file(
        record,
        validation.canonical_path,
        compute_hash=compute_hash,
        cancel_check=cancel_check,
    )


def _inspect_file(
    record: MediaIndexRecord,
    path: Path | None,
    *,
    compute_hash: bool,
    cancel_check: Callable[[], bool] | None,
) -> MediaIndexRecord:
    if path is None:
        return replace(
            record,
            entity_state=EntityState.ENTITY_PATH_MISSING,
            unavailable_reason="Entity path is unavailable",
        )
    try:
        before = path.stat()
    except FileNotFoundError:
        return replace(
            record,
            entity_state=EntityState.ENTITY_MISSING,
            unavailable_reason="Entity file is unavailable",
        )
    except OSError:
        return replace(
            record,
            entity_state=EntityState.ENTITY_NOT_REGULAR_FILE,
            unavailable_reason="Entity file cannot be inspected",
        )
    if _is_reparse_point(path):
        return replace(
            record,
            entity_state=EntityState.ENTITY_SYMLINK_ESCAPE,
            unavailable_reason="Linked entity paths are blocked",
        )
    if not stat.S_ISREG(before.st_mode):
        return replace(
            record,
            entity_state=EntityState.ENTITY_NOT_REGULAR_FILE,
            unavailable_reason="Entity is not a regular file",
        )
    if before.st_size == 0:
        return replace(
            record,
            entity_state=EntityState.ENTITY_EMPTY,
            actual_size=0,
            unavailable_reason="Entity file is empty",
        )

    try:
        with path.open("rb") as handle:
            prefix = handle.read(8192)
            detected = _detect_mime_type(prefix, record.declared_filename)
            actual_hash = ""
            if compute_hash or record.expected_hash:
                digest = hashlib.sha256()
                digest.update(prefix)
                while True:
                    if cancel_check is not None and cancel_check():
                        raise MediaIndexCancelled("Media inspection was cancelled")
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                actual_hash = digest.hexdigest()
    except MediaIndexCancelled:
        raise
    except OSError:
        return replace(
            record,
            entity_state=EntityState.ENTITY_NOT_REGULAR_FILE,
            unavailable_reason="Entity file cannot be inspected",
        )

    try:
        after = path.stat()
    except OSError:
        return replace(
            record,
            entity_state=EntityState.ENTITY_CHANGED_DURING_CHECK,
            unavailable_reason="Entity changed during inspection",
        )
    inspected = replace(
        record,
        actual_size=after.st_size,
        detected_mime_type=detected,
        actual_hash=actual_hash,
        last_modified=str(after.st_mtime_ns),
        canonical_path=path,
    )
    if _stat_identity(before) != _stat_identity(after):
        return replace(
            inspected,
            entity_state=EntityState.ENTITY_CHANGED_DURING_CHECK,
            unavailable_reason="Entity changed during inspection",
        )
    if (
        record.declared_size is not None
        and record.declared_size != after.st_size
    ):
        return replace(
            inspected,
            entity_state=EntityState.ENTITY_SIZE_MISMATCH,
            unavailable_reason="Entity size does not match metadata",
        )
    if record.expected_hash and actual_hash != record.expected_hash:
        return replace(
            inspected,
            entity_state=EntityState.ENTITY_HASH_MISMATCH,
            unavailable_reason="Entity integrity check failed",
        )
    if not _mime_compatible(record.declared_mime_type, detected):
        return replace(
            inspected,
            entity_state=EntityState.ENTITY_MIME_MISMATCH,
            unavailable_reason="Entity MIME type does not match metadata",
        )

    effective_mime = detected or record.declared_mime_type
    active = effective_mime in _ACTIVE_MIME_TYPES
    can_stream = (
        effective_mime.startswith(_STREAMING_MIME_PREFIXES)
        or effective_mime in _STREAMING_MIME_TYPES
        or after.st_size >= 1024 * 1024
    ) and not active
    return replace(
        inspected,
        entity_state=EntityState.ENTITY_AVAILABLE,
        can_open=bool(effective_mime) and not active,
        can_stream=can_stream,
        unavailable_reason="",
    )


def _validate_media_id(media_id: str) -> MediaResolution | None:
    if not isinstance(media_id, str) or _MEDIA_ID_RE.fullmatch(media_id) is None:
        return _failure(
            "invalid_media_id",
            EntityState.INVALID_RELATIONSHIP,
            "Media identifier is invalid",
        )
    return None


def _failure(
    code: str,
    state: EntityState,
    message: str,
) -> MediaResolution:
    return MediaResolution(
        ok=False,
        error_code=code,
        entity_state=state,
        safe_message=message,
    )


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _normalize_allowed_roots(
    roots: Sequence[str | Path],
) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for raw_root in roots:
        root = Path(raw_root)
        if not root.is_absolute():
            raise MediaIndexError("Allowed entity roots must be fixed absolute paths")
        canonical = root.resolve(strict=False)
        if canonical not in normalized:
            normalized.append(canonical)
    return tuple(normalized)


def _unsafe_path(reason: str) -> PathValidationResult:
    return PathValidationResult(
        EntityState.ENTITY_OUTSIDE_ALLOWED_ROOT,
        reason,
    )


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath(
            (os.path.normcase(str(candidate)), os.path.normcase(str(root)))
        )
    except ValueError:
        return False
    return common == os.path.normcase(str(root))


def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _path_has_reparse_component(path: Path) -> bool:
    return any(
        os.path.lexists(component) and _is_reparse_point(component)
        for component in (path, *path.parents)
    )


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_dev", 0)),
        int(getattr(value, "st_ino", 0)),
    )


def _clean_identity(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > 512
        or "\x00" in cleaned
        or _has_control(cleaned)
    ):
        return ""
    return cleaned


def _private_text_digest(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_text_field(
    attachment: Mapping[str, Any],
    key: str,
    *,
    private: bool = False,
    allow_controls: bool = False,
) -> dict[str, Any] | None:
    if key not in attachment or attachment[key] is None:
        return {"state": _MISSING_ATTACHMENT_FIELD}
    value = attachment[key]
    if (
        not isinstance(value, str)
        or len(value) > 16 * 1024 * 1024
        or "\x00" in value
        or (not allow_controls and _has_control(value))
    ):
        return None
    normalized = unicodedata.normalize("NFC", value)
    if private:
        return {"state": "value", "sha256": _private_text_digest(normalized)}
    return {"state": "value", "value": normalized}


def _canonical_mime_field(
    attachment: Mapping[str, Any],
) -> dict[str, Any] | None:
    field = _canonical_text_field(attachment, "file_type")
    if field is None or field["state"] == _MISSING_ATTACHMENT_FIELD:
        return field
    raw_value = str(field["value"])
    normalized = _normalize_mime(raw_value)
    if normalized:
        return {"state": "value", "value": normalized}
    return {
        "state": "unrecognized",
        "sha256": _private_text_digest(raw_value.strip().lower()),
    }


def _canonical_size_field(
    attachment: Mapping[str, Any],
) -> dict[str, Any] | None:
    if "file_size" not in attachment or attachment["file_size"] is None:
        return {"state": _MISSING_ATTACHMENT_FIELD}
    value = attachment["file_size"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return {"state": "value", "value": value}


def _canonical_claude_attachment_digest(
    attachment: Mapping[str, Any],
) -> str:
    filename = _canonical_text_field(attachment, "file_name", private=True)
    mime_type = _canonical_mime_field(attachment)
    size = _canonical_size_field(attachment)
    content = _canonical_text_field(
        attachment,
        "extracted_content",
        private=True,
        allow_controls=True,
    )
    if any(field is None for field in (filename, mime_type, size, content)):
        return ""
    descriptor = {
        "attachment_structure_version": _SYNTHETIC_ATTACHMENT_SCHEMA_VERSION,
        "file_name": filename,
        "file_type": mime_type,
        "file_size": size,
        "extracted_content": content,
    }
    canonical = json.dumps(
        descriptor,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _source_attachment_identity(
    source_platform: str,
    conversation_id: str,
    message_id: str,
    attachment: Mapping[str, Any],
) -> tuple[str, str, str]:
    native_value = attachment.get("id")
    native_id = _clean_identity(native_value)
    if native_id:
        return native_id, "native", ""
    if "id" in attachment and native_value == "":
        return "", "missing", ""
    if native_value not in (None, ""):
        return "", "missing", ""

    platform = _clean_identity(source_platform).lower()
    conversation = _clean_identity(conversation_id)
    message = _clean_identity(message_id)
    if (
        platform not in _APPROVED_SYNTHETIC_ATTACHMENT_SOURCES
        or not conversation
        or not message
    ):
        return "", "missing", ""

    descriptor_digest = _canonical_claude_attachment_digest(attachment)
    if not descriptor_digest:
        return "", "missing", ""
    payload = (
        _SYNTHETIC_ATTACHMENT_ID_DOMAIN
        + platform.encode("utf-8")
        + b"\0"
        + _private_text_digest(conversation).encode("ascii")
        + b"\0"
        + _private_text_digest(message).encode("ascii")
        + b"\0"
        + descriptor_digest.encode("ascii")
    )
    encoded = (
        base64.b32encode(hashlib.sha256(payload).digest())
        .decode("ascii")
        .rstrip("=")
        .lower()
    )
    synthetic_id = f"ysa1_{encoded}"
    if _SYNTHETIC_ATTACHMENT_ID_RE.fullmatch(synthetic_id) is None:
        raise MediaIndexError("Synthetic media identity is invalid")
    return synthetic_id, "synthetic", descriptor_digest


def _clean_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _safe_display_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    leaf = PurePosixPath(normalized.replace("\\", "/")).name
    leaf = "".join(
        "_" if character in '<>:"/\\|?*' or ord(character) < 32 else character
        for character in leaf
    )
    leaf = re.sub(r"\s+", " ", leaf).strip(" .")
    if not leaf:
        leaf = "attachment"
    stem = leaf.split(".", 1)[0].lower()
    if stem in _WINDOWS_RESERVED_NAMES:
        leaf = f"_{leaf}"
    if len(leaf) > 180:
        suffix = PurePosixPath(leaf).suffix[:20]
        keep = max(1, 180 - len(suffix))
        leaf = leaf[:keep].rstrip(" .") + suffix
    return leaf


def _normalize_mime(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.split(";", 1)[0].strip().lower()
    if not normalized or not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", normalized):
        return ""
    aliases = {
        "image/jpg": "image/jpeg",
        "application/x-zip-compressed": "application/zip",
    }
    return aliases.get(normalized, normalized)


def _declared_size(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _expected_hash(attachment: Mapping[str, Any]) -> str:
    for key in ("sha256", "checksum", "hash"):
        value = attachment.get(key)
        if isinstance(value, str) and _HEX_SHA256_RE.fullmatch(value.strip()):
            return value.strip().lower()
    return ""


def _source_kind(attachment: Mapping[str, Any]) -> str:
    source = attachment.get("source")
    if isinstance(source, str) and source.strip():
        normalized = re.sub(r"[^a-z0-9_-]+", "-", source.strip().lower())
        return normalized[:64] or "chatgpt_attachment"
    return "chatgpt_attachment"


def _contains_external_url(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_external_url(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_external_url(item) for item in value)
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value.strip())
    return parsed.scheme.lower() in {"http", "https", "file", "data", "blob"}


def _detect_mime_type(prefix: bytes, filename: str) -> str:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"PK\x03\x04"):
        return "application/zip"
    if prefix.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio/wav"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "video/mp4"
    if prefix and b"\x00" not in prefix:
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            guessed, _ = mimetypes.guess_type(filename)
            normalized = _normalize_mime(guessed)
            if normalized.startswith("text/") or normalized in {
                "application/json",
                "application/javascript",
            }:
                return normalized
            return "text/plain"
    guessed, _ = mimetypes.guess_type(filename)
    return _normalize_mime(guessed)


def _mime_compatible(declared: str, detected: str) -> bool:
    if not declared or not detected:
        return True
    if declared == detected or declared == "application/octet-stream":
        return True
    if declared.startswith("text/") and detected == "text/plain":
        return True
    if detected == "application/zip" and declared in {
        "application/epub+zip",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        return True
    return False


def _media_kind(filename: str, mime_type: str) -> str:
    normalized = _normalize_mime(mime_type)
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    if normalized == "application/pdf":
        return "pdf"
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if suffix in {".zip", ".7z", ".rar", ".tar", ".gz"}:
        return "archive"
    if suffix in {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}:
        return "office"
    if normalized.startswith("text/"):
        return "text"
    return "binary"


def _size_bin(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value < 1024 * 1024:
        return "<1MiB"
    if value < 10 * 1024 * 1024:
        return "1-10MiB"
    if value < 50 * 1024 * 1024:
        return "10-50MiB"
    if value < 100 * 1024 * 1024:
        return "50-100MiB"
    return ">=100MiB"
