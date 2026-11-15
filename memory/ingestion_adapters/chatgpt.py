"""Unified, read-only ChatGPT source adapter for YunOS v8.3-C.

The adapter owns source discovery, archive validation, ChatGPT conversation
graph interpretation, deterministic normalization, and attachment inventory.
It contains no production SQL, performs no network requests, never executes an
``ImportPlan``, and delegates all global conflict decisions to the v8.3-B
planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
import unicodedata
from urllib.parse import unquote, urlsplit
import zipfile

from memory.ingestion_core import (
    ArchiveEntryMetadata,
    ArchiveLimits,
    BatchState,
    DatabaseFingerprint,
    ImportBatch,
    IngestionCoreError,
    SourceManifest,
    build_database_fingerprint,
    build_source_manifest,
    create_import_batch,
    make_plan_digest,
    normalized_json_digest,
    stream_file_sha256,
    validate_archive_metadata,
)
from memory.ingestion_planning import (
    AssetEntity,
    ExistingRecord,
    ImportPlan,
    NormalizedAttachment,
    NormalizedConversation,
    NormalizedMessage,
    RecordLocator,
    make_asset_entity,
    make_provenance,
    make_record_locator,
    normalize_attachment as normalize_planning_attachment,
    normalize_conversation as normalize_planning_conversation,
    normalize_message as normalize_planning_message,
    plan_import,
)
from memory.media_index import (
    MediaIndexError,
    derive_media_id,
    derive_source_attachment_identity,
)


SOURCE_TYPE = "chatgpt"
SOURCE_VERSION = "official_export"
ADAPTER_NAME = "chatgpt_unified"
ADAPTER_VERSION = "v8.3-real-chatgpt-adapter-2"
ACTIVE_PATH_COMPAT = "active_path_compat"
ALL_REACHABLE_AUDIT = "all_reachable_audit"
BRANCH_POLICIES = frozenset({ACTIVE_PATH_COMPAT, ALL_REACHABLE_AUDIT})
STRUCTURED_TEXT_V1 = "structured_text_v1"
LEGACY_IMPORT_TEXT_V1 = "legacy_import_text_v1"
TEXT_POLICIES = frozenset({STRUCTURED_TEXT_V1, LEGACY_IMPORT_TEXT_V1})
FILENAME_COMPAT_MIME_V1 = "filename_compat_mime_v1"
CONTENT_SNIFF_MIME_V1 = "content_sniff_mime_v1"
ASSET_MIME_POLICIES = frozenset({
    FILENAME_COMPAT_MIME_V1,
    CONTENT_SNIFF_MIME_V1,
})
_TEXT_POLICY_VERSION_TOKENS = {
    STRUCTURED_TEXT_V1: "st1",
    LEGACY_IMPORT_TEXT_V1: "lt1",
}
_ASSET_MIME_POLICY_VERSION_TOKENS = {
    FILENAME_COMPAT_MIME_V1: "fm1",
    CONTENT_SNIFF_MIME_V1: "cm1",
}
DETERMINISTIC_TIMESTAMP = "1970-01-01T00:00:00.000000Z"
SYNTHETIC_MESSAGE_NAMESPACE = "chatgpt-synthetic-message-v1"
SYNTHETIC_CONVERSATION_NAMESPACE = "chatgpt-synthetic-conversation-v1"

_CONVERSATIONS_RE = re.compile(r"conversations(?:-\d+)?\.json\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_EXTERNAL_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_INTERNAL_LOCATOR_RE = re.compile(
    r"^(?:sandbox:(?:/|file)|file-service://|sediment://|asset://)",
    re.IGNORECASE,
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MAX_PREFIX_BYTES = 8192

CancelCheck = Callable[[], bool]


class ChatGPTAdapterError(ValueError):
    """Fail-closed adapter error that exposes only a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class AdapterLimits:
    """Configurable source limits with bounded, conservative defaults."""

    max_entries: int = 100_000
    max_member_size: int = 2 * 1024 * 1024 * 1024
    max_total_uncompressed_size: int = 20 * 1024 * 1024 * 1024
    max_compression_ratio: int = 1000
    max_path_depth: int = 32
    max_json_bytes: int = 512 * 1024 * 1024
    max_json_depth: int = 64
    max_json_nodes: int = 5_000_000
    max_text_chars: int = 10_000_000
    stream_chunk_size: int = 1024 * 1024

    def __post_init__(self) -> None:
        for value in (
            self.max_entries,
            self.max_member_size,
            self.max_total_uncompressed_size,
            self.max_compression_ratio,
            self.max_path_depth,
            self.max_json_bytes,
            self.max_json_depth,
            self.max_json_nodes,
            self.max_text_chars,
            self.stream_chunk_size,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ChatGPTAdapterError("invalid_adapter_limit")
        if self.stream_chunk_size < 4096:
            raise ChatGPTAdapterError("invalid_stream_chunk_size")

    def archive_limits(self) -> ArchiveLimits:
        return ArchiveLimits(
            max_entries=self.max_entries,
            max_member_size=self.max_member_size,
            max_total_uncompressed_size=self.max_total_uncompressed_size,
            max_compression_ratio=self.max_compression_ratio,
            max_path_depth=self.max_path_depth,
        )


@dataclass(frozen=True)
class SourceProbe:
    archive_kind: str
    archive_sha256: str
    archive_size: int
    entry_count: int
    total_compressed_size: int
    total_uncompressed_size: int
    conversations_file_count: int
    source_locator_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_kind": self.archive_kind,
            "archive_sha256": self.archive_sha256,
            "archive_size": self.archive_size,
            "entry_count": self.entry_count,
            "total_compressed_size": self.total_compressed_size,
            "total_uncompressed_size": self.total_uncompressed_size,
            "conversations_file_count": self.conversations_file_count,
            "source_locator_kind": self.source_locator_kind,
        }


@dataclass(frozen=True)
class SourceAuditSummary:
    record_counts: Mapping[str, int]
    asset_counts: Mapping[str, int]
    warning_codes: tuple[str, ...]
    branch_policy: str = ACTIVE_PATH_COMPAT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "record_counts",
            MappingProxyType(dict(sorted(self.record_counts.items()))),
        )
        object.__setattr__(
            self,
            "asset_counts",
            MappingProxyType(dict(sorted(self.asset_counts.items()))),
        )
        object.__setattr__(
            self,
            "warning_codes",
            tuple(sorted(set(self.warning_codes))),
        )
        if self.branch_policy not in BRANCH_POLICIES:
            raise ChatGPTAdapterError("invalid_branch_policy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_counts": dict(self.record_counts),
            "asset_counts": dict(self.asset_counts),
            "warning_codes": list(self.warning_codes),
            "branch_policy": self.branch_policy,
        }


@dataclass(frozen=True)
class AttachmentOccurrence:
    """Path-free attachment occurrence inventory retained beside v8.3-B records."""

    source_type: str
    source_attachment_id: str = field(repr=False)
    source_conversation_id: str = field(repr=False)
    source_message_id: str = field(repr=False)
    identity_kind: str
    occurrence_index: int
    attachment_kind: str
    safe_display_filename: str = field(repr=False)
    declared_mime_type: str | None
    declared_size: int | None
    source_locator_kind: str
    source_locator_fingerprint: str
    entity_digest: str | None
    entity_state: str
    warning_codes: tuple[str, ...]
    public_media_id: str
    actual_size: int | None = None
    detected_mime_type: str | None = None
    content_sha256: str | None = None
    archive_member_fingerprint: str | None = None
    archive_member_name: str | None = field(default=None, repr=False, compare=False)
    archive_sha256: str | None = None
    mapping_method: str = "unresolved"
    mapping_confidence: str = "none"
    resolution_candidate_count: int = 0
    mapping_evidence_digest: str | None = None

    def semantic_descriptor(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_attachment_identity": normalized_json_digest(
                {"source_attachment_id": self.source_attachment_id}
            ),
            "source_message_identity": normalized_json_digest(
                {"source_message_id": self.source_message_id}
            ),
            "source_conversation_identity": normalized_json_digest(
                {"source_conversation_id": self.source_conversation_id}
            ),
            "identity_kind": self.identity_kind,
            "occurrence_index": self.occurrence_index,
            "attachment_kind": self.attachment_kind,
            "safe_display_filename_digest": normalized_json_digest(
                {"safe_display_filename": self.safe_display_filename}
            ),
            "declared_mime_type": self.declared_mime_type,
            "declared_size": self.declared_size,
            "source_locator_kind": self.source_locator_kind,
            "source_locator_fingerprint": self.source_locator_fingerprint,
            "entity_digest": self.entity_digest,
            "entity_state": self.entity_state,
            "warning_codes": list(self.warning_codes),
            "public_media_id": self.public_media_id,
            "actual_size": self.actual_size,
            "detected_mime_type": self.detected_mime_type,
            "content_sha256": self.content_sha256,
            "archive_member_fingerprint": self.archive_member_fingerprint,
            "mapping_method": self.mapping_method,
            "mapping_confidence": self.mapping_confidence,
            "resolution_candidate_count": self.resolution_candidate_count,
            "mapping_evidence_digest": self.mapping_evidence_digest,
        }


@dataclass(frozen=True)
class NormalizedBundle:
    batch: ImportBatch
    conversations: tuple[NormalizedConversation, ...]
    messages: tuple[NormalizedMessage, ...]
    attachments: tuple[NormalizedAttachment, ...]
    asset_entities: tuple[AssetEntity, ...]
    attachment_occurrences: tuple[AttachmentOccurrence, ...]
    audit_summary: SourceAuditSummary
    normalized_digest: str

    def summary(self) -> dict[str, Any]:
        return {
            "batch_mode": self.batch.batch_mode,
            "normalized_digest": self.normalized_digest,
            "conversations": len(self.conversations),
            "messages": len(self.messages),
            "logical_attachments": len(self.attachments),
            "attachment_occurrences": len(self.attachment_occurrences),
            "asset_entities": len(self.asset_entities),
            "warnings": len(self.audit_summary.warning_codes),
        }


@dataclass(frozen=True)
class TargetSnapshot:
    existing_records: tuple[ExistingRecord, ...]
    production_fingerprint: DatabaseFingerprint
    source_snapshot_complete: bool
    target_fingerprint: str

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        sources: dict[str, int] = {}
        for record in self.existing_records:
            counts[record.record_kind] = counts.get(record.record_kind, 0) + 1
            sources[record.source_type] = sources.get(record.source_type, 0) + 1
        return {
            "target_fingerprint": self.target_fingerprint,
            "existing_record_counts": dict(sorted(counts.items())),
            "source_counts": dict(sorted(sources.items())),
            "source_snapshot_complete": self.source_snapshot_complete,
        }


@dataclass(frozen=True)
class ChatGPTPlanningResult:
    manifest: SourceManifest
    bundle: NormalizedBundle
    target_snapshot: TargetSnapshot
    import_plan: ImportPlan
    adapter_warnings: tuple[str, ...]
    source_audit_summary: SourceAuditSummary
    semantic_plan_digest: str

    def summary(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest.manifest_id,
            "archive_kind": self.manifest.archive_kind,
            "batch_mode": self.bundle.batch.batch_mode,
            "target_fingerprint": self.target_snapshot.target_fingerprint,
            "plan_id": self.import_plan.plan_id,
            "semantic_plan_digest": self.semantic_plan_digest,
            "normalized_digest": self.bundle.normalized_digest,
            "counts": dict(self.import_plan.counts),
            "approval_required": self.import_plan.approval_required,
            "operation_executed": False,
            "warnings": list(self.adapter_warnings),
        }


@dataclass(frozen=True)
class _Entry:
    name: str
    compressed_size: int
    uncompressed_size: int
    signature: tuple[int, int, int, int] | None = None
    content_sha256: str | None = None


@dataclass(frozen=True)
class _LibraryAsset:
    file_id: str = field(repr=False)
    entry_name: str = field(repr=False)
    safe_filename: str = field(repr=False)
    declared_size: int | None
    declared_mime: str | None
    origination_message_id: str | None = field(default=None, repr=False)


@dataclass
class _SourceContext:
    source_path: Path = field(repr=False)
    archive_kind: str
    archive_sha256: str
    archive_size: int
    source_locator_kind: str
    entries: Mapping[str, _Entry]
    conversations_names: tuple[str, ...]
    conversations: tuple[Mapping[str, Any], ...]
    asset_name_map: Mapping[str, str]
    library_assets: Mapping[str, _LibraryAsset]
    warnings: tuple[str, ...]
    total_compressed_size: int
    total_uncompressed_size: int
    root_signature: tuple[int, int, int, int]
    entry_evidence_cache: dict[str, tuple[str, int, bytes]] = field(
        default_factory=dict,
        repr=False,
    )


@dataclass(frozen=True)
class _RawMessage:
    conversation_id: str
    node_id: str
    source_message_id: str
    identity_kind: str
    message: Mapping[str, Any]
    parent_source_message_id: str | None
    child_source_message_ids: tuple[str, ...]
    ordinal: int
    warning_codes: tuple[str, ...]
    hidden: bool


@dataclass(frozen=True)
class _GraphResult:
    conversation_id: str
    raw_conversation: Mapping[str, Any]
    active_messages: tuple[_RawMessage, ...]
    audit_messages: tuple[_RawMessage, ...]
    warning_codes: tuple[str, ...]
    mapping_nodes: int
    active_message_count: int
    branch_message_count: int
    empty_node_count: int
    orphan_node_count: int
    cycle_or_invalid_count: int


