"""Fail-closed production execution entry for exact YunOS migration v8301.

The public entry requires a v2 approval package plus a separate immutable
HumanAuthorizationRecord.  It accepts only the code-defined Main database and
reaches the existing v8301 executor only after every production preflight gate
passes.  Import, backfill, assets, v8302, and generic/latest migration are not
reachable from this module.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, Mapping

from memory.db_path_policy import DEFAULT_REAL_DB_PATH, is_reparse_point
from memory.ingestion_core import canonical_json_bytes, sanitize_reason_code
from memory.ingestion_migrations import (
    HISTORICAL_MIGRATION_VERSION,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
    MigrationPlan,
    MigrationRehearsalError,
    MigrationResult,
    _apply_v8301_migration_to_validated_target,
    _lock_path,
    build_migration_plan,
    inspect_schema_connection,
)
from memory.production_migration_authorization import (
    APPROVAL_PACKAGE_SCHEMA_VERSION,
    ProductionMigrationAuthorization,
    ProductionMigrationAuthorizationError,
    canonical_document_digest,
    load_json_document,
    migration_plan_digest,
    validate_approval_package,
    validate_authorization_binding,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_MAIN_DATABASE = (PROJECT_ROOT / "data" / "ayunos_chatgpt.db").absolute()
APPROVAL_OUTPUT_ROOT = (
    PROJECT_ROOT / "output" / "v8.3-production-migration-entry"
).absolute()
APPROVAL_PACKAGE_PATH = (
    APPROVAL_OUTPUT_ROOT / "production_v8301_migration_approval_package_v2.json"
)
EXECUTION_BASE = APPROVAL_OUTPUT_ROOT / "executions"
EXECUTION_STATE_LEDGER = APPROVAL_OUTPUT_ROOT / "execution_state.jsonl"

BACKUP_RECORD_SCHEMA_VERSION = "v8.3-v8301-backup-verification-1"
SERVICE_RECORD_SCHEMA_VERSION = "v8.3-v8301-service-quiescence-1"
JOURNAL_SCHEMA_VERSION = "v8.3-v8301-execution-journal-1"
EXECUTION_STATE_SCHEMA_VERSION = "v8.3-v8301-execution-state-1"

REQUIRED_JOURNAL_EVENTS = frozenset({
    "authorization_validated",
    "production_path_validated",
    "source_fingerprint_validated",
    "services_quiesced_validated",
    "backup_validated",
    "lease_acquired",
    "transaction_started",
    "migration_applied",
    "marker_written",
    "transaction_committed",
    "verification_started",
    "verification_passed",
    "execution_completed",
    "execution_failed",
    "recovery_required",
})

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")
_BACKUP_LABELS = ("Main", "Profile", "Semantic", "Runtime")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ProductionMigrationExecutionError(RuntimeError):
    """Privacy-safe execution error with an explicit recovery classification."""

    def __init__(self, reason_code: str, *, recovery_required: bool = False) -> None:
        reason = sanitize_reason_code(reason_code)
        super().__init__(reason)
        self.reason_code = reason
        self.recovery_required = bool(recovery_required)


@dataclass(frozen=True)
class GitExecutionState:
    commit: str
    tracked_worktree_clean: bool
    staging_empty: bool


@dataclass(frozen=True)
class ProductionMigrationRequest:
    target_version: int
    approval_package: Path
    authorization_record: Path
    production_db: Path
    expected_db_sha256: str
    expected_current_schema: str
    expected_target_schema: str
    expected_migration_digest: str
    expected_plan_digest: str
    execution_root: Path


@dataclass(frozen=True)
class ProductionMigrationPreflight:
    request: ProductionMigrationRequest
    authorization: ProductionMigrationAuthorization
    package: Mapping[str, Any]
    plan: MigrationPlan
    production_database: Path
    execution_root: Path
    git_state: GitExecutionState


@dataclass(frozen=True)
class ProductionMigrationOutcome:
    status: str
    migration_version: int
    migration_digest: str
    recovery_required: bool


def validate_target_version(value: Any) -> int:
    if type(value) is not int or value != MIGRATION_VERSION:
        raise ProductionMigrationExecutionError("target_version_must_be_exact_8301")
    return value


def _sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProductionMigrationExecutionError(reason)
    return value


def _identity(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise ProductionMigrationExecutionError(reason)
    return value


def _commit(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ProductionMigrationExecutionError(reason)
    return value


def _timestamp(value: Any, reason: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProductionMigrationExecutionError(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ProductionMigrationExecutionError(reason) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProductionMigrationExecutionError(reason)
    return parsed


def _utc_text(value: datetime | None = None) -> str:
    current = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise ProductionMigrationExecutionError("database_hash_failed") from None
    return digest.hexdigest()


def _raw_path_safe(value: str) -> bool:
    lowered = value.lower()
    if (
        not value
        or "\x00" in value
        or value.startswith(("\\\\", "//"))
        or "\\\\?\\" in lowered
        or "\\\\.\\" in lowered
    ):
        return False
    drive, tail = os.path.splitdrive(value)
    if ":" in tail or (drive and re.fullmatch(r"[A-Za-z]:", drive) is None):
        return False
    return not any(
        part in {"", ".", ".."} or part.endswith((" ", "."))
        for part in re.split(r"[\\/]", tail)
        if part != ""
    )


def _reject_reparse_components(path: Path) -> None:
    current = path
    while True:
        if (current.exists() or current.is_symlink()) and is_reparse_point(current):
            raise ProductionMigrationExecutionError("production_path_reparse_forbidden")
        if current.parent == current:
            break
        current = current.parent


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def validate_production_main_database_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not _raw_path_safe(raw):
        raise ProductionMigrationExecutionError("production_path_unsafe")
    supplied = Path(raw)
    if not supplied.is_absolute():
        raise ProductionMigrationExecutionError("production_path_not_absolute")
    if not _same_path(supplied, PRODUCTION_MAIN_DATABASE):
        raise ProductionMigrationExecutionError("production_database_mismatch")
    _reject_reparse_components(supplied)
    try:
        supplied_resolved = supplied.resolve(strict=True)
        configured_resolved = PRODUCTION_MAIN_DATABASE.resolve(strict=True)
    except OSError:
        raise ProductionMigrationExecutionError("production_database_unavailable") from None
    if (
        supplied.is_symlink()
        or not supplied_resolved.is_file()
        or supplied_resolved != configured_resolved
    ):
        raise ProductionMigrationExecutionError("production_database_mismatch")
    return supplied_resolved


def _validate_controlled_execution_root(
    value: Path,
    *,
    execution_id: str | None = None,
) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not _raw_path_safe(raw):
        raise ProductionMigrationExecutionError("execution_root_unsafe")
    target = Path(raw)
    if not target.is_absolute():
        raise ProductionMigrationExecutionError("execution_root_not_absolute")
    expected_parent = EXECUTION_BASE
    if execution_id is not None:
        _identity(execution_id, "one_time_execution_id_invalid")
        expected = expected_parent / execution_id
        if not _same_path(target, expected):
            raise ProductionMigrationExecutionError("execution_root_identity_mismatch")
    else:
        try:
            target.relative_to(expected_parent)
        except ValueError:
            raise ProductionMigrationExecutionError("execution_root_outside_boundary") from None
    _reject_reparse_components(target)
    if not target.is_dir():
        raise ProductionMigrationExecutionError("execution_root_unavailable")
    return target.resolve(strict=True)


def capture_git_execution_state() -> GitExecutionState:
    def run(*args: str) -> str:
        try:
            completed = subprocess.run(
                ("git", "-C", str(PROJECT_ROOT), *args),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
            )
        except (OSError, UnicodeError, subprocess.CalledProcessError):
            raise ProductionMigrationExecutionError("git_state_unavailable") from None
        return completed.stdout.strip()

    commit = run("rev-parse", "HEAD")
    tracked = run("status", "--porcelain", "--untracked-files=no")
    staged = run("diff", "--cached", "--name-only")
    return GitExecutionState(
        commit=commit,
        tracked_worktree_clean=not bool(tracked),
        staging_empty=not bool(staged),
    )


def validate_git_execution_state(
    state: GitExecutionState,
    *,
    expected_commit: str,
) -> None:
    if state.commit != _commit(expected_commit, "expected_code_commit_invalid"):
        raise ProductionMigrationExecutionError("git_commit_mismatch")
    if not state.tracked_worktree_clean:
        raise ProductionMigrationExecutionError("tracked_worktree_dirty")
    if not state.staging_empty:
        raise ProductionMigrationExecutionError("staging_not_empty")


def _inspect_production_immutable(path: Path):
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
        connection.execute("PRAGMA query_only=ON")
        with closing(connection):
            return inspect_schema_connection(connection)
    except sqlite3.Error:
        raise ProductionMigrationExecutionError("production_schema_inspection_failed") from None


def _wal_gate(path: Path) -> tuple[int, str]:
    wal = Path(str(path) + "-wal")
    try:
        if not wal.exists():
            return 0, _EMPTY_SHA256
        return wal.stat().st_size, _hash_file(wal)
    except OSError:
        raise ProductionMigrationExecutionError("production_wal_unavailable") from None


def validate_production_marker_gate(
    snapshot,
    *,
    expected_target_schema: str,
) -> ProductionMigrationOutcome | None:
    """Classify the immutable marker state without opening a writer."""
    _sha(expected_target_schema, "expected_target_schema_invalid")
    if snapshot.migration_version == HISTORICAL_MIGRATION_VERSION:
        raise ProductionMigrationExecutionError(
            "v8302_marker_present",
            recovery_required=True,
        )
    if (
        snapshot.migration_version is not None
        and snapshot.migration_version > MIGRATION_VERSION
    ):
        raise ProductionMigrationExecutionError(
            "higher_schema_version_present",
            recovery_required=True,
        )
    if snapshot.migration_version == MIGRATION_VERSION:
        if (
            snapshot.migration_digest == MIGRATION_DIGEST
            and snapshot.marker_resulting_fingerprint == snapshot.schema_fingerprint
            and snapshot.schema_fingerprint == expected_target_schema
        ):
            return ProductionMigrationOutcome(
                status="already_applied",
                migration_version=MIGRATION_VERSION,
                migration_digest=MIGRATION_DIGEST,
                recovery_required=False,
            )
        raise ProductionMigrationExecutionError(
            "v8301_marker_inconsistent",
            recovery_required=True,
        )
    if snapshot.marker_state != "legacy":
        raise ProductionMigrationExecutionError(
            "migration_marker_incomplete",
            recovery_required=True,
        )
    return None


def _load_document(path: Path, reason_prefix: str) -> dict[str, Any]:
    try:
        return load_json_document(path, reason_prefix=reason_prefix)
    except ProductionMigrationAuthorizationError as exc:
        raise ProductionMigrationExecutionError(exc.reason_code) from None


def _validate_bound_document_paths(
    request: ProductionMigrationRequest,
    authorization: ProductionMigrationAuthorization,
) -> tuple[Path, Path, Path]:
    if not _same_path(request.approval_package, APPROVAL_PACKAGE_PATH):
        raise ProductionMigrationExecutionError("approval_package_path_mismatch")
    _reject_reparse_components(request.approval_package)
    if not request.approval_package.is_file():
        raise ProductionMigrationExecutionError("approval_package_unavailable")
    root = _validate_controlled_execution_root(
        request.execution_root,
        execution_id=authorization.one_time_execution_id,
    )
    expected_authorization = root / "human_authorization_record.json"
    if not _same_path(request.authorization_record, expected_authorization):
        raise ProductionMigrationExecutionError("authorization_record_path_mismatch")
    _reject_reparse_components(request.authorization_record)
    if not request.authorization_record.is_file():
        raise ProductionMigrationExecutionError("authorization_record_unavailable")
    return (
        request.approval_package.resolve(strict=True),
        request.authorization_record.resolve(strict=True),
        root,
    )


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ProductionMigrationExecutionError("execution_state_invalid")
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    raise ProductionMigrationExecutionError("execution_state_invalid")
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ProductionMigrationExecutionError("execution_state_invalid")
                records.append(value)
    except ProductionMigrationExecutionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProductionMigrationExecutionError("execution_state_invalid") from None
    return records


def _validate_not_replayed(execution_id: str, root: Path) -> None:
    if (root / "recovery_required.json").exists():
        raise ProductionMigrationExecutionError(
            "recovery_required_exists",
            recovery_required=True,
        )
    records = _read_json_lines(EXECUTION_STATE_LEDGER)
    previous = _EMPTY_SHA256
    expected_fields = {
        "state_schema_version",
        "one_time_execution_id",
        "authorization_digest",
        "approval_package_digest",
        "state",
        "recorded_at",
        "previous_state_digest",
        "state_digest",
    }
    for record in records:
        if (
            set(record) != expected_fields
            or record.get("state_schema_version") != EXECUTION_STATE_SCHEMA_VERSION
            or record.get("state") not in {"consumed", "recovery_required"}
            or record.get("previous_state_digest") != previous
            or record.get("state_digest")
            != canonical_document_digest(record, digest_field="state_digest")
        ):
            raise ProductionMigrationExecutionError("execution_state_invalid")
        _identity(
            record.get("one_time_execution_id"),
            "execution_state_invalid",
        )
        _sha(record.get("authorization_digest"), "execution_state_invalid")
        _sha(record.get("approval_package_digest"), "execution_state_invalid")
        _timestamp(record.get("recorded_at"), "execution_state_invalid")
        previous = record["state_digest"]
        if record.get("one_time_execution_id") != execution_id:
            continue
        state = record.get("state")
        if state == "consumed":
            raise ProductionMigrationExecutionError("one_time_execution_replayed")
        if state == "recovery_required":
            raise ProductionMigrationExecutionError(
                "recovery_required_exists",
                recovery_required=True,
            )
        raise ProductionMigrationExecutionError("execution_state_invalid")


def _validate_record_time(
    record: Mapping[str, Any],
    *,
    now: datetime,
    prefix: str,
) -> None:
    observed = _timestamp(record.get("observed_at"), f"{prefix}_observed_at_invalid")
    expires = _timestamp(record.get("expires_at"), f"{prefix}_expires_at_invalid")
    if observed > now or expires <= observed or expires <= now:
        raise ProductionMigrationExecutionError(f"{prefix}_expired")


def _validate_service_quiescence_record(
    root: Path,
    *,
    authorization: ProductionMigrationAuthorization,
    now: datetime,
) -> None:
    record = _load_document(
        root / "services_quiesced.json",
        "service_quiescence_record",
    )
    expected_fields = {
        "record_schema_version",
        "one_time_execution_id",
        "code_commit",
        "source_database_sha256",
        "services_quiesced",
        "active_writer_count",
        "observed_at",
        "expires_at",
        "record_digest",
    }
    if set(record) != expected_fields:
        raise ProductionMigrationExecutionError(
            "service_quiescence_fields_mismatch"
        )
    if (
        record.get("record_schema_version") != SERVICE_RECORD_SCHEMA_VERSION
        or record.get("one_time_execution_id")
        != authorization.one_time_execution_id
        or record.get("code_commit") != authorization.code_commit
        or record.get("source_database_sha256")
        != authorization.source_database_sha256
        or record.get("services_quiesced") is not True
        or record.get("active_writer_count") != 0
    ):
        raise ProductionMigrationExecutionError("services_not_quiesced")
    if (
        record.get("record_digest")
        != canonical_document_digest(record, digest_field="record_digest")
    ):
        raise ProductionMigrationExecutionError(
            "service_quiescence_digest_mismatch"
        )
    _validate_record_time(record, now=now, prefix="service_quiescence_record")


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_backup_verification_record(
    root: Path,
    *,
    authorization: ProductionMigrationAuthorization,
    package: Mapping[str, Any],
    now: datetime,
) -> None:
    record = _load_document(
        root / "backup_verification.json",
        "backup_verification_record",
    )
    expected_fields = {
        "record_schema_version",
        "one_time_execution_id",
        "source_database_sha256",
        "source_schema_fingerprint",
        "backup_manifest_relative_path",
        "backup_manifest_digest",
        "backup_set_digest",
        "database_backups",
        "restore_rehearsal_passed",
        "observed_at",
        "expires_at",
        "record_digest",
    }
    if set(record) != expected_fields:
        raise ProductionMigrationExecutionError(
            "backup_verification_fields_mismatch"
        )
    approved = package["backup"]
    if (
        record.get("record_schema_version") != BACKUP_RECORD_SCHEMA_VERSION
        or record.get("one_time_execution_id")
        != authorization.one_time_execution_id
        or record.get("source_database_sha256")
        != authorization.source_database_sha256
        or record.get("source_schema_fingerprint")
        != authorization.source_schema_fingerprint
        or record.get("backup_manifest_digest")
        != approved["backup_manifest_digest"]
        or record.get("backup_set_digest") != approved["backup_set_digest"]
        or record.get("restore_rehearsal_passed") is not True
        or record.get("record_digest")
        != canonical_document_digest(record, digest_field="record_digest")
    ):
        raise ProductionMigrationExecutionError("backup_verification_mismatch")
    _validate_record_time(record, now=now, prefix="backup_verification_record")

    backup_root = (PROJECT_ROOT / "backups" / "migrations").resolve(strict=True)
    manifest_relative = record.get("backup_manifest_relative_path")
    if not isinstance(manifest_relative, str):
        raise ProductionMigrationExecutionError("backup_manifest_path_invalid")
    manifest = (PROJECT_ROOT / manifest_relative).absolute()
    _reject_reparse_components(manifest)
    try:
        manifest_resolved = manifest.resolve(strict=True)
    except OSError:
        raise ProductionMigrationExecutionError("backup_manifest_unavailable") from None
    if (
        not manifest_resolved.is_file()
        or not _path_within(manifest_resolved, backup_root)
        or manifest_resolved.parent.name != approved["backup_directory"]
    ):
        raise ProductionMigrationExecutionError("backup_manifest_path_invalid")
    manifest_document = _load_document(manifest_resolved, "backup_manifest")
    manifest_digest = manifest_document.get("manifest_digest")
    if (
        manifest_digest != approved["backup_manifest_digest"]
        or manifest_digest
        != canonical_document_digest(
            manifest_document,
            digest_field="manifest_digest",
        )
    ):
        raise ProductionMigrationExecutionError("backup_manifest_digest_mismatch")

    entries = record.get("database_backups")
    if not isinstance(entries, Mapping) or set(entries) != set(_BACKUP_LABELS):
        raise ProductionMigrationExecutionError("backup_set_fields_mismatch")
    normalized: dict[str, dict[str, Any]] = {}
    for label in _BACKUP_LABELS:
        entry = entries[label]
        if not isinstance(entry, Mapping) or set(entry) != {
            "relative_path",
            "sha256",
            "size",
        }:
            raise ProductionMigrationExecutionError("backup_entry_invalid")
        relative = entry.get("relative_path")
        if not isinstance(relative, str):
            raise ProductionMigrationExecutionError("backup_entry_path_invalid")
        path = (PROJECT_ROOT / relative).absolute()
        _reject_reparse_components(path)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise ProductionMigrationExecutionError("backup_entry_unavailable") from None
        if (
            not resolved.is_file()
            or resolved.parent != manifest_resolved.parent
            or _hash_file(resolved) != entry.get("sha256")
            or resolved.stat().st_size != entry.get("size")
        ):
            raise ProductionMigrationExecutionError("backup_entry_mismatch")
        normalized[label] = {
            "relative_path": relative,
            "sha256": entry["sha256"],
            "size": entry["size"],
        }
    calculated_set_digest = hashlib.sha256(
        canonical_json_bytes(normalized)
    ).hexdigest()
    if calculated_set_digest != approved["backup_set_digest"]:
        raise ProductionMigrationExecutionError("backup_set_digest_mismatch")


def _journal_path(root: Path) -> Path:
    return root / "execution_journal.jsonl"


def append_execution_journal_event(
    root: Path,
    *,
    execution_id: str,
    event: str,
    reason_code: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if event not in REQUIRED_JOURNAL_EVENTS:
        raise ProductionMigrationExecutionError("journal_event_invalid")
    _identity(execution_id, "one_time_execution_id_invalid")
    target = _journal_path(root)
    records = _read_json_lines(target)
    previous = _EMPTY_SHA256
    expected_fields = {
        "journal_schema_version",
        "one_time_execution_id",
        "event",
        "occurred_at",
        "reason_code",
        "previous_event_digest",
        "event_digest",
    }
    for existing in records:
        if (
            set(existing) != expected_fields
            or existing.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION
            or existing.get("one_time_execution_id") != execution_id
            or existing.get("event") not in REQUIRED_JOURNAL_EVENTS
            or existing.get("previous_event_digest") != previous
            or existing.get("event_digest")
            != canonical_document_digest(existing, digest_field="event_digest")
        ):
            raise ProductionMigrationExecutionError("journal_chain_invalid")
        _timestamp(existing.get("occurred_at"), "journal_chain_invalid")
        reason = existing.get("reason_code")
        if reason is not None and sanitize_reason_code(reason) != reason:
            raise ProductionMigrationExecutionError("journal_chain_invalid")
        previous = existing["event_digest"]
    sanitized_reason = (
        sanitize_reason_code(reason_code) if reason_code is not None else None
    )
    record = {
        "journal_schema_version": JOURNAL_SCHEMA_VERSION,
        "one_time_execution_id": execution_id,
        "event": event,
        "occurred_at": _utc_text(now),
        "reason_code": sanitized_reason,
        "previous_event_digest": previous,
    }
    record["event_digest"] = hashlib.sha256(
        canonical_json_bytes(record)
    ).hexdigest()
    line = canonical_json_bytes(record) + b"\n"
    try:
        descriptor = os.open(
            target,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise ProductionMigrationExecutionError("journal_append_failed") from None
    return record


def _append_execution_state(
    *,
    authorization: ProductionMigrationAuthorization,
    state: str,
    now: datetime | None = None,
) -> None:
    if state not in {"consumed", "recovery_required"}:
        raise ProductionMigrationExecutionError("execution_state_invalid")
    APPROVAL_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    existing = _read_json_lines(EXECUTION_STATE_LEDGER)
    previous = existing[-1].get("state_digest") if existing else _EMPTY_SHA256
    if not isinstance(previous, str) or _SHA256_RE.fullmatch(previous) is None:
        raise ProductionMigrationExecutionError("execution_state_invalid")
    record = {
        "state_schema_version": EXECUTION_STATE_SCHEMA_VERSION,
        "one_time_execution_id": authorization.one_time_execution_id,
        "authorization_digest": authorization.authorization_digest,
        "approval_package_digest": authorization.approval_package_digest,
        "state": state,
        "recorded_at": _utc_text(now),
        "previous_state_digest": previous,
    }
    record["state_digest"] = hashlib.sha256(
        canonical_json_bytes(record)
    ).hexdigest()
    try:
        descriptor = os.open(
            EXECUTION_STATE_LEDGER,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, canonical_json_bytes(record) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise ProductionMigrationExecutionError("execution_state_append_failed") from None


def _mark_recovery_required(
    root: Path,
    *,
    authorization: ProductionMigrationAuthorization,
    reason_code: str,
    now: datetime | None = None,
) -> None:
    marker = root / "recovery_required.json"
    payload = {
        "state_schema_version": EXECUTION_STATE_SCHEMA_VERSION,
        "one_time_execution_id": authorization.one_time_execution_id,
        "authorization_digest": authorization.authorization_digest,
        "state": "recovery_required",
        "reason_code": sanitize_reason_code(reason_code),
        "recorded_at": _utc_text(now),
    }
    payload["state_digest"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    try:
        descriptor = os.open(
            marker,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, canonical_json_bytes(payload) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        pass
    except OSError:
        raise ProductionMigrationExecutionError("recovery_marker_write_failed") from None
    _append_execution_state(
        authorization=authorization,
        state="recovery_required",
        now=now,
    )


def preflight_authorized_production_migration(
    request: ProductionMigrationRequest,
    *,
    now: datetime | None = None,
) -> ProductionMigrationPreflight | ProductionMigrationOutcome:
    validate_target_version(request.target_version)
    for value, reason in (
        (request.expected_db_sha256, "expected_database_digest_invalid"),
        (request.expected_current_schema, "expected_current_schema_invalid"),
        (request.expected_target_schema, "expected_target_schema_invalid"),
        (request.expected_migration_digest, "expected_migration_digest_invalid"),
        (request.expected_plan_digest, "expected_plan_digest_invalid"),
    ):
        _sha(value, reason)
    if request.expected_migration_digest != MIGRATION_DIGEST:
        raise ProductionMigrationExecutionError("migration_digest_mismatch")

    production = validate_production_main_database_path(request.production_db)
    root = _validate_controlled_execution_root(request.execution_root)
    expected_authorization_path = root / "human_authorization_record.json"
    if not _same_path(request.authorization_record, expected_authorization_path):
        raise ProductionMigrationExecutionError("authorization_record_path_mismatch")
    _reject_reparse_components(request.authorization_record)
    authorization_document = _load_document(
        request.authorization_record,
        "authorization_record",
    )
    try:
        authorization = ProductionMigrationAuthorization.from_mapping(
            authorization_document
        )
    except ProductionMigrationAuthorizationError as exc:
        raise ProductionMigrationExecutionError(exc.reason_code) from None
    approval_path, authorization_path, root = _validate_bound_document_paths(
        request,
        authorization,
    )
    package = _load_document(approval_path, "approval_package")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        package = validate_approval_package(package, now=current)
    except ProductionMigrationAuthorizationError as exc:
        raise ProductionMigrationExecutionError(exc.reason_code) from None

    git_state = capture_git_execution_state()
    validate_git_execution_state(
        git_state,
        expected_commit=authorization.code_commit,
    )
    try:
        validate_authorization_binding(
            authorization,
            package,
            expected_database_sha256=request.expected_db_sha256,
            expected_current_schema=request.expected_current_schema,
            expected_target_schema=request.expected_target_schema,
            expected_migration_digest=request.expected_migration_digest,
            expected_plan_digest=request.expected_plan_digest,
            current_code_commit=git_state.commit,
            now=current,
        )
    except ProductionMigrationAuthorizationError as exc:
        raise ProductionMigrationExecutionError(exc.reason_code) from None

    snapshot = _inspect_production_immutable(production)
    marker_outcome = validate_production_marker_gate(
        snapshot,
        expected_target_schema=request.expected_target_schema,
    )
    if marker_outcome is not None:
        return marker_outcome

    actual_database_sha = _hash_file(production)
    if actual_database_sha != request.expected_db_sha256:
        raise ProductionMigrationExecutionError("database_digest_drift")
    if snapshot.schema_fingerprint != request.expected_current_schema:
        raise ProductionMigrationExecutionError("source_schema_drift")
    plan = build_migration_plan(snapshot)
    actual_plan_digest = migration_plan_digest(plan)
    if plan.expected_target_schema_fingerprint != request.expected_target_schema:
        raise ProductionMigrationExecutionError("target_schema_drift")
    if actual_plan_digest != request.expected_plan_digest:
        raise ProductionMigrationExecutionError("migration_plan_digest_mismatch")

    if approval_path != request.approval_package.resolve(strict=True):
        raise ProductionMigrationExecutionError("approval_package_path_mismatch")
    if authorization_path != request.authorization_record.resolve(strict=True):
        raise ProductionMigrationExecutionError("authorization_record_path_mismatch")

    _validate_not_replayed(authorization.one_time_execution_id, root)
    wal_size, _wal_sha = _wal_gate(production)
    if wal_size != 0:
        raise ProductionMigrationExecutionError("production_wal_not_zero")
    if _lock_path(production).exists():
        raise ProductionMigrationExecutionError("writer_lease_conflict")
    _validate_service_quiescence_record(
        root,
        authorization=authorization,
        now=current,
    )
    _validate_backup_verification_record(
        root,
        authorization=authorization,
        package=package,
        now=current,
    )
    return ProductionMigrationPreflight(
        request=request,
        authorization=authorization,
        package=package,
        plan=plan,
        production_database=production,
        execution_root=root,
        git_state=git_state,
    )


def apply_authorized_production_migration(
    request: ProductionMigrationRequest,
    *,
    now: datetime | None = None,
) -> MigrationResult | ProductionMigrationOutcome:
    """Execute exact v8301 only after the complete authorization preflight."""
    preflight = preflight_authorized_production_migration(request, now=now)
    if isinstance(preflight, ProductionMigrationOutcome):
        return preflight
    authorization = preflight.authorization
    root = preflight.execution_root
    event = lambda name, reason=None: append_execution_journal_event(
        root,
        execution_id=authorization.one_time_execution_id,
        event=name,
        reason_code=reason,
        now=now,
    )
    for name in (
        "authorization_validated",
        "production_path_validated",
        "source_fingerprint_validated",
        "services_quiesced_validated",
        "backup_validated",
    ):
        event(name)
    database_committed_and_verified = False

    def core_event(name: str) -> None:
        try:
            event(name)
        except ProductionMigrationExecutionError as exc:
            raise OSError("journal_event_failed") from exc

    try:
        result = _apply_v8301_migration_to_validated_target(
            preflight.plan,
            preflight.production_database,
            execution_id=authorization.one_time_execution_id,
            event_hook=core_event,
        )
        database_committed_and_verified = True
        event("execution_completed")
        _append_execution_state(
            authorization=authorization,
            state="consumed",
            now=now,
        )
        return result
    except MigrationRehearsalError as exc:
        if exc.recovery_required:
            try:
                event("recovery_required", exc.reason_code)
                _mark_recovery_required(
                    root,
                    authorization=authorization,
                    reason_code=exc.reason_code,
                    now=now,
                )
            except ProductionMigrationExecutionError:
                pass
            raise ProductionMigrationExecutionError(
                exc.reason_code,
                recovery_required=True,
            ) from None
        try:
            event("execution_failed", exc.reason_code)
        except ProductionMigrationExecutionError:
            pass
        raise ProductionMigrationExecutionError(exc.reason_code) from None
    except ProductionMigrationExecutionError as exc:
        if exc.recovery_required or database_committed_and_verified:
            try:
                event("recovery_required", exc.reason_code)
                _mark_recovery_required(
                    root,
                    authorization=authorization,
                    reason_code=exc.reason_code,
                    now=now,
                )
            except ProductionMigrationExecutionError:
                pass
            raise ProductionMigrationExecutionError(
                exc.reason_code,
                recovery_required=True,
            ) from None
        try:
            event("execution_failed", exc.reason_code)
        except ProductionMigrationExecutionError:
            pass
        raise
    except OSError:
        try:
            event("recovery_required", "post_commit_state_uncertain")
            _mark_recovery_required(
                root,
                authorization=authorization,
                reason_code="post_commit_state_uncertain",
                now=now,
            )
        except ProductionMigrationExecutionError:
            pass
        raise ProductionMigrationExecutionError(
            "post_commit_state_uncertain",
            recovery_required=True,
        ) from None


def assert_execution_protocol_static_guards() -> None:
    """Review-time guard against accidental scope broadening."""
    if (
        MIGRATION_VERSION != 8301
        or HISTORICAL_MIGRATION_VERSION != 8302
        or APPROVAL_PACKAGE_SCHEMA_VERSION
        != "v8.3-v8301-production-approval-package-2"
    ):
        raise ProductionMigrationExecutionError("execution_protocol_guard_failed")
