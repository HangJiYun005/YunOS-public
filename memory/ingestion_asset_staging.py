"""Controlled prototype asset staging for YunOS v8.3-D.

All writes are confined to the system temporary directory or the ignored
``output/v8.3-d-asset-preservation-staging`` node directory.  The module never
opens the production database, never downloads URLs, and never promotes staged
bytes into ``imports`` or another production location.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote
import uuid
import zipfile

from memory.db_path_policy import is_reparse_point
from memory.ingestion_assets import (
    AssetPlanningError,
    AssetStageLimits,
    AssetStageOperation,
    AssetStagePlan,
    AssetStageResult,
)
from memory.ingestion_core import canonical_json_bytes, normalized_json_digest


ASSET_STAGE_LEDGER_VERSION = "v8.3-d-asset-stage-ledger-1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE_OUTPUT_ROOT = PROJECT_ROOT / "output" / "v8.3-d-asset-preservation-staging"
PRODUCTION_EXECUTION_STAGING_ROOT = (
    PROJECT_ROOT
    / "output"
    / "v8.3-real-import-asset-baseline-fix"
    / "executions"
)
SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir())

_BATCH_ID_RE = re.compile(r"batch_[0-9a-f]{32}\Z")
_OPERATION_ID_RE = re.compile(r"astageop_[0-9a-f]{64}\Z")
_SAFE_LEDGER_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


class AssetStagingError(RuntimeError):
    """A sanitized, fail-closed staging error."""

    def __init__(self, reason_code: str, *, quarantined: bool = False) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.quarantined = quarantined


class AssetStagingInterrupted(BaseException):
    """Test seam that models a process exit without normal cleanup."""

    def __init__(self, point: str) -> None:
        super().__init__(point)
        self.point = point
        self.lease_token: str | None = None


class _Cancellation(RuntimeError):
    def __init__(self, *, quarantined: bool = False) -> None:
        super().__init__("cancelled")
        self.quarantined = quarantined


@dataclass(frozen=True)
class ZipAssetSource:
    """Runtime-only source binding; paths and member names are never persisted."""

    operation_id: str
    archive_path: Path = field(repr=False)
    member_name: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, str)
            or _OPERATION_ID_RE.fullmatch(self.operation_id) is None
        ):
            raise AssetStagingError("invalid_operation_id")
        object.__setattr__(self, "archive_path", Path(self.archive_path))
        _validate_member_name(self.member_name)


@dataclass(frozen=True)
class BatchStagePaths:
    root: Path = field(repr=False)
    batch: Path = field(repr=False)
    incoming: Path = field(repr=False)
    verified: Path = field(repr=False)
    quarantine: Path = field(repr=False)
    ledger: Path = field(repr=False)


@dataclass(frozen=True)
class AssetRecoveryReport:
    batch_id: str
    stage_plan_digest: str
    status: str
    verified_entities: int
    missing_entities: int
    incoming_residuals: int
    unknown_files: int
    ledger_valid: bool
    lease_present: bool
    warning_codes: tuple[str, ...]
    verification_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "stage_plan_digest": self.stage_plan_digest,
            "status": self.status,
            "verified_entities": self.verified_entities,
            "missing_entities": self.missing_entities,
            "incoming_residuals": self.incoming_residuals,
            "unknown_files": self.unknown_files,
            "ledger_valid": self.ledger_valid,
            "lease_present": self.lease_present,
            "warning_codes": list(self.warning_codes),
            "verification_digest": self.verification_digest,
        }


@dataclass(frozen=True)
class _LedgerState:
    sequence: int
    event_digest: str
    state: str


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _raw_path_guard(value: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AssetStagingError("invalid_stage_root")
    normalized = value.replace("/", "\\")
    lower = normalized.lower()
    if (
        _URL_SCHEME_RE.match(value)
        or normalized.startswith("\\\\?\\")
        or normalized.startswith("\\\\.\\")
        or normalized.startswith("\\\\")
    ):
        raise AssetStagingError("unsafe_stage_root")
    decoded_once = unquote(value)
    decoded_twice = unquote(decoded_once)
    for candidate in (value, decoded_once, decoded_twice):
        parts = candidate.replace("\\", "/").split("/")
        if any(part in {".", ".."} for part in parts):
            raise AssetStagingError("unsafe_stage_root")
    drive_prefix = 2 if _WINDOWS_DRIVE_RE.match(normalized) else 0
    if ":" in normalized[drive_prefix:]:
        raise AssetStagingError("unsafe_stage_root")
    if any(
        part.endswith((" ", "."))
        for part in normalized.split("\\")
        if part
    ):
        raise AssetStagingError("unsafe_stage_root")
    if "%2e" in lower or "%25" in lower or "%5c" in lower or "%2f" in lower:
        raise AssetStagingError("unsafe_stage_root")


def _existing_reparse_components(root: Path, target: Path) -> None:
    current = root
    if current.exists() and is_reparse_point(current):
        raise AssetStagingError("stage_root_reparse_forbidden")
    relative = target.relative_to(root)
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and is_reparse_point(current):
            raise AssetStagingError("stage_root_reparse_forbidden")


def validate_stage_root(path: str | os.PathLike[str]) -> Path:
    raw = os.fspath(path)
    _raw_path_guard(raw)
    unresolved = Path(raw).expanduser().absolute()
    if (unresolved.exists() or unresolved.is_symlink()) and is_reparse_point(unresolved):
        raise AssetStagingError("stage_root_reparse_forbidden")
    resolved = unresolved.resolve(strict=False)
    allowed_roots = tuple(
        root.resolve(strict=False)
        for root in (
            SYSTEM_TEMP_ROOT,
            NODE_OUTPUT_ROOT,
            PRODUCTION_EXECUTION_STAGING_ROOT,
        )
    )
    owner_root = next(
        (root for root in allowed_roots if _is_relative_to(resolved, root)),
        None,
    )
    if owner_root is None:
        raise AssetStagingError("stage_root_outside_allowed_roots")
    _existing_reparse_components(owner_root, resolved)
    production = (PROJECT_ROOT / "data").resolve(strict=False)
    imports = (PROJECT_ROOT / "imports").resolve(strict=False)
    if _is_relative_to(resolved, production) or _is_relative_to(resolved, imports):
        raise AssetStagingError("production_path_forbidden")
    return resolved


def _validate_member_name(member_name: str) -> str:
    if (
        not isinstance(member_name, str)
        or not member_name
        or "\x00" in member_name
        or _URL_SCHEME_RE.match(member_name)
    ):
        raise AssetStagingError("unsafe_zip_member")
    normalized = member_name.replace("\\", "/")
    decoded_once = unquote(normalized)
    decoded_twice = unquote(decoded_once)
    for candidate in (normalized, decoded_once, decoded_twice):
        if (
            candidate.startswith("/")
            or candidate.startswith("//")
            or _WINDOWS_DRIVE_RE.match(candidate)
            or candidate.startswith("//?/")
            or candidate.startswith("//./")
        ):
            raise AssetStagingError("unsafe_zip_member")
        pure = PurePosixPath(candidate)
        if any(part in {"", ".", ".."} for part in pure.parts):
            raise AssetStagingError("unsafe_zip_member")
        for part in pure.parts:
            if (
                ":" in part
                or part.endswith((" ", "."))
                or len(part) > 255
            ):
                raise AssetStagingError("unsafe_zip_member")
    if "%2e" in normalized.lower() or "%25" in normalized.lower():
        raise AssetStagingError("unsafe_zip_member")
    return normalized


def _validate_batch_id(batch_id: str) -> str:
    if (
        not isinstance(batch_id, str)
        or _BATCH_ID_RE.fullmatch(batch_id) is None
    ):
        raise AssetStagingError("invalid_batch_id")
    return batch_id


def _mkdir_controlled(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or is_reparse_point(path):
            raise AssetStagingError("stage_directory_unsafe")
        return
    path.mkdir()
    if not path.is_dir() or is_reparse_point(path):
        raise AssetStagingError("stage_directory_unsafe")


def prepare_batch_paths(
    stage_root: str | os.PathLike[str],
    batch_id: str,
) -> BatchStagePaths:
    root = validate_stage_root(stage_root)
    _validate_batch_id(batch_id)
    root.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(root):
        raise AssetStagingError("stage_root_reparse_forbidden")
    batches = root / "batches"
    _mkdir_controlled(batches)
    batch = batches / batch_id
    _mkdir_controlled(batch)
    incoming = batch / "incoming"
    verified = batch / "verified"
    quarantine = batch / "quarantine"
    ledger = batch / "ledger"
    for directory in (incoming, verified, quarantine, ledger):
        _mkdir_controlled(directory)
    for directory in (batch, incoming, verified, quarantine, ledger):
        resolved = directory.resolve(strict=True)
        if not _is_relative_to(resolved, root) or is_reparse_point(directory):
            raise AssetStagingError("stage_directory_escape")
    return BatchStagePaths(
        root=root,
        batch=batch,
        incoming=incoming,
        verified=verified,
        quarantine=quarantine,
        ledger=ledger,
    )


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temp_path = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    data = canonical_json_bytes(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temp_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json(path: Path, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AssetStagingError(reason_code) from None
    if not isinstance(value, dict):
        raise AssetStagingError(reason_code)
    return value


def _ensure_ownership(paths: BatchStagePaths, plan: AssetStagePlan) -> None:
    ownership_path = paths.ledger / "ownership.json"
    payload = {
        "ledger_version": ASSET_STAGE_LEDGER_VERSION,
        "batch_id": plan.batch_id,
        "stage_plan_digest": plan.stage_plan_digest,
        "ownership_digest": normalized_json_digest(
            {
                "ledger_version": ASSET_STAGE_LEDGER_VERSION,
                "batch_id": plan.batch_id,
                "stage_plan_digest": plan.stage_plan_digest,
            }
        ),
    }
    if ownership_path.exists():
        if is_reparse_point(ownership_path):
            raise AssetStagingError("ownership_record_unsafe")
        if _read_json(ownership_path, "ownership_record_corrupt") != payload:
            raise AssetStagingError("batch_ownership_mismatch")
        return
    _atomic_json(ownership_path, payload)


def _lease_path(paths: BatchStagePaths) -> Path:
    return paths.ledger / "lease.json"


def _acquire_lease(paths: BatchStagePaths, plan: AssetStagePlan) -> str:
    lease_path = _lease_path(paths)
    token = "lease_" + uuid.uuid4().hex
    payload = {
        "ledger_version": ASSET_STAGE_LEDGER_VERSION,
        "batch_id": plan.batch_id,
        "stage_plan_digest": plan.stage_plan_digest,
        "lease_token_digest": normalized_json_digest({"lease_token": token}),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lease_path, flags, 0o600)
    except FileExistsError:
        raise AssetStagingError("batch_lease_conflict") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        lease_path.unlink(missing_ok=True)
        raise
    return token


def _lease_matches(paths: BatchStagePaths, token: str) -> bool:
    lease_path = _lease_path(paths)
    if not lease_path.exists() or is_reparse_point(lease_path):
        return False
    value = _read_json(lease_path, "lease_record_corrupt")
    expected = normalized_json_digest({"lease_token": token})
    return value.get("lease_token_digest") == expected


def _release_lease(paths: BatchStagePaths, token: str) -> None:
    if not _lease_matches(paths, token):
        raise AssetStagingError("lease_authority_mismatch")
    _lease_path(paths).unlink()


def _read_ledger_state(paths: BatchStagePaths) -> _LedgerState:
    events = read_stage_ledger_events(paths)
    if not events:
        return _LedgerState(0, "0" * 64, "discovered")
    last = events[-1]
    return _LedgerState(
        int(last["sequence"]),
        str(last["event_digest"]),
        str(last["next_state"]),
    )


def read_stage_ledger_events(
    paths_or_root: BatchStagePaths | str | os.PathLike[str],
    batch_id: str | None = None,
) -> list[dict[str, Any]]:
    if isinstance(paths_or_root, BatchStagePaths):
        paths = paths_or_root
    else:
        if batch_id is None:
            raise AssetStagingError("batch_id_required")
        paths = prepare_batch_paths(paths_or_root, batch_id)
    ledger_path = paths.ledger / "events.jsonl"
    if not ledger_path.exists():
        return []
    if is_reparse_point(ledger_path):
        raise AssetStagingError("ledger_file_unsafe")
    try:
        raw_lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise AssetStagingError("stage_ledger_corrupt") from None
    events: list[dict[str, Any]] = []
    previous = "0" * 64
    for index, raw in enumerate(raw_lines, start=1):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            raise AssetStagingError("stage_ledger_corrupt") from None
        if not isinstance(event, dict):
            raise AssetStagingError("stage_ledger_corrupt")
        required = {
            "ledger_version",
            "sequence",
            "operation_id",
            "entity_digest",
            "previous_state",
            "next_state",
            "bytes_processed",
            "reason_code",
            "previous_event_digest",
            "event_digest",
        }
        if set(event) != required or event.get("ledger_version") != ASSET_STAGE_LEDGER_VERSION:
            raise AssetStagingError("stage_ledger_corrupt")
        if event.get("sequence") != index or event.get("previous_event_digest") != previous:
            raise AssetStagingError("stage_ledger_chain_mismatch")
        unsigned = {key: value for key, value in event.items() if key != "event_digest"}
        if _canonical_digest(unsigned) != event.get("event_digest"):
            raise AssetStagingError("stage_ledger_chain_mismatch")
        previous = str(event["event_digest"])
        events.append(event)
    return events


def _append_event(
    paths: BatchStagePaths,
    *,
    operation_id: str | None,
    entity_digest: str | None,
    previous_state: str,
    next_state: str,
    bytes_processed: int,
    reason_code: str,
    fault_injector: Callable[[str], None] | None = None,
) -> _LedgerState:
    if _SAFE_LEDGER_REASON_RE.fullmatch(reason_code) is None:
        reason_code = "redacted_error_detail"
    if fault_injector is not None:
        fault_injector("before_ledger_write")
    state = _read_ledger_state(paths)
    if state.state != previous_state:
        raise AssetStagingError("stage_ledger_state_mismatch")
    unsigned = {
        "ledger_version": ASSET_STAGE_LEDGER_VERSION,
        "sequence": state.sequence + 1,
        "operation_id": operation_id,
        "entity_digest": entity_digest,
        "previous_state": previous_state,
        "next_state": next_state,
        "bytes_processed": bytes_processed,
        "reason_code": reason_code,
        "previous_event_digest": state.event_digest,
    }
    event = {**unsigned, "event_digest": _canonical_digest(unsigned)}
    ledger_path = paths.ledger / "events.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(ledger_path, flags, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(canonical_json_bytes(event) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    snapshot = {
        "ledger_version": ASSET_STAGE_LEDGER_VERSION,
        "sequence": event["sequence"],
        "event_digest": event["event_digest"],
        "state": next_state,
    }
    _atomic_json(paths.ledger / "state.json", snapshot)
    return _LedgerState(
        int(event["sequence"]),
        str(event["event_digest"]),
        next_state,
    )


def _zip_info_is_link(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _archive_guard(path: Path) -> Path:
    raw = os.fspath(path)
    if _URL_SCHEME_RE.match(raw) or "\x00" in raw:
        raise AssetStagingError("external_url_forbidden")
    unresolved = path.expanduser().absolute()
    if (
        not unresolved.exists()
        or not unresolved.is_file()
        or unresolved.is_symlink()
        or is_reparse_point(unresolved)
    ):
        raise AssetStagingError("unsafe_source_archive")
    return unresolved.resolve(strict=True)


def _sniff_mime(prefix: bytes) -> str:
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
    if prefix:
        try:
            text = prefix.decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream"
        if all(ord(char) >= 32 or char in "\r\n\t" for char in text):
            return "text/plain"
    return "application/octet-stream"


def _hash_file(path: Path, chunk_size: int) -> tuple[str, int, str]:
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            if len(prefix) < 8192:
                prefix.extend(chunk[: 8192 - len(prefix)])
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, _sniff_mime(bytes(prefix))


def _incoming_path(paths: BatchStagePaths, operation_id: str) -> Path:
    return paths.incoming / (
        "incoming_" + operation_id + "_" + uuid.uuid4().hex + ".part"
    )


def _quarantine(
    paths: BatchStagePaths,
    temp_path: Path,
    operation_id: str,
    reason_code: str,
) -> bool:
    if not temp_path.exists():
        return False
    safe_reason = (
        reason_code
        if _SAFE_LEDGER_REASON_RE.fullmatch(reason_code)
        else "validation_failed"
    )
    target = paths.quarantine / (
        "quarantine_"
        + operation_id
        + "_"
        + safe_reason
        + "_"
        + uuid.uuid4().hex
        + ".part"
    )
    os.replace(temp_path, target)
    return True


def _result(
    *,
    plan: AssetStagePlan,
    status: str,
    staged: int,
    reused: int,
    quarantined: int,
    bytes_processed: int,
    warnings: Sequence[str],
    reasons: Sequence[str],
    ledger_digest: str | None,
) -> AssetStageResult:
    verification = {
        "batch_id": plan.batch_id,
        "stage_plan_digest": plan.stage_plan_digest,
        "status": status,
        "entities_staged": staged,
        "entities_reused": reused,
        "entities_quarantined": quarantined,
        "bytes_processed": bytes_processed,
        "warning_codes": sorted(set(warnings)),
        "reason_codes": sorted(set(reasons)),
        "ledger_digest": ledger_digest,
        "asset_stage_fingerprint": (
            plan.asset_stage_fingerprint.to_dict()
            if status in {"staged", "staged_with_warnings"}
            else None
        ),
    }
    return AssetStageResult(
        batch_id=plan.batch_id,
        stage_plan_digest=plan.stage_plan_digest,
        status=status,
        entities_staged=staged,
        entities_reused=reused,
        entities_quarantined=quarantined,
        bytes_processed=bytes_processed,
        asset_stage_fingerprint=(
            plan.asset_stage_fingerprint
            if status in {"staged", "staged_with_warnings"}
            else None
        ),
        warning_codes=tuple(sorted(set(warnings))),
        reason_codes=tuple(sorted(set(reasons))),
        ledger_digest=ledger_digest,
        verification_digest=_canonical_digest(verification),
    )


def _source_mapping(
    plan: AssetStagePlan,
    sources: Sequence[ZipAssetSource],
) -> Mapping[str, ZipAssetSource]:
    result: dict[str, ZipAssetSource] = {}
    for source in sources:
        if not isinstance(source, ZipAssetSource):
            raise AssetStagingError("invalid_asset_source")
        if source.operation_id in result:
            raise AssetStagingError("duplicate_asset_source")
        result[source.operation_id] = source
    planned = {item.operation_id for item in plan.operations}
    if set(result) - planned:
        raise AssetStagingError("unplanned_asset_source")
    if planned - set(result):
        raise AssetStagingError("planned_asset_source_missing")
    return MappingProxyType(result)


def _check_cancel(
    cancel_check: Callable[[], bool] | None,
) -> None:
    if cancel_check is not None and cancel_check():
        raise _Cancellation()


def _topological_operations(
    operations: Sequence[AssetStageOperation],
) -> tuple[AssetStageOperation, ...]:
    pending = {item.operation_id: item for item in operations}
    completed: set[str] = set()
    ordered: list[AssetStageOperation] = []
    while pending:
        ready = sorted(
            (
                item
                for item in pending.values()
                if set(item.dependency_operation_ids).issubset(completed)
            ),
            key=lambda item: item.operation_id,
        )
        if not ready:
            raise AssetStagingError("asset_stage_dependency_unsatisfied")
        for item in ready:
            ordered.append(item)
            completed.add(item.operation_id)
            pending.pop(item.operation_id)
    return tuple(ordered)


def _stream_operation(
    *,
    operation: AssetStageOperation,
    source: ZipAssetSource,
    paths: BatchStagePaths,
    limits: AssetStageLimits,
    physical_before: int,
    cancel_check: Callable[[], bool] | None,
    fault_injector: Callable[[str], None] | None,
) -> tuple[str, int, bool]:
    _check_cancel(cancel_check)
    archive_path = _archive_guard(source.archive_path)
    member_name = _validate_member_name(source.member_name)
    if operation.actual_size > limits.max_entity_bytes:
        raise AssetStagingError("asset_entity_size_limit_exceeded")
    free = shutil.disk_usage(paths.batch).free
    if free - operation.actual_size < limits.min_free_disk_bytes:
        raise AssetStagingError("insufficient_disk_space")
    started = time.monotonic()
    temp_path = _incoming_path(paths, operation.operation_id)
    quarantined = False
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > limits.max_zip_entries:
                raise AssetStagingError("zip_entry_limit_exceeded")
            matching = [item for item in infos if item.filename == member_name]
            if len(matching) != 1:
                raise AssetStagingError(
                    "zip_member_missing" if not matching else "duplicate_zip_member"
                )
            info = matching[0]
            _validate_member_name(info.filename)
            if info.is_dir() or _zip_info_is_link(info):
                raise AssetStagingError("unsafe_zip_member")
            if info.file_size > limits.max_entity_bytes:
                raise AssetStagingError("asset_entity_size_limit_exceeded")
            if (
                info.file_size > 0
                and info.file_size
                > max(1, info.compress_size) * limits.max_compression_ratio
            ):
                raise AssetStagingError("zip_compression_ratio_exceeded")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temp_path, flags, 0o600)
            digest = hashlib.sha256()
            actual = 0
            prefix = bytearray()
            with archive.open(info, "r") as source_handle:
                with os.fdopen(descriptor, "wb") as output_handle:
                    while True:
                        _check_cancel(cancel_check)
                        if time.monotonic() - started > limits.max_operation_seconds:
                            raise AssetStagingError("asset_operation_timeout")
                        chunk = source_handle.read(limits.chunk_size)
                        if not chunk:
                            break
                        actual += len(chunk)
                        if actual > limits.max_entity_bytes:
                            raise AssetStagingError(
                                "asset_entity_size_limit_exceeded"
                            )
                        if (
                            physical_before + actual
                            > limits.max_batch_physical_bytes
                        ):
                            raise AssetStagingError(
                                "asset_batch_physical_limit_exceeded"
                            )
                        if len(prefix) < 8192:
                            prefix.extend(chunk[: 8192 - len(prefix)])
                        digest.update(chunk)
                        output_handle.write(chunk)
                        if fault_injector is not None:
                            fault_injector("during_stream_write")
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
        if fault_injector is not None:
            fault_injector("after_incoming_close")
        actual_digest = digest.hexdigest()
        actual_mime = _sniff_mime(bytes(prefix))
        reason: str | None = None
        if actual != info.file_size or actual != operation.actual_size:
            reason = "asset_size_mismatch"
        elif actual_digest != operation.entity_digest:
            reason = "asset_digest_mismatch"
        elif actual_mime != operation.detected_mime:
            reason = "asset_mime_mismatch"
        if reason is not None:
            quarantined = _quarantine(
                paths, temp_path, operation.operation_id, reason
            )
            raise AssetStagingError(reason)
        target = paths.verified / ("sha256_" + operation.entity_digest)
        if target.exists() or target.is_symlink():
            if (
                not target.is_file()
                or target.is_symlink()
                or is_reparse_point(target)
            ):
                raise AssetStagingError("verified_entity_path_unsafe")
            existing_digest, existing_size, existing_mime = _hash_file(
                target, limits.chunk_size
            )
            if (
                existing_digest != operation.entity_digest
                or existing_size != operation.actual_size
                or existing_mime != operation.detected_mime
            ):
                raise AssetStagingError("verified_entity_collision")
            temp_path.unlink()
            return "reused", actual, quarantined
        if fault_injector is not None:
            fault_injector("before_atomic_move")
        os.replace(temp_path, target)
        if fault_injector is not None:
            fault_injector("after_atomic_move")
        final_digest, final_size, final_mime = _hash_file(
            target, limits.chunk_size
        )
        if (
            final_digest != operation.entity_digest
            or final_size != operation.actual_size
            or final_mime != operation.detected_mime
        ):
            raise AssetStagingError("post_move_verification_failed")
        return "staged", actual, quarantined
    except AssetStagingInterrupted:
        raise
    except _Cancellation as exc:
        if temp_path.exists():
            quarantined = _quarantine(
                paths, temp_path, operation.operation_id, "cancelled"
            )
        exc.quarantined = quarantined
        raise
    except (AssetStagingError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        if temp_path.exists():
            reason = (
                exc.reason_code
                if isinstance(exc, AssetStagingError)
                else "staging_io_error"
            )
            quarantined = _quarantine(
                paths, temp_path, operation.operation_id, reason
            )
        if isinstance(exc, AssetStagingError):
            exc.quarantined = quarantined
            raise
        raise AssetStagingError(
            "staging_io_error",
            quarantined=quarantined,
        ) from None


def execute_asset_stage_plan(
    *,
    plan: AssetStagePlan,
    sources: Sequence[ZipAssetSource],
    stage_root: str | os.PathLike[str],
    limits: AssetStageLimits = AssetStageLimits(),
    cancel_check: Callable[[], bool] | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> AssetStageResult:
    """Execute a plan inside one controlled, leased batch directory."""
    if not isinstance(plan, AssetStagePlan):
        raise AssetStagingError("invalid_asset_stage_plan")
    if not isinstance(limits, AssetStageLimits):
        raise AssetStagingError("invalid_asset_stage_limits")
    if len(plan.operations) > limits.max_entities:
        raise AssetStagingError("asset_entity_limit_exceeded")
    if len(plan.occurrence_bindings) > limits.max_occurrences:
        raise AssetStagingError("asset_occurrence_limit_exceeded")
    if (
        plan.expected_asset_delta["physical_bytes"]
        > limits.max_batch_physical_bytes
    ):
        raise AssetStagingError("asset_batch_physical_limit_exceeded")
    if (
        plan.expected_asset_delta["logical_bytes"]
        > limits.max_batch_logical_bytes
    ):
        raise AssetStagingError("asset_batch_logical_limit_exceeded")
    source_by_operation = _source_mapping(plan, sources)
    paths = prepare_batch_paths(stage_root, plan.batch_id)
    _ensure_ownership(paths, plan)
    lease_token = _acquire_lease(paths, plan)
    staged = 0
    reused = 0
    quarantined = 0
    processed = 0
    warnings = list(plan.warning_codes)
    reasons: list[str] = []
    lease_should_release = True
    try:
        state = _read_ledger_state(paths)
        if state.state == "discovered":
            state = _append_event(
                paths,
                operation_id=None,
                entity_digest=None,
                previous_state="discovered",
                next_state="planned",
                bytes_processed=0,
                reason_code="plan_bound",
                fault_injector=fault_injector,
            )
        if state.state == "planned":
            state = _append_event(
                paths,
                operation_id=None,
                entity_digest=None,
                previous_state="planned",
                next_state="staging",
                bytes_processed=0,
                reason_code="staging_started",
                fault_injector=fault_injector,
            )
        elif state.state in {"staged", "staged_with_warnings"}:
            report = inspect_asset_stage_batch(
                stage_root=paths.root,
                batch_id=plan.batch_id,
                plan=plan,
                ignore_lease=True,
            )
            if report.status not in {"staged", "staged_with_warnings"}:
                raise AssetStagingError("completed_stage_revalidation_failed")
            return _result(
                plan=plan,
                status=report.status,
                staged=0,
                reused=report.verified_entities,
                quarantined=0,
                bytes_processed=0,
                warnings=tuple(set(warnings) | set(report.warning_codes)),
                reasons=(),
                ledger_digest=state.event_digest,
            )
        elif state.state != "staging":
            raise AssetStagingError("batch_recovery_required")
        _check_cancel(cancel_check)
        for operation in _topological_operations(plan.operations):
            outcome, byte_count, was_quarantined = _stream_operation(
                operation=operation,
                source=source_by_operation[operation.operation_id],
                paths=paths,
                limits=limits,
                physical_before=processed,
                cancel_check=cancel_check,
                fault_injector=fault_injector,
            )
            processed += byte_count
            quarantined += int(was_quarantined)
            if outcome == "staged":
                staged += 1
            else:
                reused += 1
            state = _append_event(
                paths,
                operation_id=operation.operation_id,
                entity_digest=operation.entity_digest,
                previous_state="staging",
                next_state="staging",
                bytes_processed=byte_count,
                reason_code=(
                    "entity_staged" if outcome == "staged" else "entity_reused"
                ),
                fault_injector=fault_injector,
            )
        state = _append_event(
            paths,
            operation_id=None,
            entity_digest=None,
            previous_state="staging",
            next_state="verifying",
            bytes_processed=processed,
            reason_code="streaming_complete",
            fault_injector=fault_injector,
        )
        for operation in plan.operations:
            target = paths.verified / ("sha256_" + operation.entity_digest)
            digest, size, mime = _hash_file(target, limits.chunk_size)
            if (
                digest != operation.entity_digest
                or size != operation.actual_size
                or mime != operation.detected_mime
            ):
                raise AssetStagingError("final_batch_verification_failed")
        final_status = "staged_with_warnings" if warnings else "staged"
        state = _append_event(
            paths,
            operation_id=None,
            entity_digest=None,
            previous_state="verifying",
            next_state=final_status,
            bytes_processed=processed,
            reason_code="batch_verified",
            fault_injector=fault_injector,
        )
        return _result(
            plan=plan,
            status=final_status,
            staged=staged,
            reused=reused,
            quarantined=quarantined,
            bytes_processed=processed,
            warnings=warnings,
            reasons=reasons,
            ledger_digest=state.event_digest,
        )
    except AssetStagingInterrupted as exc:
        lease_should_release = False
        exc.lease_token = lease_token
        raise
    except _Cancellation as exc:
        quarantined += int(exc.quarantined)
        reasons.append("cancelled")
        try:
            state = _read_ledger_state(paths)
            if state.state in {"planned", "staging", "verifying"}:
                state = _append_event(
                    paths,
                    operation_id=None,
                    entity_digest=None,
                    previous_state=state.state,
                    next_state="cancelled",
                    bytes_processed=processed,
                    reason_code="cancelled",
                )
            ledger_digest = state.event_digest
        except (AssetStagingError, OSError):
            ledger_digest = None
        return _result(
            plan=plan,
            status="cancelled",
            staged=staged,
            reused=reused,
            quarantined=quarantined,
            bytes_processed=processed,
            warnings=warnings,
            reasons=reasons,
            ledger_digest=ledger_digest,
        )
    except (AssetStagingError, AssetPlanningError, OSError) as exc:
        reason = (
            exc.reason_code
            if hasattr(exc, "reason_code")
            else "staging_io_error"
        )
        reasons.append(reason)
        quarantined += int(getattr(exc, "quarantined", False))
        status = (
            "recovery_required"
            if reason
            in {
                "stage_ledger_corrupt",
                "stage_ledger_chain_mismatch",
                "stage_ledger_state_mismatch",
                "batch_recovery_required",
                "post_move_verification_failed",
                "stage_ledger_write_failed",
            }
            else "failed"
        )
        try:
            state = _read_ledger_state(paths)
            if state.state in {
                "planned",
                "staging",
                "verifying",
            }:
                state = _append_event(
                    paths,
                    operation_id=None,
                    entity_digest=None,
                    previous_state=state.state,
                    next_state=status,
                    bytes_processed=processed,
                    reason_code=reason,
                )
            ledger_digest = state.event_digest
        except (AssetStagingError, OSError):
            status = "recovery_required"
            ledger_digest = None
        return _result(
            plan=plan,
            status=status,
            staged=staged,
            reused=reused,
            quarantined=quarantined,
            bytes_processed=processed,
            warnings=warnings,
            reasons=reasons,
            ledger_digest=ledger_digest,
        )
    finally:
        if lease_should_release and _lease_path(paths).exists():
            try:
                _release_lease(paths, lease_token)
            except AssetStagingError:
                pass


def inspect_asset_stage_batch(
    *,
    stage_root: str | os.PathLike[str],
    batch_id: str,
    plan: AssetStagePlan,
    ignore_lease: bool = False,
) -> AssetRecoveryReport:
    if plan.batch_id != batch_id:
        raise AssetStagingError("batch_plan_binding_mismatch")
    paths = prepare_batch_paths(stage_root, batch_id)
    _ensure_ownership(paths, plan)
    warnings: set[str] = set()
    lease_present = _lease_path(paths).exists()
    try:
        events = read_stage_ledger_events(paths)
        ledger_valid = True
    except AssetStagingError:
        events = []
        ledger_valid = False
        warnings.add("ledger_invalid")
    incoming = [
        item for item in paths.incoming.iterdir()
        if item.is_file() or item.is_symlink()
    ]
    if incoming:
        warnings.add("incoming_residual")
    expected_names = {
        "sha256_" + operation.entity_digest: operation
        for operation in plan.operations
    }
    verified = 0
    missing = 0
    unknown = 0
    for name, operation in expected_names.items():
        target = paths.verified / name
        if not target.exists():
            missing += 1
            continue
        if (
            not target.is_file()
            or target.is_symlink()
            or is_reparse_point(target)
        ):
            warnings.add("verified_entity_unsafe")
            continue
        digest, size, mime = _hash_file(target, 1024 * 1024)
        if (
            digest == operation.entity_digest
            and size == operation.actual_size
            and mime == operation.detected_mime
        ):
            verified += 1
        else:
            warnings.add("verified_entity_mismatch")
    for item in paths.verified.iterdir():
        if item.name not in expected_names:
            unknown += 1
    for item in paths.quarantine.iterdir():
        if not item.name.startswith("quarantine_astageop_"):
            unknown += 1
    if unknown:
        warnings.add("unknown_file_preserved")
    final_state = events[-1]["next_state"] if events else "discovered"
    complete = (
        ledger_valid
        and not incoming
        and not missing
        and verified == len(plan.operations)
        and unknown == 0
        and final_state in {"staged", "staged_with_warnings"}
        and (ignore_lease or not lease_present)
    )
    if complete:
        status = (
            "staged_with_warnings"
            if final_state == "staged_with_warnings" or warnings
            else "staged"
        )
    else:
        status = "recovery_required"
        if lease_present and not ignore_lease:
            warnings.add("lease_present_no_auto_takeover")
    payload = {
        "batch_id": batch_id,
        "stage_plan_digest": plan.stage_plan_digest,
        "status": status,
        "verified_entities": verified,
        "missing_entities": missing,
        "incoming_residuals": len(incoming),
        "unknown_files": unknown,
        "ledger_valid": ledger_valid,
        "lease_present": lease_present,
        "warning_codes": sorted(warnings),
    }
    return AssetRecoveryReport(
        batch_id=batch_id,
        stage_plan_digest=plan.stage_plan_digest,
        status=status,
        verified_entities=verified,
        missing_entities=missing,
        incoming_residuals=len(incoming),
        unknown_files=unknown,
        ledger_valid=ledger_valid,
        lease_present=lease_present,
        warning_codes=tuple(sorted(warnings)),
        verification_digest=_canonical_digest(payload),
    )


def recover_asset_stage_batch(
    *,
    stage_root: str | os.PathLike[str],
    batch_id: str,
    plan: AssetStagePlan,
    lease_token: str,
) -> AssetRecoveryReport:
    """Reconcile a crashed batch only with its exact, non-expired lease token."""
    paths = prepare_batch_paths(stage_root, batch_id)
    _ensure_ownership(paths, plan)
    if not _lease_matches(paths, lease_token):
        raise AssetStagingError("lease_authority_mismatch")
    known_operation_ids = {item.operation_id for item in plan.operations}
    unknown_incoming = 0
    for item in list(paths.incoming.iterdir()):
        matching = next(
            (
                operation_id
                for operation_id in known_operation_ids
                if item.name.startswith("incoming_" + operation_id + "_")
            ),
            None,
        )
        if matching is None:
            unknown_incoming += 1
            continue
        if item.is_file() and not item.is_symlink() and not is_reparse_point(item):
            _quarantine(paths, item, matching, "crash_residual")
    report = inspect_asset_stage_batch(
        stage_root=paths.root,
        batch_id=batch_id,
        plan=plan,
        ignore_lease=True,
    )
    if (
        report.verified_entities == len(plan.operations)
        and report.missing_entities == 0
        and unknown_incoming == 0
        and report.unknown_files == 0
        and report.ledger_valid
    ):
        state = _read_ledger_state(paths)
        if state.state in {"staging", "verifying", "recovery_required"}:
            if state.state == "staging":
                state = _append_event(
                    paths,
                    operation_id=None,
                    entity_digest=None,
                    previous_state="staging",
                    next_state="verifying",
                    bytes_processed=0,
                    reason_code="crash_recovery_reverify",
                )
            state = _append_event(
                paths,
                operation_id=None,
                entity_digest=None,
                previous_state=state.state,
                next_state="staged",
                bytes_processed=0,
                reason_code="crash_recovery_complete",
            )
    else:
        state = _read_ledger_state(paths)
        if state.state in {"staging", "verifying"}:
            _append_event(
                paths,
                operation_id=None,
                entity_digest=None,
                previous_state=state.state,
                next_state="recovery_required",
                bytes_processed=0,
                reason_code="crash_recovery_incomplete",
            )
    _release_lease(paths, lease_token)
    return inspect_asset_stage_batch(
        stage_root=paths.root,
        batch_id=batch_id,
        plan=plan,
    )


def rollback_asset_stage_batch(
    *,
    stage_root: str | os.PathLike[str],
    batch_id: str,
    plan: AssetStagePlan,
) -> AssetStageResult:
    """Remove only files proven to belong to this isolated batch."""
    paths = prepare_batch_paths(stage_root, batch_id)
    _ensure_ownership(paths, plan)
    lease_token = _acquire_lease(paths, plan)
    removed = 0
    warnings: set[str] = set()
    try:
        state = _read_ledger_state(paths)
        if state.state not in {
            "staged",
            "staged_with_warnings",
            "failed",
            "cancelled",
            "recovery_required",
        }:
            raise AssetStagingError("rollback_state_forbidden")
        state = _append_event(
            paths,
            operation_id=None,
            entity_digest=None,
            previous_state=state.state,
            next_state="rolling_back",
            bytes_processed=0,
            reason_code="rollback_started",
        )
        for operation in plan.operations:
            target = paths.verified / ("sha256_" + operation.entity_digest)
            if target.exists():
                if (
                    target.is_file()
                    and not target.is_symlink()
                    and not is_reparse_point(target)
                ):
                    digest, size, mime = _hash_file(target, 1024 * 1024)
                    if (
                        digest == operation.entity_digest
                        and size == operation.actual_size
                        and mime == operation.detected_mime
                    ):
                        target.unlink()
                        removed += operation.actual_size
                    else:
                        warnings.add("unknown_file_preserved")
                else:
                    warnings.add("unknown_file_preserved")
            for item in list(paths.incoming.glob(
                "incoming_" + operation.operation_id + "_*.part"
            )):
                if (
                    item.is_file()
                    and not item.is_symlink()
                    and not is_reparse_point(item)
                ):
                    item.unlink()
        known_verified = {
            "sha256_" + operation.entity_digest for operation in plan.operations
        }
        if any(item.name not in known_verified for item in paths.verified.iterdir()):
            warnings.add("unknown_file_preserved")
        state = _append_event(
            paths,
            operation_id=None,
            entity_digest=None,
            previous_state="rolling_back",
            next_state="rolled_back",
            bytes_processed=0,
            reason_code="rollback_complete",
        )
        return _result(
            plan=plan,
            status="rolled_back",
            staged=0,
            reused=0,
            quarantined=0,
            bytes_processed=removed,
            warnings=tuple(warnings),
            reasons=(),
            ledger_digest=state.event_digest,
        )
    finally:
        if _lease_path(paths).exists():
            _release_lease(paths, lease_token)