@dataclass(frozen=True)
class _PreparedMessage:
    raw: _RawMessage
    text: str
    content_type: str
    structured_parts: tuple[Mapping[str, Any], ...]
    attachment_descriptors: tuple[Mapping[str, Any], ...]
    warning_codes: tuple[str, ...]


@dataclass
class _ScanState:
    context: _SourceContext
    manifest: SourceManifest
    graphs: tuple[_GraphResult, ...]
    prepared_messages: Mapping[tuple[str, str], _PreparedMessage]
    occurrences: tuple[AttachmentOccurrence, ...]
    audit_summary: SourceAuditSummary


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _check_cancel(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ChatGPTAdapterError("scan_cancelled")


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        info.st_size,
        info.st_mtime_ns,
        getattr(info, "st_dev", 0),
        getattr(info, "st_ino", 0),
    )


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _checked_lstat(path: Path, *, allow_directory: bool) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError:
        raise ChatGPTAdapterError("source_unavailable") from None
    if _is_link(info):
        raise ChatGPTAdapterError("source_link_not_allowed")
    if allow_directory and stat.S_ISDIR(info.st_mode):
        return info
    if not stat.S_ISREG(info.st_mode):
        raise ChatGPTAdapterError("source_not_regular")
    return info


def _safe_member_name(raw_name: str, max_depth: int) -> str:
    if not isinstance(raw_name, str):
        raise ChatGPTAdapterError("unsafe_archive_member")
    try:
        raw_name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ChatGPTAdapterError("invalid_unicode") from None
    name = unicodedata.normalize("NFC", raw_name).replace("\\", "/")
    if (
        not name
        or name.startswith("/")
        or name.startswith("//")
        or _WINDOWS_DRIVE_RE.match(name)
        or name.startswith("\\\\?\\")
        or name.startswith("\\\\.\\")
        or "\x00" in name
    ):
        raise ChatGPTAdapterError("unsafe_archive_member")
    path = PurePosixPath(name)
    if (
        any(part in {"", ".", ".."} for part in path.parts)
        or len(path.parts) > max_depth
    ):
        raise ChatGPTAdapterError("unsafe_archive_member")
    return path.as_posix()


def _strict_json_loads(payload: bytes, limits: AdapterLimits) -> Any:
    if len(payload) > limits.max_json_bytes:
        raise ChatGPTAdapterError("json_size_limit_exceeded")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ChatGPTAdapterError("invalid_unicode") from None

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ChatGPTAdapterError("duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(_: str) -> Any:
        raise ChatGPTAdapterError("invalid_json_number")

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except ChatGPTAdapterError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise ChatGPTAdapterError("invalid_json") from None

    nodes = 0

    def normalize(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > limits.max_json_nodes:
            raise ChatGPTAdapterError("json_node_limit_exceeded")
        if depth > limits.max_json_depth:
            raise ChatGPTAdapterError("json_depth_limit_exceeded")
        if isinstance(item, str):
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise ChatGPTAdapterError("invalid_unicode") from None
            return unicodedata.normalize("NFC", item)
        if isinstance(item, dict):
            return {
                normalize(key, depth + 1): normalize(child, depth + 1)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [normalize(child, depth + 1) for child in item]
        if isinstance(item, float) and not math.isfinite(item):
            raise ChatGPTAdapterError("invalid_json_number")
        if item is None or isinstance(item, (bool, int, float)):
            return item
        raise ChatGPTAdapterError("unsupported_json_type")

    return normalize(value, 0)


def _read_bounded(handle: Any, maximum: int, cancel_check: CancelCheck | None) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        _check_cancel(cancel_check)
        chunk = handle.read(min(1024 * 1024, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise ChatGPTAdapterError("json_size_limit_exceeded")
    return b"".join(chunks)


def _zip_link(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(unix_mode)


def _discover_conversation_names(names: Iterable[str]) -> tuple[str, ...]:
    candidates = [
        name
        for name in names
        if _CONVERSATIONS_RE.fullmatch(PurePosixPath(name).name)
    ]
    base_names: set[str] = set()
    for name in candidates:
        base = PurePosixPath(name).name.casefold()
        if base in base_names:
            raise ChatGPTAdapterError("duplicate_conversations_source")
        base_names.add(base)
    if not candidates:
        raise ChatGPTAdapterError("conversations_json_missing")
    return tuple(sorted(candidates, key=lambda value: value.casefold()))


def _validate_conversation_documents(
    documents: Iterable[Any],
) -> tuple[Mapping[str, Any], ...]:
    conversations: list[Mapping[str, Any]] = []
    for document in documents:
        if not isinstance(document, list):
            raise ChatGPTAdapterError("invalid_conversations_document")
        for conversation in document:
            if not isinstance(conversation, Mapping):
                raise ChatGPTAdapterError("invalid_conversation_record")
            if "mapping" not in conversation:
                raise ChatGPTAdapterError("invalid_chatgpt_source")
            conversations.append(conversation)
    return tuple(conversations)


def _parse_library_assets(
    raw_value: Any,
    entries: Mapping[str, _Entry],
) -> tuple[dict[str, _LibraryAsset], tuple[str, ...]]:
    if raw_value is None:
        return {}, ()
    if not isinstance(raw_value, list):
        return {}, ("invalid_library_files",)
    assets: dict[str, _LibraryAsset] = {}
    warnings: set[str] = set()
    for raw in raw_value:
        if not isinstance(raw, Mapping):
            warnings.add("invalid_library_asset_record")
            continue
        file_id = raw.get("file_id")
        if (
            not isinstance(file_id, str)
            or not file_id.strip()
            or _looks_like_locator(file_id)
            or "/" in file_id
            or "\\" in file_id
        ):
            warnings.add("invalid_library_asset_id")
            continue
        file_id = unicodedata.normalize("NFC", file_id.strip())
        entry_name = file_id + ".dat"
        if entry_name not in entries:
            warnings.add("library_asset_member_missing")
            continue
        safe_filename = _safe_filename(raw.get("file_name"))
        declared_size = _declared_size(raw.get("file_size_bytes"))
        declared_mime = _declared_mime(raw.get("mime_type"))
        message_id = raw.get("origination_message_id")
        if not isinstance(message_id, str) or not message_id.strip():
            message_id = None
        else:
            message_id = unicodedata.normalize("NFC", message_id.strip())
        candidate = _LibraryAsset(
            file_id=file_id,
            entry_name=entry_name,
            safe_filename=safe_filename,
            declared_size=declared_size,
            declared_mime=declared_mime,
            origination_message_id=message_id,
        )
        previous = assets.get(file_id)
        if previous is not None and previous != candidate:
            raise ChatGPTAdapterError("duplicate_library_asset_identity")
        assets[file_id] = candidate
        if (
            declared_size is not None
            and declared_size != entries[entry_name].uncompressed_size
        ):
            warnings.add("library_asset_size_mismatch")
    return assets, tuple(sorted(warnings))


def _scan_zip(
    source: Path,
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
) -> _SourceContext:
    before = _checked_lstat(source, allow_directory=False)
    try:
        archive_sha256, archive_size = stream_file_sha256(
            source,
            chunk_size=limits.stream_chunk_size,
            cancel_check=cancel_check,
        )
    except IngestionCoreError as exc:
        raise ChatGPTAdapterError(exc.reason_code) from None
    entries: dict[str, _Entry] = {}
    folded_names: dict[str, str] = {}
    documents: list[Any] = []
    warnings: list[str] = []
    asset_name_map: dict[str, str] = {}
    library_assets: dict[str, _LibraryAsset] = {}
    try:
        with zipfile.ZipFile(source) as archive:
            metadata: list[ArchiveEntryMetadata] = []
            infos: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                _check_cancel(cancel_check)
                if info.is_dir():
                    continue
                name = _safe_member_name(info.filename, limits.max_path_depth)
                folded = name.casefold()
                if name in entries:
                    raise ChatGPTAdapterError("duplicate_archive_entry")
                if folded in folded_names and folded_names[folded] != name:
                    raise ChatGPTAdapterError("archive_case_collision")
                folded_names[folded] = name
                entry = _Entry(name, info.compress_size, info.file_size)
                entries[name] = entry
                infos[name] = info
                metadata.append(
                    ArchiveEntryMetadata(
                        member_name=name,
                        compressed_size=info.compress_size,
                        uncompressed_size=info.file_size,
                        is_link=_zip_link(info),
                    )
                )
            try:
                summary = validate_archive_metadata(metadata, limits.archive_limits())
            except IngestionCoreError as exc:
                raise ChatGPTAdapterError(exc.reason_code) from None
            conversation_names = _discover_conversation_names(entries)
            for name in conversation_names:
                with archive.open(infos[name], "r") as handle:
                    documents.append(
                        _strict_json_loads(
                            _read_bounded(handle, limits.max_json_bytes, cancel_check),
                            limits,
                        )
                    )
            mapping_names = [
                name
                for name in entries
                if PurePosixPath(name).name == "conversation_asset_file_names.json"
            ]
            if len(mapping_names) > 1:
                raise ChatGPTAdapterError("duplicate_asset_name_map")
            if mapping_names:
                name = mapping_names[0]
                with archive.open(infos[name], "r") as handle:
                    raw_mapping = _strict_json_loads(
                        _read_bounded(handle, limits.max_json_bytes, cancel_check),
                        limits,
                    )
                if isinstance(raw_mapping, Mapping) and all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in raw_mapping.items()
                ):
                    asset_name_map = dict(raw_mapping)
                else:
                    warnings.append("invalid_asset_name_map")
            library_names = [
                name
                for name in entries
                if PurePosixPath(name).name == "library_files.json"
            ]
            if len(library_names) > 1:
                raise ChatGPTAdapterError("duplicate_library_files")
            if library_names:
                name = library_names[0]
                with archive.open(infos[name], "r") as handle:
                    raw_library = _strict_json_loads(
                        _read_bounded(handle, limits.max_json_bytes, cancel_check),
                        limits,
                    )
                library_assets, library_warnings = _parse_library_assets(
                    raw_library,
                    entries,
                )
                warnings.extend(library_warnings)
    except ChatGPTAdapterError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise ChatGPTAdapterError("invalid_zip_archive") from None
    after = _checked_lstat(source, allow_directory=False)
    if _signature(before) != _signature(after):
        raise ChatGPTAdapterError("source_changed_during_scan")
    return _SourceContext(
        source_path=source,
        archive_kind="zip",
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        source_locator_kind="local_archive",
        entries=MappingProxyType(entries),
        conversations_names=conversation_names,
        conversations=_validate_conversation_documents(documents),
        asset_name_map=MappingProxyType(asset_name_map),
        library_assets=MappingProxyType(library_assets),
        warnings=tuple(sorted(set(warnings))),
        total_compressed_size=summary.total_compressed_size,
        total_uncompressed_size=summary.total_uncompressed_size,
        root_signature=_signature(after),
    )


def _iter_directory_entries(
    root: Path,
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
) -> Iterator[tuple[Path, str, os.stat_result]]:
    pending: list[tuple[Path, int]] = [(root, 0)]
    count = 0
    while pending:
        directory, depth = pending.pop()
        _check_cancel(cancel_check)
        try:
            children = sorted(
                os.scandir(directory),
                key=lambda item: unicodedata.normalize("NFC", item.name).casefold(),
            )
        except OSError:
            raise ChatGPTAdapterError("source_read_failed") from None
        for child in children:
            _check_cancel(cancel_check)
            child_path = Path(child.path)
            try:
                # On Windows ``DirEntry.stat`` can expose zeroed device/inode
                # fields while a later ``Path.lstat`` exposes real values.
                # Use one representation for both sides of the change gate.
                info = child_path.lstat()
            except OSError:
                raise ChatGPTAdapterError("source_changed_during_scan") from None
            if _is_link(info):
                raise ChatGPTAdapterError("source_link_not_allowed")
            relative = _safe_member_name(
                child_path.relative_to(root).as_posix(),
                limits.max_path_depth,
            )
            if stat.S_ISDIR(info.st_mode):
                if depth + 1 >= limits.max_path_depth:
                    raise ChatGPTAdapterError("archive_path_depth_exceeded")
                pending.append((child_path, depth + 1))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ChatGPTAdapterError("source_not_regular")
            count += 1
            if count > limits.max_entries:
                raise ChatGPTAdapterError("archive_entry_limit_exceeded")
            yield child_path, relative, info


def _scan_directory(
    source: Path,
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
) -> _SourceContext:
    before = _checked_lstat(source, allow_directory=True)
    if not stat.S_ISDIR(before.st_mode):
        raise ChatGPTAdapterError("source_not_directory")
    rows = list(_iter_directory_entries(source, limits, cancel_check))
    entries: dict[str, _Entry] = {}
    folded_names: dict[str, str] = {}
    digest_rows: list[dict[str, Any]] = []
    total = 0
    for file_path, name, info in rows:
        folded = name.casefold()
        if folded in folded_names and folded_names[folded] != name:
            raise ChatGPTAdapterError("archive_case_collision")
        folded_names[folded] = name
        if info.st_size > limits.max_member_size:
            raise ChatGPTAdapterError("archive_member_size_limit_exceeded")
        total += info.st_size
        if total > limits.max_total_uncompressed_size:
            raise ChatGPTAdapterError("archive_total_size_limit_exceeded")
        try:
            digest, size = stream_file_sha256(
                file_path,
                chunk_size=limits.stream_chunk_size,
                cancel_check=cancel_check,
            )
        except IngestionCoreError as exc:
            raise ChatGPTAdapterError(exc.reason_code) from None
        entry = _Entry(
            name=name,
            compressed_size=size,
            uncompressed_size=size,
            signature=_signature(info),
            content_sha256=digest,
        )
        entries[name] = entry
        digest_rows.append({"name": name, "size": size, "sha256": digest})
    conversation_names = _discover_conversation_names(entries)
    documents: list[Any] = []
    for name in conversation_names:
        entry = entries[name]
        if entry.uncompressed_size > limits.max_json_bytes:
            raise ChatGPTAdapterError("json_size_limit_exceeded")
        try:
            payload = (source / PurePosixPath(name)).read_bytes()
        except OSError:
            raise ChatGPTAdapterError("source_changed_during_scan") from None
        documents.append(_strict_json_loads(payload, limits))
    asset_name_map: dict[str, str] = {}
    library_assets: dict[str, _LibraryAsset] = {}
    warnings: list[str] = []
    mapping_names = [
        name
        for name in entries
        if PurePosixPath(name).name == "conversation_asset_file_names.json"
    ]
    if len(mapping_names) > 1:
        raise ChatGPTAdapterError("duplicate_asset_name_map")
    if mapping_names:
        try:
            mapping_payload = (source / PurePosixPath(mapping_names[0])).read_bytes()
        except OSError:
            raise ChatGPTAdapterError("source_changed_during_scan") from None
        raw_mapping = _strict_json_loads(mapping_payload, limits)
        if isinstance(raw_mapping, Mapping) and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_mapping.items()
        ):
            asset_name_map = dict(raw_mapping)
        else:
            warnings.append("invalid_asset_name_map")
    library_names = [
        name
        for name in entries
        if PurePosixPath(name).name == "library_files.json"
    ]
    if len(library_names) > 1:
        raise ChatGPTAdapterError("duplicate_library_files")
    if library_names:
        try:
            library_payload = (
                source / PurePosixPath(library_names[0])
            ).read_bytes()
        except OSError:
            raise ChatGPTAdapterError("source_changed_during_scan") from None
        raw_library = _strict_json_loads(library_payload, limits)
        library_assets, library_warnings = _parse_library_assets(
            raw_library,
            entries,
        )
        warnings.extend(library_warnings)
    for file_path, name, original_info in rows:
        try:
            current = file_path.lstat()
        except OSError:
            raise ChatGPTAdapterError("source_changed_during_scan") from None
        if _signature(current) != _signature(original_info) or _is_link(current):
            raise ChatGPTAdapterError("source_changed_during_scan")
    after = _checked_lstat(source, allow_directory=True)
    if _signature(before) != _signature(after):
        raise ChatGPTAdapterError("source_changed_during_scan")
    archive_sha256 = normalized_json_digest(
        {"directory_archive_protocol": ADAPTER_VERSION, "entries": digest_rows}
    )
    return _SourceContext(
        source_path=source,
        archive_kind="directory",
        archive_sha256=archive_sha256,
        archive_size=total,
        source_locator_kind="local_directory",
        entries=MappingProxyType(entries),
        conversations_names=conversation_names,
        conversations=_validate_conversation_documents(documents),
        asset_name_map=MappingProxyType(asset_name_map),
        library_assets=MappingProxyType(library_assets),
        warnings=tuple(sorted(set(warnings))),
        total_compressed_size=total,
        total_uncompressed_size=total,
        root_signature=_signature(after),
    )


def _scan_single_json(
    source: Path,
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
) -> _SourceContext:
    before = _checked_lstat(source, allow_directory=False)
    if before.st_size > limits.max_json_bytes:
        raise ChatGPTAdapterError("json_size_limit_exceeded")
    try:
        digest, size = stream_file_sha256(
            source,
            chunk_size=limits.stream_chunk_size,
            cancel_check=cancel_check,
        )
        payload = source.read_bytes()
    except IngestionCoreError as exc:
        raise ChatGPTAdapterError(exc.reason_code) from None
    except OSError:
        raise ChatGPTAdapterError("source_read_failed") from None
    document = _strict_json_loads(payload, limits)
    after = _checked_lstat(source, allow_directory=False)
    if _signature(before) != _signature(after):
        raise ChatGPTAdapterError("source_changed_during_scan")
    return _SourceContext(
        source_path=source,
        archive_kind="single_json",
        archive_sha256=digest,
        archive_size=size,
        source_locator_kind="local_file",
        entries=MappingProxyType(
            {
                "conversations.json": _Entry(
                    "conversations.json",
                    size,
                    size,
                    _signature(after),
                    digest,
                )
            }
        ),
        conversations_names=("conversations.json",),
        conversations=_validate_conversation_documents((document,)),
        asset_name_map=MappingProxyType({}),
        library_assets=MappingProxyType({}),
        warnings=(),
        total_compressed_size=size,
        total_uncompressed_size=size,
        root_signature=_signature(after),
    )


def _load_source(
    source: str | os.PathLike[str],
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
) -> _SourceContext:
    path = Path(source)
    info = _checked_lstat(path, allow_directory=True)
    _check_cancel(cancel_check)
    if stat.S_ISDIR(info.st_mode):
        return _scan_directory(path, limits, cancel_check)
    try:
        is_zip = zipfile.is_zipfile(path)
    except OSError:
        raise ChatGPTAdapterError("source_read_failed") from None
    if is_zip:
        return _scan_zip(path, limits, cancel_check)
    return _scan_single_json(path, limits, cancel_check)


def _conversation_id(conversation: Mapping[str, Any]) -> tuple[str, str]:
    raw = conversation.get("conversation_id") or conversation.get("id")
    if isinstance(raw, str) and raw.strip():
        return unicodedata.normalize("NFC", raw.strip()), "native"
    digest = normalized_json_digest(
        {
            "namespace": SYNTHETIC_CONVERSATION_NAMESPACE,
            "mapping": conversation.get("mapping"),
            "created_at": conversation.get("create_time"),
            "updated_at": conversation.get("update_time"),
        }
    )
    return f"{SYNTHETIC_CONVERSATION_NAMESPACE}:{digest}", "synthetic"


def _raw_timestamp_number(value: Any) -> float:
    if isinstance(value, bool):
        return float("-inf")
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return float("-inf")
        return numeric if math.isfinite(numeric) else float("-inf")
    return float("-inf")


def _hidden_message(message: Mapping[str, Any]) -> bool:
    metadata = message.get("metadata")
    status = str(message.get("status") or "").strip().lower()
    return bool(
        isinstance(metadata, Mapping)
        and (
            metadata.get("is_visually_hidden_from_conversation") is True
            or metadata.get("is_hidden") is True
        )
    ) or status in {"deleted", "hidden"}


def _graph_conversation(conversation: Mapping[str, Any]) -> _GraphResult:
    conversation_id, _ = _conversation_id(conversation)
    mapping = conversation.get("mapping")
    if not isinstance(mapping, Mapping):
        raise ChatGPTAdapterError("invalid_conversation_mapping")
    normalized_mapping: dict[str, Mapping[str, Any]] = {}
    warnings: set[str] = set()
    invalid_relations = 0
    for raw_node_id, raw_node in mapping.items():
        if not isinstance(raw_node_id, str) or not isinstance(raw_node, Mapping):
            warnings.add("invalid_mapping_node")
            invalid_relations += 1
            continue
        normalized_mapping[unicodedata.normalize("NFC", raw_node_id)] = raw_node
    parent_by_node: dict[str, str | None] = {}
    inferred_children: dict[str, set[str]] = {
        node_id: set() for node_id in normalized_mapping
    }
    missing_parent_nodes: set[str] = set()
    for node_id, node in normalized_mapping.items():
        parent = node.get("parent")
        if parent is not None and not isinstance(parent, str):
            parent = None
            warnings.add("invalid_parent_relation")
            invalid_relations += 1
        parent = unicodedata.normalize("NFC", parent) if isinstance(parent, str) else None
        parent_by_node[node_id] = parent
        if parent is not None:
            if parent in normalized_mapping:
                inferred_children[parent].add(node_id)
            else:
                missing_parent_nodes.add(node_id)
                warnings.add("missing_parent")
    for node_id, node in normalized_mapping.items():
        declared = node.get("children")
        if declared is None:
            declared = []
        if not isinstance(declared, list) or any(
            not isinstance(child, str) for child in declared
        ):
            warnings.add("invalid_children_relation")
            invalid_relations += 1
            continue
        declared_set = {
            unicodedata.normalize("NFC", child) for child in declared
        }
        if declared_set != inferred_children[node_id]:
            warnings.add("parent_children_mismatch")
            invalid_relations += 1

    cycle_nodes: set[str] = set()
    for start in normalized_mapping:
        path: list[str] = []
        positions: dict[str, int] = {}
        cursor: str | None = start
        while cursor is not None and cursor in normalized_mapping:
            if cursor in positions:
                cycle_nodes.update(path[positions[cursor] :])
                break
            positions[cursor] = len(path)
            path.append(cursor)
            cursor = parent_by_node.get(cursor)
    if cycle_nodes:
        warnings.add("conversation_cycle")

    effective_ids: dict[str, str | None] = {}
    identity_kinds: dict[str, str] = {}
    seen_synthetic: dict[str, str] = {}
    duplicate_message_ids: set[str] = set()
    seen_message_ids: set[str] = set()
    for node_id, node in normalized_mapping.items():
        message = node.get("message")
        if not isinstance(message, Mapping):
            effective_ids[node_id] = None
            continue
        raw_message_id = message.get("id")
        if isinstance(raw_message_id, str) and raw_message_id.strip():
            source_id = unicodedata.normalize("NFC", raw_message_id.strip())
            kind = "native"
            if node_id and source_id != node_id:
                warnings.add("message_node_id_mismatch")
        elif node_id:
            source_id = node_id
            kind = "mapping_fallback"
        else:
            digest = normalized_json_digest(
                {
                    "namespace": SYNTHETIC_MESSAGE_NAMESPACE,
                    "conversation_id": conversation_id,
                    "parent": node.get("parent"),
                    "children": sorted(node.get("children") or []),
                    "message": message,
                }
            )
            source_id = f"{SYNTHETIC_MESSAGE_NAMESPACE}:{digest}"
            kind = "synthetic"
            previous = seen_synthetic.get(source_id)
            if previous is not None and previous != node_id:
                raise ChatGPTAdapterError("synthetic_message_identity_collision")
            seen_synthetic[source_id] = node_id
        effective_ids[node_id] = source_id
        identity_kinds[node_id] = kind
        if source_id in seen_message_ids:
            duplicate_message_ids.add(source_id)
        seen_message_ids.add(source_id)
    if duplicate_message_ids:
        warnings.add("duplicate_message_identity")

    current_node = conversation.get("current_node")
    if not isinstance(current_node, str) or current_node not in normalized_mapping:
        leaves = [
            node_id
            for node_id, node in normalized_mapping.items()
            if isinstance(node.get("message"), Mapping)
            and not inferred_children[node_id]
        ]
        if not leaves:
            leaves = [
                node_id
                for node_id, node in normalized_mapping.items()
                if isinstance(node.get("message"), Mapping)
            ]
        if leaves:
            current_node = max(
                leaves,
                key=lambda node_id: (
                    _raw_timestamp_number(
                        (normalized_mapping[node_id].get("message") or {}).get(
                            "create_time"
                        )
                    ),
                    effective_ids.get(node_id) or "",
                ),
            )
            warnings.add("current_node_fallback")
        else:
            current_node = None
    chain: list[str] = []
    seen_chain: set[str] = set()
    cursor = current_node
    while isinstance(cursor, str) and cursor in normalized_mapping:
        if cursor in seen_chain:
            warnings.add("active_path_cycle")
            break
        seen_chain.add(cursor)
        chain.append(cursor)
        parent = parent_by_node.get(cursor)
        if parent is not None and parent not in normalized_mapping:
            break
        cursor = parent
    active_node_order = list(reversed(chain))
    active_visible_nodes = [
        node_id
        for node_id in active_node_order
        if isinstance(normalized_mapping[node_id].get("message"), Mapping)
        and not _hidden_message(normalized_mapping[node_id]["message"])
    ]

    def nearest_parent_message(node_id: str) -> str | None:
        cursor = parent_by_node.get(node_id)
        visited: set[str] = set()
        while cursor is not None and cursor in normalized_mapping and cursor not in visited:
            visited.add(cursor)
            source_id = effective_ids.get(cursor)
            if source_id is not None:
                return source_id
            cursor = parent_by_node.get(cursor)
        return None

    def child_message_ids(node_id: str, allowed_nodes: set[str]) -> tuple[str, ...]:
        found: list[str] = []
        pending = sorted(inferred_children.get(node_id, ()))
        visited: set[str] = set()
        while pending:
            child = pending.pop(0)
            if child in visited:
                continue
            visited.add(child)
            if child in allowed_nodes and effective_ids.get(child):
                found.append(str(effective_ids[child]))
            else:
                pending.extend(sorted(inferred_children.get(child, ())))
        return tuple(sorted(set(found)))

    all_visible_nodes = {
        node_id
        for node_id, node in normalized_mapping.items()
        if isinstance(node.get("message"), Mapping)
        and not _hidden_message(node["message"])
    }
    active_set = set(active_visible_nodes)

    def make_raw(node_id: str, ordinal: int, allowed: set[str]) -> _RawMessage:
        node = normalized_mapping[node_id]
        message = node["message"]
        item_warnings: set[str] = set()
        source_id = effective_ids[node_id]
        if identity_kinds.get(node_id) == "mapping_fallback":
            item_warnings.add("message_id_missing_mapping_fallback")
        if identity_kinds.get(node_id) == "synthetic":
            item_warnings.add("synthetic_message_identity")
        raw_message_id = message.get("id")
        if (
            isinstance(raw_message_id, str)
            and raw_message_id.strip()
            and node_id
            and raw_message_id.strip() != node_id
        ):
            item_warnings.add("message_node_id_mismatch")
        if message.get("create_time") is None:
            item_warnings.add("message_created_at_missing")
        return _RawMessage(
            conversation_id=conversation_id,
            node_id=node_id,
            source_message_id=str(source_id),
            identity_kind=identity_kinds[node_id],
            message=message,
            parent_source_message_id=nearest_parent_message(node_id),
            child_source_message_ids=child_message_ids(node_id, allowed),
            ordinal=ordinal,
            warning_codes=tuple(sorted(item_warnings)),
            hidden=_hidden_message(message),
        )

    active_messages = tuple(
        make_raw(node_id, ordinal, active_set)
        for ordinal, node_id in enumerate(active_visible_nodes)
    )
    audit_order = sorted(
        all_visible_nodes,
        key=lambda node_id: (
            _raw_timestamp_number(normalized_mapping[node_id]["message"].get("create_time")),
            effective_ids.get(node_id) or "",
        ),
    )
    audit_messages = tuple(
        make_raw(node_id, ordinal, all_visible_nodes)
        for ordinal, node_id in enumerate(audit_order)
    )
    branch_count = max(0, len(audit_messages) - len(active_messages))
    if branch_count:
        warnings.add("branch_messages_excluded")
    hidden_count = sum(
        isinstance(node.get("message"), Mapping)
        and _hidden_message(node["message"])
        for node in normalized_mapping.values()
    )
    if hidden_count:
        warnings.add("hidden_messages_excluded")
    empty_count = sum(
        not isinstance(node.get("message"), Mapping)
        for node in normalized_mapping.values()
    )
    return _GraphResult(
        conversation_id=conversation_id,
        raw_conversation=conversation,
        active_messages=active_messages,
        audit_messages=audit_messages,
        warning_codes=tuple(sorted(warnings)),
        mapping_nodes=len(normalized_mapping),
        active_message_count=len(active_messages),
        branch_message_count=branch_count,
        empty_node_count=empty_count,
        orphan_node_count=len(missing_parent_nodes),
        cycle_or_invalid_count=len(cycle_nodes) + invalid_relations,
    )


def _safe_token(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        candidate = value.strip().lower().replace(" ", "_")
        if _TOKEN_RE.fullmatch(candidate):
            return candidate
    return fallback


def _normalize_text(value: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ChatGPTAdapterError("invalid_unicode") from None
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _looks_like_locator(value: str) -> bool:
    stripped = value.strip()
    return bool(
        _INTERNAL_LOCATOR_RE.match(stripped)
        or _EXTERNAL_URL_RE.match(stripped)
        or stripped.startswith("\\\\")
        or _WINDOWS_DRIVE_RE.match(stripped)
    )


def _safe_filename(value: Any) -> str:
    if not isinstance(value, str):
        return "attachment.bin"
    normalized = unicodedata.normalize("NFC", value).replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].strip()
    name = "".join(
        character
        for character in name
        if character >= " " and character not in '<>:"/\\|?*'
    )
    return (name[:255] or "attachment.bin")


def _declared_size(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _declared_mime(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if "/" not in normalized or len(normalized) > 255:
        return None
    if any(character.isspace() or ord(character) < 32 for character in normalized):
        return None
    return normalized


def _attachment_descriptor(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_type = raw.get("type") or raw.get("content_type") or raw.get("kind")
    locator = (
        raw.get("asset_pointer")
        or raw.get("sandbox_path")
        or raw.get("file_path")
        or raw.get("url")
        or raw.get("download_url")
    )
    filename = raw.get("file_name") or raw.get("filename") or raw.get("name")
    mime = (
        raw.get("mime_type")
        or raw.get("file_type")
        or (
            raw.get("content_type")
            if isinstance(raw.get("content_type"), str)
            and "/" in raw.get("content_type", "")
            else None
        )
    )
    size = raw.get("file_size")
    if size is None:
        size = raw.get("size_bytes")
    if size is None:
        size = raw.get("size")
    native_id = (
        raw.get("attachment_id")
        or raw.get("file_id")
        or raw.get("asset_id")
        or raw.get("id")
    )
    attachment_type = str(raw_type or "").lower()
    looks_like_attachment = bool(
        locator
        or filename
        or mime
        or size is not None
        or native_id
        or any(
            token in attachment_type
            for token in ("file", "image", "audio", "attachment", "asset_pointer")
        )
    )
    if not looks_like_attachment:
        return None
    descriptor: dict[str, Any] = {}
    if (
        isinstance(native_id, str)
        and native_id.strip()
        and not _looks_like_locator(native_id)
        and "/" not in native_id
        and "\\" not in native_id
    ):
        descriptor["id"] = unicodedata.normalize("NFC", native_id.strip())
    if isinstance(filename, str):
        descriptor["file_name"] = unicodedata.normalize("NFC", filename)
    declared_mime = _declared_mime(mime)
    if declared_mime:
        descriptor["file_type"] = declared_mime
    declared_size = _declared_size(size)
    if declared_size is not None:
        descriptor["file_size"] = declared_size
    extracted = raw.get("extracted_content")
    if isinstance(extracted, str):
        descriptor["extracted_content"] = _normalize_text(extracted)
    if isinstance(locator, str):
        descriptor["_locator"] = unicodedata.normalize("NFC", locator.strip())
    declared_hash = (
        raw.get("sha256")
        or raw.get("content_sha256")
        or raw.get("content_hash")
        or raw.get("checksum")
    )
    if (
        isinstance(declared_hash, str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", declared_hash.strip())
    ):
        descriptor["content_sha256"] = declared_hash.strip().lower()
    descriptor["_attachment_kind"] = _safe_token(
        attachment_type, "attachment"
    )
    descriptor["_descriptor_digest"] = normalized_json_digest(
        {
            key: value
            for key, value in descriptor.items()
            if not key.startswith("_")
        }
    )
    return descriptor


def _legacy_import_text(content: Mapping[str, Any]) -> str:
    parts = content.get("parts")
    values: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, str):
                value = part
            elif isinstance(part, Mapping):
                if isinstance(part.get("text"), str):
                    value = part["text"]
                elif isinstance(part.get("content"), str):
                    value = part["content"]
                elif part.get("name") or part.get("filename"):
                    value = (
                        "[非文本内容/附件："
                        + str(part.get("name") or part.get("filename"))
                        + "]"
                    )
                else:
                    value = "[非文本内容]"
            elif part is not None:
                value = str(part)
            else:
                continue
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise ChatGPTAdapterError("invalid_unicode") from None
            values.append(value)
    if not values and isinstance(content.get("text"), str):
        values.append(content["text"])
    content_type = content.get("content_type")
    if not values and content_type and content_type != "text":
        values.append("[" + str(content_type) + "]")
    return "\n".join(value for value in values if value).strip()


def _parse_message_content(
    raw_message: Mapping[str, Any],
    limits: AdapterLimits,
    *,
    text_policy: str = STRUCTURED_TEXT_V1,
) -> tuple[str, str, tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    if text_policy not in TEXT_POLICIES:
        raise ChatGPTAdapterError("invalid_text_policy")
    content = raw_message.get("content")
    if not isinstance(content, Mapping):
        content = {}
    content_type = _safe_token(content.get("content_type"), "unknown")
    text_parts: list[str] = []
    structured: list[Mapping[str, Any]] = []
    attachments: list[Mapping[str, Any]] = []
    warnings: set[str] = set()
    parts = content.get("parts")
    if not isinstance(parts, list):
        if isinstance(content.get("text"), str):
            parts = [content["text"]]
        elif parts is None:
            parts = []
        else:
            parts = [parts]

    for part in parts:
        if isinstance(part, str):
            normalized = _normalize_text(part)
            if _looks_like_locator(normalized):
                descriptor = _attachment_descriptor({"asset_pointer": normalized})
                if descriptor:
                    attachments.append(descriptor)
                    structured.append(
                        {
                            "part_type": "file_reference",
                            "descriptor_digest": descriptor["_descriptor_digest"],
                        }
                    )
                continue
            text_parts.append(normalized)
            structured.append({"part_type": "text", "text": normalized})
            continue
        if isinstance(part, Mapping):
            descriptor = _attachment_descriptor(part)
            part_type = _safe_token(
                part.get("type") or part.get("content_type"),
                "unknown",
            )
            visible = part.get("text")
            if not isinstance(visible, str):
                visible = part.get("content")
            if isinstance(visible, str):
                normalized_visible = _normalize_text(visible)
                text_parts.append(normalized_visible)
            else:
                normalized_visible = None
            if descriptor is not None:
                attachments.append(descriptor)
                structured.append(
                    {
                        "part_type": (
                            "image_asset_pointer"
                            if "image" in part_type
                            or "asset_pointer" in part_type
                            else "file"
                        ),
                        "descriptor_digest": descriptor["_descriptor_digest"],
                    }
                )
            elif part_type in {
                "text",
                "code",
                "tool_output",
                "audio",
                "multimodal_text",
                "computer_output",
            }:
                item: dict[str, Any] = {"part_type": part_type}
                if normalized_visible is not None:
                    item["content"] = normalized_visible
                language = part.get("language")
                if isinstance(language, str):
                    item["language"] = _safe_token(language, "unknown")
                structured.append(item)
            else:
                structured.append(
                    {
                        "part_type": "unsupported",
                        "descriptor_digest": normalized_json_digest(part),
                    }
                )
                warnings.add("unsupported_content_part")
            continue
        if part is not None:
            structured.append(
                {
                    "part_type": "unsupported",
                    "descriptor_digest": normalized_json_digest(
                        {"value_type": type(part).__name__, "value": part}
                    ),
                }
            )
            warnings.add("unsupported_content_part")

    metadata = raw_message.get("metadata")
    if isinstance(metadata, Mapping):
        for key in (
            "attachments",
            "files",
            "content_references",
            "image_gen_attachments",
            "dalle_attachments",
        ):
            raw_items = metadata.get(key)
            if isinstance(raw_items, Mapping):
                raw_items = [raw_items]
            if not isinstance(raw_items, list):
                continue
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    warnings.add("unsupported_attachment_descriptor")
                    continue
                descriptor = _attachment_descriptor(raw_item)
                if descriptor is None:
                    warnings.add("unsupported_attachment_descriptor")
                    continue
                attachments.append(descriptor)
                structured.append(
                    {
                        "part_type": "attachment_metadata",
                        "descriptor_digest": descriptor["_descriptor_digest"],
                    }
                )
    text = (
        _legacy_import_text(content)
        if text_policy == LEGACY_IMPORT_TEXT_V1
        else "\n".join(text_parts)
    )
    if len(text) > limits.max_text_chars:
        raise ChatGPTAdapterError("message_text_limit_exceeded")
    return (
        text,
        content_type,
        tuple(structured),
        tuple(attachments),
        tuple(sorted(warnings)),
    )


def _locator_kind(locator: str | None) -> str:
    if not locator:
        return "metadata_only"
    if _EXTERNAL_URL_RE.match(locator):
        return "external_url"
    if locator.lower().startswith("sandbox:"):
        return "sandbox_file"
    if "://" in locator:
        return "asset_pointer"
    return "archive_member"


def _locator_fingerprint(
    kind: str,
    locator: str | None,
    filename: str,
    native_id: str | None,
) -> str:
    locator_value = locator or ""
    if kind == "external_url":
        try:
            parsed = urlsplit(locator_value)
            safe_locator = {
                "scheme": parsed.scheme.lower(),
                "host_digest": normalized_json_digest(
                    {"host": parsed.hostname or ""}
                ),
                "url_digest": normalized_json_digest({"url": locator_value}),
            }
        except ValueError:
            safe_locator = {"url_digest": normalized_json_digest({"url": locator_value})}
    else:
        safe_locator = {
            "locator_digest": normalized_json_digest({"locator": locator_value})
        }
    return normalized_json_digest(
        {
            "locator_protocol": ADAPTER_VERSION,
            "locator_kind": kind,
            "locator": safe_locator,
            "filename_digest": normalized_json_digest({"filename": filename}),
            "native_id_digest": (
                normalized_json_digest({"native_id": native_id})
                if native_id
                else None
            ),
        }
    )


@dataclass(frozen=True)
class _AssetResolution:
    state: str
    entry_name: str | None = field(default=None, repr=False)
    mapping_method: str = "unresolved"
    confidence: str = "none"
    candidate_fingerprints: tuple[str, ...] = ()


def _entry_fingerprint(entry_name: str) -> str:
    return normalized_json_digest({"archive_member": entry_name})


def _canonical_locator_values(locator: str | None) -> tuple[str, ...]:
    if not locator:
        return ()
    try:
        value = unicodedata.normalize("NFC", unquote(locator.strip()))
    except (UnicodeError, ValueError):
        return ()
    value = value.replace("\\", "/")
    values: list[str] = []
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.scheme:
        if parsed.netloc:
            values.append(parsed.netloc)
        if parsed.path:
            values.append(parsed.path)
    else:
        values.append(value.removeprefix("sandbox:"))
    canonical: list[str] = []
    for item in values:
        normalized = unicodedata.normalize("NFC", item).replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        if (
            not normalized
            or any(part in {"", ".", ".."} for part in PurePosixPath(normalized).parts)
        ):
            continue
        canonical.extend((normalized, PurePosixPath(normalized).name))
    return tuple(dict.fromkeys(canonical))


def _library_assets_for_message(
    context: _SourceContext,
    message_id: str,
) -> tuple[_LibraryAsset, ...]:
    return tuple(
        sorted(
            (
                item
                for item in context.library_assets.values()
                if item.origination_message_id == message_id
            ),
            key=lambda item: item.file_id,
        )
    )


def _filter_asset_candidates(
    context: _SourceContext,
    candidates: Iterable[str],
    descriptor: Mapping[str, Any],
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
) -> tuple[str, ...]:
    result = sorted(set(candidates))
    declared_size = _declared_size(descriptor.get("file_size"))
    declared_mime = _declared_mime(descriptor.get("file_type"))
    declared_hash = descriptor.get("content_sha256")
    if declared_size is not None:
        result = [
            name
            for name in result
            if context.entries[name].uncompressed_size == declared_size
        ]
    if declared_mime is not None or isinstance(declared_hash, str):
        filtered: list[str] = []
        for name in result:
            content_sha256, _, prefix = _stream_entry_evidence(
                context,
                name,
                limits,
                cancel_check,
            )
            display_name = (
                context.asset_name_map.get(name)
                or next(
                    (
                        item.safe_filename
                        for item in context.library_assets.values()
                        if item.entry_name == name
                    ),
                    name,
                )
            )
            detected_mime = _detect_candidate_mime(
                prefix,
                display_name,
            )
            if declared_mime is not None and detected_mime != declared_mime:
                continue
            if isinstance(declared_hash, str) and content_sha256 != declared_hash:
                continue
            filtered.append(name)
        result = filtered
    return tuple(result)


def _resolution(
    *,
    context: _SourceContext,
    descriptor: Mapping[str, Any],
    message_id: str,
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
) -> _AssetResolution:
    locator = descriptor.get("_locator")
    locator = locator if isinstance(locator, str) else None
    filename = descriptor.get("file_name")
    filename = filename if isinstance(filename, str) else None
    native_id = descriptor.get("id")
    native_id = native_id if isinstance(native_id, str) else None
    kind = _locator_kind(locator)
    if kind == "external_url":
        return _AssetResolution(
            state="external_url_blocked",
            mapping_method="external_url_blocked",
        )
    entry_names = set(context.entries)
    folded_names = {
        name.casefold(): name
        for name in entry_names
    }

    exact_path_matches: set[str] = set()
    for value in _canonical_locator_values(locator):
        if value in entry_names:
            exact_path_matches.add(value)
        elif value.casefold() in folded_names:
            exact_path_matches.add(folded_names[value.casefold()])
    if len(exact_path_matches) == 1:
        return _AssetResolution(
            state="verified",
            entry_name=next(iter(exact_path_matches)),
            mapping_method="explicit_archive_relative_path",
            confidence="exact",
        )
    if len(exact_path_matches) > 1:
        return _AssetResolution(
            state="ambiguous",
            mapping_method="ambiguous_explicit_archive_path",
            candidate_fingerprints=tuple(
                _entry_fingerprint(item)
                for item in sorted(exact_path_matches)
            ),
        )

    file_id_matches: set[str] = set()
    file_id_values = ([native_id] if native_id else []) + list(
        _canonical_locator_values(locator)
    )
    for value in file_id_values:
        token = PurePosixPath(value).name
        library = context.library_assets.get(token)
        if library is not None:
            file_id_matches.add(library.entry_name)
        for candidate in (token, token + ".dat"):
            if candidate in entry_names:
                file_id_matches.add(candidate)
    if len(file_id_matches) == 1:
        return _AssetResolution(
            state="verified",
            entry_name=next(iter(file_id_matches)),
            mapping_method="explicit_file_or_upload_id",
            confidence="exact",
        )
    if len(file_id_matches) > 1:
        return _AssetResolution(
            state="ambiguous",
            mapping_method="ambiguous_explicit_file_id",
            candidate_fingerprints=tuple(
                _entry_fingerprint(item)
                for item in sorted(file_id_matches)
            ),
        )

    message_candidates = _filter_asset_candidates(
        context,
        (
            item.entry_name
            for item in _library_assets_for_message(context, message_id)
            if (
                filename is None
                or item.safe_filename.casefold()
                == _safe_filename(filename).casefold()
            )
        ),
        descriptor,
        limits,
        cancel_check,
    )
    if len(message_candidates) == 1:
        return _AssetResolution(
            state="verified",
            entry_name=message_candidates[0],
            mapping_method="official_library_message_relation",
            confidence="exact",
        )
    if len(message_candidates) > 1:
        return _AssetResolution(
            state="ambiguous",
            mapping_method="ambiguous_library_message_relation",
            candidate_fingerprints=tuple(
                _entry_fingerprint(item) for item in message_candidates
            ),
        )

    name_candidates: tuple[str, ...] = ()
    if filename:
        normalized_filename = unicodedata.normalize("NFC", filename).casefold()
        raw_candidates = tuple(
            asset_file
            for asset_file, original_name in context.asset_name_map.items()
            if unicodedata.normalize("NFC", original_name).casefold()
            == normalized_filename
        )
        filtered_candidates = _filter_asset_candidates(
            context,
            raw_candidates,
            descriptor,
            limits,
            cancel_check,
        )
        if len(filtered_candidates) == 1:
            method = (
                "official_asset_name_map_exact"
                if len(raw_candidates) == 1
                else "unique_basename_size_mime_or_hash"
            )
            return _AssetResolution(
                state="verified",
                entry_name=filtered_candidates[0],
                mapping_method=method,
                confidence="exact" if len(raw_candidates) == 1 else "corroborated",
            )
        name_candidates = filtered_candidates or raw_candidates
        if len(name_candidates) > 1:
            return _AssetResolution(
                state="ambiguous",
                mapping_method="ambiguous_asset_name_map",
                candidate_fingerprints=tuple(
                    _entry_fingerprint(item) for item in sorted(name_candidates)
                ),
            )

    if not locator:
        return _AssetResolution(
            state="metadata_only",
            mapping_method="metadata_only_no_locator",
        )
    return _AssetResolution(
        state="missing",
        mapping_method="locator_not_found",
    )


def _detect_magic_mime(prefix: bytes) -> str | None:
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
    return None


def _detect_mime(prefix: bytes) -> str:
    magic = _detect_magic_mime(prefix)
    if magic is not None:
        return magic
    if prefix:
        try:
            text = prefix.decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream"
        if all(
            ord(character) >= 32 or character in "\r\n\t"
            for character in text
        ):
            return "text/plain"
    return "application/octet-stream"


def _detect_candidate_mime(prefix: bytes, filename: str) -> str:
    magic = _detect_magic_mime(prefix)
    if magic is not None:
        return magic
    guessed, _ = mimetypes.guess_type(filename, strict=False)
    return (guessed or "application/octet-stream").lower()


def _stream_entry_evidence(
    context: _SourceContext,
    entry_name: str,
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
) -> tuple[str, int, bytes]:
    cached = context.entry_evidence_cache.get(entry_name)
    if cached is not None:
        return cached
    entry = context.entries[entry_name]
    digest = hashlib.sha256()
    total = 0
    prefix = bytearray()
    try:
        if context.archive_kind == "zip":
            with zipfile.ZipFile(context.source_path) as archive:
                with archive.open(entry_name, "r") as handle:
                    while True:
                        _check_cancel(cancel_check)
                        chunk = handle.read(limits.stream_chunk_size)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limits.max_member_size:
                            raise ChatGPTAdapterError(
                                "archive_member_size_limit_exceeded"
                            )
                        digest.update(chunk)
                        if len(prefix) < _MAX_PREFIX_BYTES:
                            prefix.extend(
                                chunk[: _MAX_PREFIX_BYTES - len(prefix)]
                            )
        elif context.archive_kind == "directory":
            path = context.source_path / PurePosixPath(entry_name)
            before = _checked_lstat(path, allow_directory=False)
            with path.open("rb") as handle:
                while True:
                    _check_cancel(cancel_check)
                    chunk = handle.read(limits.stream_chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limits.max_member_size:
                        raise ChatGPTAdapterError(
                            "archive_member_size_limit_exceeded"
                        )
                    digest.update(chunk)
                    if len(prefix) < _MAX_PREFIX_BYTES:
                        prefix.extend(chunk[: _MAX_PREFIX_BYTES - len(prefix)])
            after = _checked_lstat(path, allow_directory=False)
            if _signature(before) != _signature(after):
                raise ChatGPTAdapterError("source_changed_during_scan")
        else:
            raise ChatGPTAdapterError("asset_entity_unavailable")
    except ChatGPTAdapterError:
        raise
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile):
        raise ChatGPTAdapterError("asset_read_failed") from None
    if total != entry.uncompressed_size:
        raise ChatGPTAdapterError("archive_member_size_changed")
    result = (digest.hexdigest(), total, bytes(prefix))
    context.entry_evidence_cache[entry_name] = result
    return result


def _resolve_occurrence(
    *,
    context: _SourceContext,
    descriptor: Mapping[str, Any],
    conversation_id: str,
    message_id: str,
    occurrence_index: int,
    limits: AdapterLimits,
    cancel_check: CancelCheck | None,
    asset_mime_policy: str,
) -> AttachmentOccurrence:
    media_descriptor = {
        key: descriptor[key]
        for key in ("id", "file_name", "file_type", "file_size", "extracted_content")
        if key in descriptor
    }
    try:
        source_id, identity_kind, _ = derive_source_attachment_identity(
            SOURCE_TYPE,
            conversation_id,
            message_id,
            media_descriptor,
        )
    except MediaIndexError:
        raise ChatGPTAdapterError("attachment_identity_failed") from None
    if not source_id:
        raise ChatGPTAdapterError("attachment_identity_missing")
    try:
        public_media_id = derive_media_id(source_id, SOURCE_TYPE)
    except MediaIndexError:
        raise ChatGPTAdapterError("media_identity_failed") from None
    locator = descriptor.get("_locator")
    locator = locator if isinstance(locator, str) else None
    kind = _locator_kind(locator)
    original_filename = descriptor.get("file_name")
    display_filename = _safe_filename(
        original_filename
        or (PurePosixPath(locator).name if locator and "://" not in locator else None)
    )
    declared_mime = _declared_mime(descriptor.get("file_type"))
    declared_size = _declared_size(descriptor.get("file_size"))
    locator_fingerprint = _locator_fingerprint(
        kind,
        locator,
        display_filename,
        descriptor.get("id") if isinstance(descriptor.get("id"), str) else None,
    )
    warnings: set[str] = set()
    resolution = _resolution(
        context=context,
        descriptor=descriptor,
        message_id=message_id,
        limits=limits,
        cancel_check=cancel_check,
    )
    content_sha256: str | None = None
    actual_size: int | None = None
    detected_mime: str | None = None
    member_fingerprint: str | None = None
    mapping_evidence_digest = normalized_json_digest(
        {
            "mapping_protocol": ADAPTER_VERSION,
            "mapping_method": resolution.mapping_method,
            "confidence": resolution.confidence,
            "candidate_fingerprints": list(resolution.candidate_fingerprints),
            "locator_fingerprint": locator_fingerprint,
        }
    )
    if resolution.entry_name is not None:
        entry_name = resolution.entry_name
        content_sha256, actual_size, prefix = _stream_entry_evidence(
            context,
            entry_name,
            limits,
            cancel_check,
        )
        detected_mime = (
            _detect_mime(prefix)
            if asset_mime_policy == CONTENT_SNIFF_MIME_V1
            else _detect_candidate_mime(prefix, display_filename)
        )
        member_fingerprint = normalized_json_digest(
            {"archive_member": entry_name}
        )
        state = "verified"
        if declared_size is not None and declared_size != actual_size:
            state = "size_mismatch"
            warnings.add("attachment_size_mismatch")
        if (
            declared_mime is not None
            and declared_mime != "application/octet-stream"
            and detected_mime != "application/octet-stream"
            and declared_mime != detected_mime
        ):
            state = "mime_mismatch" if state == "verified" else state
            warnings.add("attachment_mime_mismatch")
        entity_digest = normalized_json_digest(
            {
                "content_sha256": content_sha256,
                "actual_size": actual_size,
                "detected_mime": detected_mime,
            }
        )
    elif resolution.state == "external_url_blocked":
        state = "external_url_blocked"
        entity_digest = None
        warnings.add("external_url_blocked")
    elif resolution.state == "ambiguous":
        state = "ambiguous"
        entity_digest = mapping_evidence_digest
        warnings.add("ambiguous_asset_locator")
    elif resolution.state == "missing":
        state = "missing"
        entity_digest = None
        warnings.add("attachment_entity_missing")
    else:
        state = "metadata_only"
        entity_digest = normalized_json_digest(
            {
                "descriptor_digest": descriptor.get("_descriptor_digest"),
                "declared_mime": declared_mime,
                "declared_size": declared_size,
            }
        )
    attachment_kind = _safe_token(
        descriptor.get("_attachment_kind"), "attachment"
    )
    if attachment_kind == "attachment" and declared_mime:
        attachment_kind = declared_mime.split("/", 1)[0]
    return AttachmentOccurrence(
        source_type=SOURCE_TYPE,
        source_attachment_id=source_id,
        source_conversation_id=conversation_id,
        source_message_id=message_id,
        identity_kind=identity_kind,
        occurrence_index=occurrence_index,
        attachment_kind=attachment_kind,
        safe_display_filename=display_filename,
        declared_mime_type=declared_mime,
        declared_size=declared_size,
        source_locator_kind=kind,
        source_locator_fingerprint=locator_fingerprint,
        entity_digest=entity_digest,
        entity_state=state,
        warning_codes=tuple(sorted(warnings)),
        public_media_id=public_media_id,
        actual_size=actual_size,
        detected_mime_type=detected_mime,
        content_sha256=content_sha256,
        archive_member_fingerprint=member_fingerprint,
        archive_member_name=(
            resolution.entry_name
            if resolution.entry_name is not None
            else None
        ),
        archive_sha256=context.archive_sha256,
        mapping_method=resolution.mapping_method,
        mapping_confidence=resolution.confidence,
        resolution_candidate_count=(
            1
            if resolution.entry_name is not None
            else len(resolution.candidate_fingerprints)
        ),
        mapping_evidence_digest=mapping_evidence_digest,
    )


def _timestamp(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, "timestamp_missing"
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, (int, float)) or (
            isinstance(value, str)
            and re.fullmatch(r"-?\d+(?:\.\d+)?", value.strip())
        ):
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError
            parsed = datetime.fromtimestamp(numeric, timezone.utc)
        elif isinstance(value, str):
            candidate = value.strip()
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                raise ValueError
            parsed = parsed.astimezone(timezone.utc)
        else:
            raise ValueError
    except (ValueError, TypeError, OverflowError, OSError):
        return None, "invalid_timestamp"
    return (
        parsed.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        None,
    )


def _target_digest(
    existing_records: Sequence[ExistingRecord],
    production_fingerprint: DatabaseFingerprint,
    source_snapshot_complete: bool,
) -> str:
    return normalized_json_digest(
        {
            "target_snapshot_protocol": ADAPTER_VERSION,
            "production_fingerprint": production_fingerprint.to_dict(),
            "existing_records": sorted(
                (record.snapshot_descriptor() for record in existing_records),
                key=lambda item: (item["record_kind"], item["source_identity"]),
            ),
            "source_snapshot_complete": source_snapshot_complete,
        }
    )


def make_target_snapshot(
    *,
    existing_records: Sequence[ExistingRecord],
    production_fingerprint: DatabaseFingerprint,
    source_snapshot_complete: bool,
) -> TargetSnapshot:
    records = tuple(
        sorted(
            existing_records,
            key=lambda item: (item.record_kind, item.source_identity),
        )
    )
    return TargetSnapshot(
        existing_records=records,
        production_fingerprint=production_fingerprint,
        source_snapshot_complete=source_snapshot_complete,
        target_fingerprint=_target_digest(
            records,
            production_fingerprint,
            source_snapshot_complete,
        ),
    )


def empty_target_snapshot(*, source_snapshot_complete: bool = True) -> TargetSnapshot:
    empty_sha = hashlib.sha256(b"").hexdigest()
    fingerprint = build_database_fingerprint(
        content_sha256=empty_sha,
        size_bytes=0,
        schema_descriptor={"tables": []},
        logical_descriptor={"records": 0, "target_kind": "synthetic_empty"},
    )
    return make_target_snapshot(
        existing_records=(),
        production_fingerprint=fingerprint,
        source_snapshot_complete=source_snapshot_complete,
    )


class ChatGPTAdapter:
    """Stateful read-through adapter that caches one verified source scan."""

    def __init__(
        self,
        source: str | os.PathLike[str],
        *,
        limits: AdapterLimits | None = None,
        cancel_check: CancelCheck | None = None,
        timestamp: str | None = None,
        text_policy: str = STRUCTURED_TEXT_V1,
        asset_mime_policy: str = FILENAME_COMPAT_MIME_V1,
    ) -> None:
        if text_policy not in TEXT_POLICIES:
            raise ChatGPTAdapterError("invalid_text_policy")
        if asset_mime_policy not in ASSET_MIME_POLICIES:
            raise ChatGPTAdapterError("invalid_asset_mime_policy")
        self._source = Path(source)
        self._limits = limits or AdapterLimits()
        self._cancel_check = cancel_check
        self._timestamp = timestamp or _now()
        self._text_policy = text_policy
        self._asset_mime_policy = asset_mime_policy
        self._adapter_version = (
            ADAPTER_VERSION
            + "."
            + _TEXT_POLICY_VERSION_TOKENS[text_policy]
            + "."
            + _ASSET_MIME_POLICY_VERSION_TOKENS[asset_mime_policy]
        )
        self._state: _ScanState | None = None

    def _ensure_scan(self) -> _ScanState:
        if self._state is not None:
            return self._state
        context = _load_source(
            self._source,
            self._limits,
            self._cancel_check,
        )
        graphs: list[_GraphResult] = []
        seen_conversation_ids: set[str] = set()
        for conversation in context.conversations:
            graph = _graph_conversation(conversation)
            if graph.conversation_id in seen_conversation_ids:
                raise ChatGPTAdapterError("duplicate_conversation_identity")
            seen_conversation_ids.add(graph.conversation_id)
            graphs.append(graph)
        graphs.sort(key=lambda item: item.conversation_id)
        prepared: dict[tuple[str, str], _PreparedMessage] = {}
        occurrences: list[AttachmentOccurrence] = []
        unsupported_parts = 0
        all_warning_codes: set[str] = set(context.warnings)
        for graph in graphs:
            all_warning_codes.update(graph.warning_codes)
            for raw in graph.audit_messages:
                (
                    text,
                    content_type,
                    structured_parts,
                    descriptors,
                    content_warnings,
                ) = _parse_message_content(
                    raw.message,
                    self._limits,
                    text_policy=self._text_policy,
                )
                unsupported_parts += sum(
                    item.get("part_type") == "unsupported"
                    for item in structured_parts
                )
                warnings = tuple(
                    sorted(
                        set(raw.warning_codes)
                        | set(content_warnings)
                    )
                )
                all_warning_codes.update(warnings)
                prepared[(graph.conversation_id, raw.source_message_id)] = (
                    _PreparedMessage(
                        raw=raw,
                        text=text,
                        content_type=content_type,
                        structured_parts=structured_parts,
                        attachment_descriptors=descriptors,
                        warning_codes=warnings,
                    )
                )
                for occurrence_index, descriptor in enumerate(descriptors):
                    occurrence = _resolve_occurrence(
                        context=context,
                        descriptor=descriptor,
                        conversation_id=graph.conversation_id,
                        message_id=raw.source_message_id,
                        occurrence_index=occurrence_index,
                        limits=self._limits,
                        cancel_check=self._cancel_check,
                        asset_mime_policy=self._asset_mime_policy,
                    )
                    occurrences.append(occurrence)
                    all_warning_codes.update(occurrence.warning_codes)
        content_groups: dict[str, list[AttachmentOccurrence]] = {}
        for occurrence in occurrences:
            if (
                occurrence.content_sha256 is not None
                and occurrence.actual_size is not None
                and occurrence.entity_state in {
                    "verified",
                    "size_mismatch",
                    "mime_mismatch",
                }
            ):
                content_groups.setdefault(
                    occurrence.content_sha256,
                    [],
                ).append(occurrence)
        canonicalized_occurrences: list[AttachmentOccurrence] = []
        for occurrence in occurrences:
            if occurrence.content_sha256 is None:
                canonicalized_occurrences.append(occurrence)
                continue
            content_group = content_groups.get(
                occurrence.content_sha256,
                [],
            )
            sizes = {item.actual_size for item in content_group}
            if len(sizes) > 1:
                raise ChatGPTAdapterError("content_digest_size_conflict")
            detected_mimes = {
                item.detected_mime_type
                for item in content_group
                if item.detected_mime_type is not None
            }
            if len(detected_mimes) <= 1:
                canonicalized_occurrences.append(occurrence)
                continue
            canonical_mime = "application/octet-stream"
            canonicalized_occurrences.append(
                replace(
                    occurrence,
                    entity_digest=normalized_json_digest(
                        {
                            "content_sha256": occurrence.content_sha256,
                            "actual_size": occurrence.actual_size,
                            "detected_mime": canonical_mime,
                        }
                    ),
                    detected_mime_type=canonical_mime,
                    warning_codes=tuple(
                        sorted(
                            set(occurrence.warning_codes)
                            | {"content_digest_mime_conflict_canonicalized"}
                        )
                    ),
                )
            )
            all_warning_codes.add(
                "content_digest_mime_conflict_canonicalized"
            )
        occurrences = canonicalized_occurrences
        groups: dict[str, list[AttachmentOccurrence]] = {}
        for occurrence in occurrences:
            groups.setdefault(occurrence.source_attachment_id, []).append(occurrence)
        for source_id, group in groups.items():
            verified_entities = {
                (
                    item.content_sha256,
                    item.actual_size,
                    item.detected_mime_type,
                )
                for item in group
                if item.entity_state in {
                    "verified",
                    "size_mismatch",
                    "mime_mismatch",
                }
            }
            metadata_variants = {
                (
                    normalized_json_digest(
                        {"safe_filename": item.safe_display_filename}
                    ),
                    item.declared_mime_type,
                    item.declared_size,
                )
                for item in group
            }
            identity_conflict = (
                len(verified_entities) > 1
                or (
                    not verified_entities
                    and len(metadata_variants) > 1
                )
            )
            if identity_conflict:
                evidence_digest = normalized_json_digest(
                    {
                        "source_attachment_identity": normalized_json_digest(
                            {"source_attachment_id": source_id}
                        ),
                        "verified_entities": sorted(verified_entities),
                        "metadata_variants": sorted(
                            metadata_variants,
                            key=lambda item: normalized_json_digest(
                                {"metadata_variant": item}
                            ),
                        ),
                    }
                )
                replacements = []
                for item in group:
                    replacements.append(
                        replace(
                            item,
                            entity_digest=evidence_digest,
                            entity_state="ambiguous",
                            warning_codes=tuple(
                                sorted(
                                    set(item.warning_codes)
                                    | {"source_attachment_identity_conflict"}
                                )
                            ),
                            actual_size=None,
                            detected_mime_type=None,
                            content_sha256=None,
                            archive_member_fingerprint=None,
                            archive_member_name=None,
                            mapping_method="source_attachment_identity_conflict",
                            mapping_confidence="none",
                            resolution_candidate_count=max(
                                len(verified_entities),
                                len(metadata_variants),
                            ),
                            mapping_evidence_digest=evidence_digest,
                        )
                    )
                group[:] = replacements
                all_warning_codes.add("source_attachment_identity_conflict")
        occurrences = [
            item
            for source_id in sorted(groups)
            for item in groups[source_id]
        ]
        state_rank = {
            "verified": 0,
            "size_mismatch": 1,
            "mime_mismatch": 2,
            "ambiguous": 3,
            "missing": 4,
            "external_url_blocked": 5,
            "metadata_only": 6,
        }
        representatives = {
            source_id: sorted(
                group,
                key=lambda item: (
                    state_rank[item.entity_state],
                    item.mapping_method,
                    item.source_locator_fingerprint,
                ),
            )[0]
            for source_id, group in groups.items()
        }
        message_occurrences = [
            item.source_message_id
            for graph in graphs
            for item in graph.audit_messages
        ]
        internal_duplicate_messages = (
            len(message_occurrences) - len(set(message_occurrences))
        )
        record_counts = {
            "conversations": len(graphs),
            "mapping_nodes": sum(item.mapping_nodes for item in graphs),
            "active_messages": sum(item.active_message_count for item in graphs),
            "branch_messages": sum(item.branch_message_count for item in graphs),
            "empty_nodes": sum(item.empty_node_count for item in graphs),
            "orphan_nodes": sum(item.orphan_node_count for item in graphs),
            "attachments": len(occurrences),
            "logical_attachments": len(representatives),
            "asset_entities": len(
                {
                    item.content_sha256
                    for item in representatives.values()
                    if item.entity_state == "verified" and item.content_sha256
                }
            ),
            "metadata_only_assets": sum(
                item.entity_state == "metadata_only" for item in occurrences
            ),
            "unsupported_parts": unsupported_parts,
            "archive_internal_duplicate_messages": internal_duplicate_messages,
            "warnings": len(all_warning_codes),
            "errors": 0,
        }
        asset_counts = {
            "native_identity": sum(
                item.identity_kind == "native"
                for item in representatives.values()
            ),
            "synthetic_identity": sum(
                item.identity_kind == "synthetic"
                for item in representatives.values()
            ),
            "bytes_verified": sum(
                item.entity_state == "verified"
                for item in representatives.values()
            ),
            "metadata_only": sum(
                item.entity_state == "metadata_only"
                for item in representatives.values()
            ),
            "missing": sum(
                item.entity_state == "missing"
                for item in representatives.values()
            ),
            "external_blocked": sum(
                item.entity_state == "external_url_blocked"
                for item in representatives.values()
            ),
            "ambiguous": sum(
                item.entity_state == "ambiguous"
                for item in representatives.values()
            ),
            "size_mismatch": sum(
                item.entity_state == "size_mismatch"
                for item in representatives.values()
            ),
            "mime_mismatch": sum(
                item.entity_state == "mime_mismatch"
                for item in representatives.values()
            ),
            "archive_asset_entries": sum(
                name.lower().endswith(".dat")
                for name in context.entries
            ),
            "official_catalog_entries": len(
                set(context.asset_name_map)
                | {
                    item.entry_name
                    for item in context.library_assets.values()
                }
            ),
        }
        warning_codes = tuple(sorted(all_warning_codes))
        manifest = build_source_manifest(
            source_type=SOURCE_TYPE,
            source_version=SOURCE_VERSION,
            adapter_name=ADAPTER_NAME,
            adapter_version=self._adapter_version,
            archive_sha256=context.archive_sha256,
            archive_size=context.archive_size,
            archive_kind=context.archive_kind,
            discovered_at=self._timestamp,
            scanned_at=self._timestamp,
            record_counts=record_counts,
            asset_counts=asset_counts,
            warnings=warning_codes[:256],
            validation_status=(
                "valid_with_warnings" if warning_codes else "valid"
            ),
            source_locator_kind=context.source_locator_kind,
        )
        audit_summary = SourceAuditSummary(
            record_counts=record_counts,
            asset_counts=asset_counts,
            warning_codes=warning_codes,
        )
        self._state = _ScanState(
            context=context,
            manifest=manifest,
            graphs=tuple(graphs),
            prepared_messages=MappingProxyType(prepared),
            occurrences=tuple(
                sorted(
                    occurrences,
                    key=lambda item: (
                        item.source_message_id,
                        item.occurrence_index,
                        item.source_attachment_id,
                    ),
                )
            ),
            audit_summary=audit_summary,
        )
        return self._state

    def probe_source(self) -> SourceProbe:
        state = self._ensure_scan()
        context = state.context
        return SourceProbe(
            archive_kind=context.archive_kind,
            archive_sha256=context.archive_sha256,
            archive_size=context.archive_size,
            entry_count=len(context.entries),
            total_compressed_size=context.total_compressed_size,
            total_uncompressed_size=context.total_uncompressed_size,
            conversations_file_count=len(context.conversations_names),
            source_locator_kind=context.source_locator_kind,
        )

    def scan_manifest(self) -> SourceManifest:
        return self._ensure_scan().manifest

    def validate_source(self) -> SourceAuditSummary:
        return self._ensure_scan().audit_summary

    def enumerate_conversations(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            graph.raw_conversation for graph in self._ensure_scan().graphs
        )

    def enumerate_messages(
        self,
        *,
        branch_policy: str = ACTIVE_PATH_COMPAT,
    ) -> tuple[_RawMessage, ...]:
        if branch_policy not in BRANCH_POLICIES:
            raise ChatGPTAdapterError("invalid_branch_policy")
        messages: list[_RawMessage] = []
        for graph in self._ensure_scan().graphs:
            messages.extend(
                graph.active_messages
                if branch_policy == ACTIVE_PATH_COMPAT
                else graph.audit_messages
            )
        return tuple(messages)

    def enumerate_assets(self) -> tuple[AttachmentOccurrence, ...]:
        return self._ensure_scan().occurrences

    def _batch(self, batch_mode: str, branch_policy: str) -> ImportBatch:
        if batch_mode not in {"full", "incremental"}:
            raise ChatGPTAdapterError("invalid_batch_mode")
        if branch_policy not in BRANCH_POLICIES:
            raise ChatGPTAdapterError("invalid_branch_policy")
        manifest = self.scan_manifest()
        batch_digest = normalized_json_digest(
            {
                "batch_identity_protocol": self._adapter_version,
                "manifest_id": manifest.manifest_id,
                "batch_mode": batch_mode,
                "branch_policy": branch_policy,
            }
        )
        return create_import_batch(
            manifest,
            batch_mode=batch_mode,
            plan={
                "phase": "adapter_plan_request",
                "manifest_id": manifest.manifest_id,
                "batch_mode": batch_mode,
                "branch_policy": branch_policy,
            },
            timestamp=self._timestamp,
            batch_id_factory=lambda: "batch_" + batch_digest[:32],
        )

    def normalize_conversation(
        self,
        graph: _GraphResult,
        *,
        batch: ImportBatch,
        ordinal: int,
        branch_policy: str = ACTIVE_PATH_COMPAT,
    ) -> NormalizedConversation:
        if branch_policy not in BRANCH_POLICIES:
            raise ChatGPTAdapterError("invalid_branch_policy")
        raw = graph.raw_conversation
        created_at, created_warning = _timestamp(raw.get("create_time"))
        updated_at, updated_warning = _timestamp(raw.get("update_time"))
        warning_codes = set(graph.warning_codes)
        missing_reasons: dict[str, str] = {}
        if created_warning:
            warning_codes.add(
                "conversation_created_at_missing"
                if created_warning == "timestamp_missing"
                else "conversation_created_at_invalid"
            )
            missing_reasons["created_at"] = created_warning
        if updated_warning:
            warning_codes.add(
                "conversation_updated_at_missing"
                if updated_warning == "timestamp_missing"
                else "conversation_updated_at_invalid"
            )
            missing_reasons["updated_at"] = updated_warning
        provenance = make_provenance(
            record_kind="conversation",
            source_type=SOURCE_TYPE,
            manifest_id=self.scan_manifest().manifest_id,
            source_record_id=graph.conversation_id,
            batch_id=batch.batch_id,
            adapter_version=self._adapter_version,
            parsed_at=self._timestamp,
            locator=make_record_locator(
                "chatgpt_conversation",
                ordinal,
                private_locator_material={
                    "conversation_id": graph.conversation_id
                },
            ),
        )
        return normalize_planning_conversation(
            provenance,
            title=_normalize_text(
                raw.get("title") if isinstance(raw.get("title"), str) else ""
            ),
            created_at=created_at,
            updated_at=updated_at,
            source_status={
                "is_archived": raw.get("is_archived") is True,
                "is_starred": raw.get("is_starred") is True,
                "default_model_slug": _safe_token(
                    raw.get("default_model_slug"), "unknown"
                ),
                "branch_policy": branch_policy,
                "mapping_nodes": graph.mapping_nodes,
                "active_messages": graph.active_message_count,
                "branch_messages": graph.branch_message_count,
                "orphan_nodes": graph.orphan_node_count,
                "empty_nodes": graph.empty_node_count,
                "cycle_or_invalid_relations": graph.cycle_or_invalid_count,
            },
            message_source_ids=[
                item.source_message_id
                for item in (
                    graph.active_messages
                    if branch_policy == ACTIVE_PATH_COMPAT
                    else graph.audit_messages
                )
            ],
            warning_codes=tuple(sorted(warning_codes)),
            missing_reasons=missing_reasons,
        )

    def normalize_message(
        self,
        prepared: _PreparedMessage,
        *,
        batch: ImportBatch,
        ordinal: int,
        attachment_refs: Sequence[str],
        branch_policy: str = ACTIVE_PATH_COMPAT,
    ) -> NormalizedMessage:
        if branch_policy not in BRANCH_POLICIES:
            raise ChatGPTAdapterError("invalid_branch_policy")
        raw = prepared.raw
        message = raw.message
        created_at, created_warning = _timestamp(message.get("create_time"))
        updated_at, updated_warning = _timestamp(message.get("update_time"))
        warnings = set(prepared.warning_codes)
        missing_reasons: dict[str, str] = {}
        if not raw.node_id:
            missing_reasons["source_node_id"] = "source_node_id_missing"
        if created_warning:
            warnings.add(
                "message_created_at_missing"
                if created_warning == "timestamp_missing"
                else "message_created_at_invalid"
            )
            missing_reasons["created_at"] = created_warning
        if updated_warning:
            warnings.add(
                "message_updated_at_missing"
                if updated_warning == "timestamp_missing"
                else "message_updated_at_invalid"
            )
            missing_reasons["updated_at"] = updated_warning
        if raw.parent_source_message_id is None:
            missing_reasons[
                "parent_source_message_id"
            ] = "root_message_has_no_parent"
        author_raw = message.get("author")
        author = author_raw if isinstance(author_raw, Mapping) else {}
        role = _safe_token(author.get("role"), "unknown")
        if role == "unknown":
            warnings.add("unknown_message_role")
        author_name = author.get("name")
        if not isinstance(author_name, str) or not author_name:
            author_name = role
        status = _safe_token(message.get("status"), "unknown")
        weight = message.get("weight")
        if isinstance(weight, bool) or (
            weight is not None
            and (
                not isinstance(weight, (int, float))
                or not math.isfinite(float(weight))
            )
        ):
            weight = None
            warnings.add("invalid_message_weight")
        provenance = make_provenance(
            record_kind="message",
            source_type=SOURCE_TYPE,
            manifest_id=self.scan_manifest().manifest_id,
            source_record_id=raw.source_message_id,
            batch_id=batch.batch_id,
            adapter_version=self._adapter_version,
            parsed_at=self._timestamp,
            locator=make_record_locator(
                "chatgpt_message",
                ordinal,
                private_locator_material={
                    "conversation_id": raw.conversation_id,
                    "source_message_id": raw.source_message_id,
                },
            ),
        )
        return normalize_planning_message(
            provenance,
            source_conversation_id=raw.conversation_id,
            source_node_id=raw.node_id or None,
            role=role,
            author=_normalize_text(author_name),
            content_type=prepared.content_type,
            text=prepared.text,
            status=status,
            weight=weight,
            created_at=created_at,
            updated_at=updated_at,
            parent_source_message_id=raw.parent_source_message_id,
            child_source_message_ids=raw.child_source_message_ids,
            metadata={
                "identity_kind": raw.identity_kind,
                "structured_parts": list(prepared.structured_parts),
                "attachment_refs": list(sorted(set(attachment_refs))),
                "branch_policy": branch_policy,
                "normalization_version": self._adapter_version,
                "text_policy": self._text_policy,
            },
            warning_codes=tuple(sorted(warnings)),
            missing_reasons=missing_reasons,
        )

    def normalize_attachment(
        self,
        occurrence: AttachmentOccurrence,
        *,
        batch: ImportBatch,
        ordinal: int,
        asset_entity_id: str | None,
    ) -> NormalizedAttachment:
        missing_reasons: dict[str, str] = {}
        for field_name, value in (
            ("declared_mime", occurrence.declared_mime_type),
            ("declared_size", occurrence.declared_size),
            ("detected_mime", occurrence.detected_mime_type),
            ("actual_size", occurrence.actual_size),
            ("content_sha256", occurrence.content_sha256),
        ):
            if value is None:
                missing_reasons[field_name] = "source_field_unavailable"
        state_map = {
            "verified": ("entity_available", None),
            "metadata_only": ("metadata_only", "entity_bytes_absent"),
            "missing": ("entity_missing", "entity_bytes_missing"),
            "external_url_blocked": ("entity_blocked", "external_url_blocked"),
            "ambiguous": ("entity_blocked", "ambiguous_asset_locator"),
            "size_mismatch": ("entity_invalid", "attachment_size_mismatch"),
            "mime_mismatch": ("entity_invalid", "attachment_mime_mismatch"),
        }
        entity_state, unavailable_reason = state_map[occurrence.entity_state]
        provenance = make_provenance(
            record_kind="attachment",
            source_type=SOURCE_TYPE,
            manifest_id=self.scan_manifest().manifest_id,
            source_record_id=occurrence.source_attachment_id,
            batch_id=batch.batch_id,
            adapter_version=self._adapter_version,
            parsed_at=self._timestamp,
            locator=make_record_locator(
                "chatgpt_attachment",
                ordinal,
                private_locator_material={
                    "source_attachment_id": occurrence.source_attachment_id
                },
            ),
        )
        locator = RecordLocator(
            locator_kind=occurrence.source_locator_kind,
            ordinal=occurrence.occurrence_index,
            locator_fingerprint=occurrence.source_locator_fingerprint,
        )
        return normalize_planning_attachment(
            provenance,
            source_conversation_id=occurrence.source_conversation_id,
            source_message_id=occurrence.source_message_id,
            original_filename=occurrence.safe_display_filename,
            safe_display_filename=occurrence.safe_display_filename,
            declared_mime=occurrence.declared_mime_type,
            detected_mime=occurrence.detected_mime_type,
            declared_size=occurrence.declared_size,
            actual_size=occurrence.actual_size,
            content_sha256=occurrence.content_sha256,
            archive_member_locator=locator,
            entity_state=entity_state,
            unavailable_reason=unavailable_reason,
            asset_entity_id=asset_entity_id,
            warning_codes=occurrence.warning_codes,
            conflict_state=(
                "pending"
                if occurrence.entity_state == "ambiguous"
                else "clear"
            ),
            missing_reasons=missing_reasons,
        )

    def _conversation_for_message(self, source_message_id: str) -> str:
        for graph in self._ensure_scan().graphs:
            if any(
                item.source_message_id == source_message_id
                for item in graph.active_messages
            ):
                return graph.conversation_id
        raise ChatGPTAdapterError("attachment_message_binding_missing")

    def build_normalized_bundle(
        self,
        *,
        batch_mode: str,
        branch_policy: str = ACTIVE_PATH_COMPAT,
    ) -> NormalizedBundle:
        if branch_policy not in BRANCH_POLICIES:
            raise ChatGPTAdapterError("invalid_branch_policy")
        state = self._ensure_scan()
        batch = self._batch(batch_mode, branch_policy)
        conversations = tuple(
            self.normalize_conversation(
                graph,
                batch=batch,
                ordinal=ordinal,
                branch_policy=branch_policy,
            )
            for ordinal, graph in enumerate(state.graphs)
        )
        selected_raw = [
            (graph, raw)
            for graph in state.graphs
            for raw in (
                graph.active_messages
                if branch_policy == ACTIVE_PATH_COMPAT
                else graph.audit_messages
            )
        ]
        selected_pairs = {
            (graph.conversation_id, raw.source_message_id)
            for graph, raw in selected_raw
        }
        selected_occurrences = tuple(
            occurrence
            for occurrence in state.occurrences
            if (
                occurrence.source_conversation_id,
                occurrence.source_message_id,
            )
            in selected_pairs
        )
        occurrences_by_message: dict[
            tuple[str, str],
            list[AttachmentOccurrence],
        ] = {}
        for occurrence in selected_occurrences:
            occurrences_by_message.setdefault(
                (
                    occurrence.source_conversation_id,
                    occurrence.source_message_id,
                ),
                [],
            ).append(occurrence)
        message_candidates: list[NormalizedMessage] = []
        message_ordinal = 0
        for graph, raw in selected_raw:
            prepared = state.prepared_messages[
                (graph.conversation_id, raw.source_message_id)
            ]
            refs = [
                item.source_attachment_id
                for item in occurrences_by_message.get(
                    (graph.conversation_id, raw.source_message_id),
                    (),
                )
            ]
            message_candidates.append(
                self.normalize_message(
                    prepared,
                    batch=batch,
                    ordinal=message_ordinal,
                    attachment_refs=refs,
                    branch_policy=branch_policy,
                )
            )
            message_ordinal += 1
        message_groups: dict[str, list[NormalizedMessage]] = {}
        for message in message_candidates:
            message_groups.setdefault(message.source_identity, []).append(message)
        messages: list[NormalizedMessage] = []
        folded_message_occurrences = 0
        message_identity_content_conflicts = 0
        for source_identity, group in message_groups.items():
            role_text_variants = {
                (item.role, item.text)
                for item in group
            }
            if len(role_text_variants) == 1:
                messages.append(
                    sorted(
                        group,
                        key=lambda item: (
                            item.source_conversation_id,
                            item.source_node_id or "",
                            item.structure_hash,
                        ),
                    )[0]
                )
                folded_message_occurrences += len(group) - 1
            else:
                messages.extend(
                    sorted(
                        group,
                        key=lambda item: (
                            item.content_hash,
                            item.source_conversation_id,
                            item.source_node_id or "",
                        ),
                    )
                )
                message_identity_content_conflicts += 1
        unique_occurrences: dict[str, AttachmentOccurrence] = {}
        state_rank = {
            "verified": 0,
            "size_mismatch": 1,
            "mime_mismatch": 2,
            "ambiguous": 3,
            "missing": 4,
            "external_url_blocked": 5,
            "metadata_only": 6,
        }
        for occurrence in sorted(
            selected_occurrences,
            key=lambda item: (
                item.source_attachment_id,
                state_rank[item.entity_state],
                item.mapping_method,
                item.source_locator_fingerprint,
            ),
        ):
            unique_occurrences.setdefault(
                occurrence.source_attachment_id,
                occurrence,
            )
        content_assets: dict[str, AssetEntity] = {}
        for occurrence in unique_occurrences.values():
            if (
                occurrence.entity_state != "verified"
                or occurrence.content_sha256 is None
                or occurrence.actual_size is None
                or occurrence.detected_mime_type is None
            ):
                continue
            if occurrence.content_sha256 not in content_assets:
                content_assets[occurrence.content_sha256] = make_asset_entity(
                    content_sha256=occurrence.content_sha256,
                    actual_size=occurrence.actual_size,
                    detected_mime=occurrence.detected_mime_type,
                    storage_root_id="chatgpt_scan_ephemeral",
                    relative_entity_path=(
                        "verified/"
                        + occurrence.content_sha256[:2]
                        + "/"
                        + occurrence.content_sha256
                        + ".blob"
                    ),
                    first_seen_batch_id=batch.batch_id,
                    integrity_state="verified",
                    created_at=self._timestamp,
                )
        attachments: list[NormalizedAttachment] = []
        for ordinal, occurrence in enumerate(
            sorted(
                unique_occurrences.values(),
                key=lambda item: item.source_attachment_id,
            )
        ):
            asset = (
                content_assets.get(occurrence.content_sha256)
                if occurrence.content_sha256
                else None
            )
            attachments.append(
                self.normalize_attachment(
                    occurrence,
                    batch=batch,
                    ordinal=ordinal,
                    asset_entity_id=asset.asset_entity_id if asset else None,
                )
            )
        descriptor = {
            "normalization_protocol": self._adapter_version,
            "branch_policy": branch_policy,
            "text_policy": self._text_policy,
            "conversations": sorted(
                (
                    {
                        "source_identity": item.source_identity,
                        "content_hash": item.content_hash,
                        "structure_hash": item.message_identity_digest,
                        "warnings": list(item.warning_codes),
                    }
                    for item in conversations
                ),
                key=lambda item: item["source_identity"],
            ),
            "messages": sorted(
                (
                    {
                        "source_identity": item.source_identity,
                        "content_hash": item.content_hash,
                        "structure_hash": item.structure_hash,
                        "warnings": list(item.warning_codes),
                    }
                    for item in messages
                ),
                key=lambda item: item["source_identity"],
            ),
            "attachments": sorted(
                (
                    {
                        "source_identity": item.source_identity,
                        "content_hash": item.content_hash,
                        "metadata_digest": item.metadata_digest,
                        "entity_state": item.entity_state,
                        "warnings": list(item.warning_codes),
                    }
                    for item in attachments
                ),
                key=lambda item: item["source_identity"],
            ),
            "assets": sorted(
                (
                    {
                        "source_identity": item.source_identity,
                        "content_sha256": item.content_sha256,
                        "actual_size": item.actual_size,
                        "detected_mime": item.detected_mime,
                    }
                    for item in content_assets.values()
                ),
                key=lambda item: item["source_identity"],
            ),
            "occurrences": [
                item.semantic_descriptor() for item in selected_occurrences
            ],
        }
        adjusted_record_counts = dict(state.audit_summary.record_counts)
        adjusted_record_counts.update(
            {
                "selected_message_occurrences": len(message_candidates),
                "normalized_stable_messages": len(messages),
                "archive_internal_duplicates_folded": (
                    folded_message_occurrences
                ),
                "message_identity_content_conflicts": (
                    message_identity_content_conflicts
                ),
                "selected_attachment_occurrences": len(
                    selected_occurrences
                ),
                "selected_logical_attachments": len(unique_occurrences),
            }
        )
        adjusted_audit = SourceAuditSummary(
            record_counts=adjusted_record_counts,
            asset_counts=state.audit_summary.asset_counts,
            warning_codes=state.audit_summary.warning_codes,
            branch_policy=branch_policy,
        )
        return NormalizedBundle(
            batch=batch,
            conversations=conversations,
            messages=tuple(messages),
            attachments=tuple(attachments),
            asset_entities=tuple(
                sorted(
                    content_assets.values(),
                    key=lambda item: item.asset_entity_id,
                )
            ),
            attachment_occurrences=selected_occurrences,
            audit_summary=adjusted_audit,
            normalized_digest=normalized_json_digest(descriptor),
        )

    def produce_import_plan(
        self,
        *,
        target_snapshot: TargetSnapshot,
        batch_mode: str,
        branch_policy: str = ACTIVE_PATH_COMPAT,
    ) -> ChatGPTPlanningResult:
        bundle = self.build_normalized_bundle(
            batch_mode=batch_mode,
            branch_policy=branch_policy,
        )
        plan = plan_import(
            manifest=self.scan_manifest(),
            batch=bundle.batch,
            conversations=bundle.conversations,
            messages=bundle.messages,
            attachments=bundle.attachments,
            asset_entities=bundle.asset_entities,
            existing_records=target_snapshot.existing_records,
            production_fingerprint=target_snapshot.production_fingerprint,
            asset_stage_fingerprint=None,
            created_at=self._timestamp,
            source_snapshot_complete=target_snapshot.source_snapshot_complete,
        )
        planned_batch = replace(
            bundle.batch,
            current_state=BatchState.PLANNED.value,
            plan_digest=make_plan_digest(plan.to_dict()),
            warning_count=len(bundle.audit_summary.warning_codes),
            error_count=0,
            conflict_count=len(plan.conflicts),
        )
        bundle = replace(bundle, batch=planned_batch)
        semantic_plan_digest = normalized_json_digest(
            {
                "semantic_plan_protocol": self._adapter_version,
                "batch_mode": batch_mode,
                "branch_policy": branch_policy,
                "normalized_digest": bundle.normalized_digest,
                "target_fingerprint": target_snapshot.target_fingerprint,
                "actions": sorted(
                    (
                        {
                            "action_type": action.action_type,
                            "record_kind": action.record_kind,
                            "source_identity": action.source_identity,
                            "requires_approval": action.requires_approval,
                            "evidence_codes": list(action.evidence_codes),
                        }
                        for action in plan.actions
                    ),
                    key=lambda item: (
                        item["record_kind"],
                        item["source_identity"],
                        item["action_type"],
                    ),
                ),
                "conflicts": sorted(
                    (
                        {
                            "record_kind": conflict.record_kind,
                            "source_identity": conflict.source_identity,
                            "conflict_type": conflict.conflict_type,
                            "risk_level": conflict.risk_level,
                        }
                        for conflict in plan.conflicts
                    ),
                    key=lambda item: (
                        item["record_kind"],
                        item["source_identity"],
                        item["conflict_type"],
                    ),
                ),
            }
        )
        return ChatGPTPlanningResult(
            manifest=self.scan_manifest(),
            bundle=bundle,
            target_snapshot=target_snapshot,
            import_plan=plan,
            adapter_warnings=bundle.audit_summary.warning_codes,
            source_audit_summary=bundle.audit_summary,
            semantic_plan_digest=semantic_plan_digest,
        )

    def verify_scan_determinism(self) -> bool:
        first = self._ensure_scan()
        second = ChatGPTAdapter(
            self._source,
            limits=self._limits,
            cancel_check=self._cancel_check,
            timestamp=self._timestamp,
            text_policy=self._text_policy,
            asset_mime_policy=self._asset_mime_policy,
        )._ensure_scan()
        first_descriptor = {
            "manifest": {
                "manifest_id": first.manifest.manifest_id,
                "record_counts": dict(first.manifest.record_counts),
                "asset_counts": dict(first.manifest.asset_counts),
                "warnings": list(first.manifest.warnings),
            },
            "audit": first.audit_summary.to_dict(),
            "occurrences": [
                item.semantic_descriptor() for item in first.occurrences
            ],
        }
        second_descriptor = {
            "manifest": {
                "manifest_id": second.manifest.manifest_id,
                "record_counts": dict(second.manifest.record_counts),
                "asset_counts": dict(second.manifest.asset_counts),
                "warnings": list(second.manifest.warnings),
            },
            "audit": second.audit_summary.to_dict(),
            "occurrences": [
                item.semantic_descriptor() for item in second.occurrences
            ],
        }
        return first_descriptor == second_descriptor


def probe_source(
    source: str | os.PathLike[str],
    *,
    limits: AdapterLimits | None = None,
    cancel_check: CancelCheck | None = None,
    text_policy: str = STRUCTURED_TEXT_V1,
    asset_mime_policy: str = FILENAME_COMPAT_MIME_V1,
) -> SourceProbe:
    return ChatGPTAdapter(
        source,
        limits=limits,
        cancel_check=cancel_check,
        text_policy=text_policy,
        asset_mime_policy=asset_mime_policy,
    ).probe_source()


def scan_chatgpt_source(
    source: str | os.PathLike[str],
    *,
    limits: AdapterLimits | None = None,
    cancel_check: CancelCheck | None = None,
    timestamp: str | None = None,
    text_policy: str = STRUCTURED_TEXT_V1,
    asset_mime_policy: str = FILENAME_COMPAT_MIME_V1,
) -> SourceManifest:
    return ChatGPTAdapter(
        source,
        limits=limits,
        cancel_check=cancel_check,
        timestamp=timestamp,
        text_policy=text_policy,
        asset_mime_policy=asset_mime_policy,
    ).scan_manifest()


def normalize_chatgpt_source(
    source: str | os.PathLike[str],
    *,
    batch_mode: str,
    branch_policy: str = ACTIVE_PATH_COMPAT,
    limits: AdapterLimits | None = None,
    cancel_check: CancelCheck | None = None,
    timestamp: str | None = None,
    text_policy: str = STRUCTURED_TEXT_V1,
    asset_mime_policy: str = FILENAME_COMPAT_MIME_V1,
) -> NormalizedBundle:
    return ChatGPTAdapter(
        source,
        limits=limits,
        cancel_check=cancel_check,
        timestamp=timestamp,
        text_policy=text_policy,
        asset_mime_policy=asset_mime_policy,
    ).build_normalized_bundle(
        batch_mode=batch_mode,
        branch_policy=branch_policy,
    )


def plan_chatgpt_import(
    source: str | os.PathLike[str],
    *,
    target_snapshot: TargetSnapshot,
    batch_mode: str,
    branch_policy: str = ACTIVE_PATH_COMPAT,
    limits: AdapterLimits | None = None,
    cancel_check: CancelCheck | None = None,
    timestamp: str | None = None,
    text_policy: str = STRUCTURED_TEXT_V1,
    asset_mime_policy: str = FILENAME_COMPAT_MIME_V1,
) -> ChatGPTPlanningResult:
    return ChatGPTAdapter(
        source,
        limits=limits,
        cancel_check=cancel_check,
        timestamp=timestamp,
        text_policy=text_policy,
        asset_mime_policy=asset_mime_policy,
    ).produce_import_plan(
        target_snapshot=target_snapshot,
        batch_mode=batch_mode,
        branch_policy=branch_policy,
    )
