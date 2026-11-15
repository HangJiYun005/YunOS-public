"""Approval-bound, test-only execution core for YunOS v8.3-E.

The module deliberately has no production writer.  Its only writable target is
an isolated SQLite database created by :func:`initialize_test_execution_target`
under the system temporary directory or the v8.3-E evidence directory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from memory.db_path_policy import is_reparse_point
from memory.ingestion_approval import (
    ApprovalArtifact,
    IngestionApprovalError,
    import_plan_digest,
    validate_approval_artifact,
)
from memory.ingestion_assets import AssetStagePlan, AssetStageResult
from memory.ingestion_core import (
    ImportBatch,
    SourceManifest,
    canonical_json_bytes,
    make_plan_digest,
    normalized_json_digest,
    sanitize_reason_code,
    utc_now,
)
from memory.ingestion_planning import ImportPlan, PlanAction
from memory.ingestion_verification import (
    COUNT_KEYS,
    TEST_EXECUTION_SCHEMA_VERSION,
    ExecutionVerificationResult,
    IngestionVerificationError,
    TestTargetSnapshot,
    VerificationExpectation,
    capture_test_target_snapshot,
    make_verification_expectation,
    verify_test_execution,
)


EXECUTION_OPERATION_SCHEMA_VERSION = "v8.3-e-execution-operation-1"
EXECUTION_BUNDLE_SCHEMA_VERSION = "v8.3-e-execution-bundle-1"
EXECUTION_JOURNAL_SCHEMA_VERSION = "v8.3-e-execution-journal-1"
WRITER_LEASE_SCHEMA_VERSION = "v8.3-e-writer-lease-1"
EXECUTION_RESULT_SCHEMA_VERSION = "v8.3-e-execution-result-1"
RECOVERY_REPORT_SCHEMA_VERSION = "v8.3-e-recovery-report-1"
ROLLBACK_REPORT_SCHEMA_VERSION = "v8.3-e-rollback-report-1"
SANITIZED_REPORT_SCHEMA_VERSION = "v8.3-e-sanitized-report-1"

NODE_OUTPUT_NAME = "v8.3-e-approval-atomic-commit"
ZERO_DIGEST = "0" * 64
STREAM_CHUNK_SIZE = 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_MIME_RE = re.compile(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*\Z")

DATABASE_OPERATION_TYPES = frozenset({
    "create_conversation",
    "create_message",
    "create_attachment_metadata",
    "register_asset_entity",
    "bind_source_record",
    "update_safe_metadata",
    "mark_metadata_only",
    "verify_existing_record",
})
HISTORICAL_OPERATION_TYPES = frozenset({
    "verify_native_source_binding",
    "create_legacy_source_binding",
    "create_historical_attachment_binding",
    "verify_media_identity",
    "record_legacy_unknown",
    "record_backfill_conflict",
    "flag_probable_binding",
    "skip_existing_binding",
})
AUDIT_ONLY_OPERATION_TYPES = frozenset({
    "skip_exact_duplicate",
    "flag_possible_duplicate",
    "flag_conflict",
    "mark_deletion_candidate",
})
KNOWN_OPERATION_TYPES = (
    DATABASE_OPERATION_TYPES
    | AUDIT_ONLY_OPERATION_TYPES
    | HISTORICAL_OPERATION_TYPES
)

JOURNAL_EVENT_TYPES = frozenset({
    "preflight_started",
    "preflight_passed",
    "lease_acquired",
    "backup_created",
    "asset_promotion_started",
    "asset_promoted",
    "database_transaction_started",
    "operation_applied",
    "database_committed",
    "verification_started",
    "verification_passed",
    "execution_completed",
    "execution_failed",
    "recovery_required",
    "rollback_started",
    "asset_rolled_back",
    "database_restored",
    "rollback_completed",
})

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_NODE_OUTPUT_ROOT = (_PROJECT_ROOT / "output" / NODE_OUTPUT_NAME).resolve()
_HISTORICAL_NODE_OUTPUT_ROOT = (
    _PROJECT_ROOT
    / "output"
    / "v8.3-g-historical-backfill-compatibility"
).resolve()
_SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
_LEASE_ROOT = _SYSTEM_TEMP_ROOT / "ayunos-v83-e-writer-leases"
_PRODUCTION_DATABASE = (_PROJECT_ROOT / "data" / "ayunos_chatgpt.db").resolve()
_PRODUCTION_IMPORTS = (_PROJECT_ROOT / "imports").resolve()
_PRODUCTION_DATA = (_PROJECT_ROOT / "data").resolve()


class IngestionExecutionError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        reason = sanitize_reason_code(reason_code)
        super().__init__(reason)
        self.reason_code = reason


class ExecutionInterrupted(BaseException):
    """Fault-injection signal that intentionally leaves recovery evidence."""

    def __init__(
        self,
        checkpoint: str,
        lease_token: "WriterLeaseToken",
    ) -> None:
        super().__init__(checkpoint)
        self.checkpoint = checkpoint
        self.lease_token = lease_token


def _sha(value: str, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IngestionExecutionError(reason)
    return value


def _identity(value: str, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise IngestionExecutionError(reason)
    return value


def _token(value: str, reason: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise IngestionExecutionError(reason)
    return value


def _timestamp(value: str, reason: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IngestionExecutionError(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise IngestionExecutionError(reason) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise IngestionExecutionError(reason)
    return parsed


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(STREAM_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_raw_path(value: str) -> None:
    lowered = value.lower()
    if (
        not value
        or "\x00" in value
        or "%" in value
        or value.startswith(("\\\\", "//"))
        or "\\\\?\\" in lowered
        or "\\\\.\\" in lowered
        or any(part in {"", ".", ".."} for part in re.split(r"[\\/]", value)[1:])
    ):
        raise IngestionExecutionError("execution_path_unsafe")
    for part in re.split(r"[\\/]", value):
        if part.endswith((" ", ".")):
            raise IngestionExecutionError("execution_path_unsafe")
    drive, tail = os.path.splitdrive(value)
    if ":" in tail:
        raise IngestionExecutionError("execution_path_unsafe")
    if drive and not re.fullmatch(r"[A-Za-z]:", drive):
        raise IngestionExecutionError("execution_path_unsafe")


def _reject_reparse_components(path: Path) -> None:
    current = path
    existing: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            existing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for component in existing:
        if is_reparse_point(component):
            raise IngestionExecutionError("execution_reparse_forbidden")


def validate_test_execution_root(
    value: str | os.PathLike[str],
) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise IngestionExecutionError("execution_path_unsafe")
    _reject_raw_path(raw)
    unresolved = Path(raw)
    if not unresolved.is_absolute():
        raise IngestionExecutionError("execution_root_not_absolute")
    _reject_reparse_components(unresolved)
    resolved = unresolved.resolve(strict=False)
    if not (
        _is_relative_to(resolved, _SYSTEM_TEMP_ROOT)
        or _is_relative_to(resolved, _NODE_OUTPUT_ROOT)
        or _is_relative_to(resolved, _HISTORICAL_NODE_OUTPUT_ROOT)
    ):
        raise IngestionExecutionError("execution_root_outside_test_boundary")
    if (
        resolved == _PROJECT_ROOT
        or _is_relative_to(resolved, _PRODUCTION_DATA)
        or _is_relative_to(resolved, _PRODUCTION_IMPORTS)
    ):
        raise IngestionExecutionError("production_path_forbidden")
    return resolved


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _freeze_json(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise IngestionExecutionError("operation_payload_not_json")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class TestExecutionTarget:
    target_id: str
    target_identity_digest: str
    execution_root_fingerprint: str
    root: Path = field(repr=False, compare=False)
    database_path: Path = field(repr=False, compare=False)
    asset_root: Path = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _identity(self.target_id, "invalid_test_target_id")
        _sha(self.target_identity_digest, "invalid_test_target_digest")
        _sha(
            self.execution_root_fingerprint,
            "invalid_execution_root_fingerprint",
        )
        root = validate_test_execution_root(self.root)
        database = Path(self.database_path).resolve(strict=False)
        assets = Path(self.asset_root).resolve(strict=False)
        if (
            database != root / "target.sqlite"
            or assets != root / "assets"
            or database == _PRODUCTION_DATABASE
        ):
            raise IngestionExecutionError("test_target_layout_mismatch")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "database_path", database)
        object.__setattr__(self, "asset_root", assets)


def initialize_test_execution_target(
    execution_root: str | os.PathLike[str],
) -> TestExecutionTarget:
    root = validate_test_execution_root(execution_root)
    if root.exists():
        if not root.is_dir() or is_reparse_point(root):
            raise IngestionExecutionError("execution_root_unsafe")
        if any(root.iterdir()):
            raise IngestionExecutionError("execution_root_not_empty")
    root.mkdir(parents=True, exist_ok=True)
    assets = root / "assets"
    assets.mkdir()
    database = root / "target.sqlite"
    target_id = "testtarget_" + uuid.uuid4().hex
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE execution_schema(
                schema_version TEXT NOT NULL,
                target_id TEXT NOT NULL
            );
            CREATE TABLE conversations(
                conversation_id TEXT PRIMARY KEY,
                source_identity TEXT NOT NULL UNIQUE,
                safe_metadata_json TEXT NOT NULL
            );
            CREATE TABLE messages(
                message_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL
                    REFERENCES conversations(conversation_id),
                source_identity TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                safe_metadata_json TEXT NOT NULL
            );
            CREATE TABLE asset_entities(
                entity_digest TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                mime TEXT NOT NULL
            );
            CREATE TABLE attachments(
                attachment_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES messages(message_id),
                source_identity TEXT NOT NULL UNIQUE,
                entity_digest TEXT REFERENCES asset_entities(entity_digest),
                state TEXT NOT NULL,
                safe_metadata_json TEXT NOT NULL
            );
            CREATE TABLE source_bindings(
                source_type TEXT NOT NULL,
                source_identity TEXT NOT NULL,
                record_kind TEXT NOT NULL,
                target_id TEXT NOT NULL,
                UNIQUE(source_type, source_identity)
            );
            CREATE TABLE execution_applied_operations(
                operation_id TEXT PRIMARY KEY,
                operation_type TEXT NOT NULL,
                payload_digest TEXT NOT NULL
            );
            CREATE TABLE execution_conflict_resolutions(
                conflict_id TEXT PRIMARY KEY,
                selected_action TEXT NOT NULL,
                evidence_digest TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO execution_schema(schema_version,target_id) VALUES(?,?)",
            (TEST_EXECUTION_SCHEMA_VERSION, target_id),
        )
        connection.commit()
    finally:
        connection.close()
    target_identity_digest = normalized_json_digest({"test_target_id": target_id})
    execution_root_fingerprint = normalized_json_digest({
        "target_identity_digest": target_identity_digest,
        "schema_version": TEST_EXECUTION_SCHEMA_VERSION,
        "node": NODE_OUTPUT_NAME,
    })
    return TestExecutionTarget(
        target_id=target_id,
        target_identity_digest=target_identity_digest,
        execution_root_fingerprint=execution_root_fingerprint,
        root=root,
        database_path=database,
        asset_root=assets,
    )


def load_test_execution_target(
    execution_root: str | os.PathLike[str],
) -> TestExecutionTarget:
    root = validate_test_execution_root(execution_root)
    database = root / "target.sqlite"
    assets = root / "assets"
    snapshot = capture_test_target_snapshot(database, assets)
    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        row = connection.execute(
            "SELECT target_id FROM execution_schema"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise IngestionExecutionError("test_target_schema_mismatch")
    target_id = str(row[0])
    return TestExecutionTarget(
        target_id=target_id,
        target_identity_digest=snapshot.target_identity_digest,
        execution_root_fingerprint=normalized_json_digest({
            "target_identity_digest": snapshot.target_identity_digest,
            "schema_version": TEST_EXECUTION_SCHEMA_VERSION,
            "node": NODE_OUTPUT_NAME,
        }),
        root=root,
        database_path=database,
        asset_root=assets,
    )


@dataclass(frozen=True)
class ExecutionOperation:
    operation_id: str
    plan_action_id: str
    operation_type: str
    dependency_operation_ids: tuple[str, ...]
    payload_digest: str
    payload: Mapping[str, Any] = field(repr=False, compare=False)
    operation_schema_version: str = EXECUTION_OPERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.operation_id, "invalid_execution_operation_id")
        _identity(self.plan_action_id, "invalid_plan_action_id")
        if self.operation_type not in KNOWN_OPERATION_TYPES:
            raise IngestionExecutionError("unknown_operation_type")
        dependencies = tuple(sorted(set(self.dependency_operation_ids)))
        for item in dependencies:
            _identity(item, "invalid_dependency_operation_id")
        if self.operation_id in dependencies:
            raise IngestionExecutionError("operation_dependency_cycle")
        object.__setattr__(self, "dependency_operation_ids", dependencies)
        frozen = _freeze_json(self.payload)
        object.__setattr__(self, "payload", frozen)
        _sha(self.payload_digest, "invalid_operation_payload_digest")
        if self.payload_digest != normalized_json_digest(_thaw_json(frozen)):
            raise IngestionExecutionError("operation_payload_digest_mismatch")
        if self.operation_schema_version != EXECUTION_OPERATION_SCHEMA_VERSION:
            raise IngestionExecutionError("operation_schema_version_mismatch")
        expected = "execop_" + normalized_json_digest(self.identity_payload())
        if self.operation_id != expected:
            raise IngestionExecutionError("operation_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "plan_action_id": self.plan_action_id,
            "operation_type": self.operation_type,
            "dependency_operation_ids": list(self.dependency_operation_ids),
            "payload_digest": self.payload_digest,
            "operation_schema_version": self.operation_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, **self.identity_payload()}


def make_execution_operation(
    *,
    plan_action_id: str,
    operation_type: str,
    payload: Mapping[str, Any],
    dependency_operation_ids: Sequence[str] = (),
) -> ExecutionOperation:
    frozen = _freeze_json(payload)
    payload_digest = normalized_json_digest(_thaw_json(frozen))
    identity_payload = {
        "plan_action_id": plan_action_id,
        "operation_type": operation_type,
        "dependency_operation_ids": sorted(set(dependency_operation_ids)),
        "payload_digest": payload_digest,
        "operation_schema_version": EXECUTION_OPERATION_SCHEMA_VERSION,
    }
    digest = normalized_json_digest(identity_payload)
    return ExecutionOperation(
        operation_id="execop_" + digest,
        payload=frozen,
        **identity_payload,
    )


@dataclass(frozen=True)
class StagedEntitySource:
    operation_id: str
    entity_digest: str
    actual_size: int
    detected_mime: str
    staged_path: Path = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _identity(self.operation_id, "invalid_stage_operation_id")
        _sha(self.entity_digest, "invalid_entity_digest")
        if (
            isinstance(self.actual_size, bool)
            or not isinstance(self.actual_size, int)
            or self.actual_size < 0
        ):
            raise IngestionExecutionError("invalid_entity_size")
        if not isinstance(self.detected_mime, str) or _MIME_RE.fullmatch(
            self.detected_mime
        ) is None:
            raise IngestionExecutionError("invalid_entity_mime")
        path = Path(self.staged_path)
        if (
            not path.is_absolute()
            or not path.is_file()
            or is_reparse_point(path)
        ):
            raise IngestionExecutionError("staged_entity_unsafe")
        object.__setattr__(self, "staged_path", path.resolve())


@dataclass(frozen=True)
class ExecutionBundle:
    execution_id: str
    batch_id: str
    manifest_id: str
    plan_digest: str
    stage_plan_digest: str
    approval_digest: str
    target_snapshot_fingerprint: str
    approved_operations: tuple[str, ...]
    rejected_operations: tuple[str, ...]
    database_operations: tuple[ExecutionOperation, ...]
    approved_assets: tuple[Mapping[str, Any], ...]
    verification_expectations: VerificationExpectation
    database_backup_fingerprint: str
    execution_root_fingerprint: str
    conflict_decisions: tuple[Mapping[str, str], ...]
    execution_schema_version: str = EXECUTION_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.execution_id, "invalid_execution_id")
        _identity(self.batch_id, "invalid_batch_id")
        _identity(self.manifest_id, "invalid_manifest_id")
        for value, reason in (
            (self.plan_digest, "invalid_plan_digest"),
            (self.stage_plan_digest, "invalid_stage_plan_digest"),
            (self.approval_digest, "invalid_approval_digest"),
            (
                self.target_snapshot_fingerprint,
                "invalid_target_snapshot_fingerprint",
            ),
            (
                self.database_backup_fingerprint,
                "invalid_database_backup_fingerprint",
            ),
            (
                self.execution_root_fingerprint,
                "invalid_execution_root_fingerprint",
            ),
        ):
            _sha(value, reason)
        approved = tuple(sorted(set(self.approved_operations)))
        rejected = tuple(sorted(set(self.rejected_operations)))
        for operation_id in (*approved, *rejected):
            _identity(operation_id, "invalid_operation_id")
        if set(approved).intersection(rejected):
            raise IngestionExecutionError("bundle_operation_overlap")
        object.__setattr__(self, "approved_operations", approved)
        object.__setattr__(self, "rejected_operations", rejected)
        operations = tuple(
            sorted(self.database_operations, key=lambda item: item.operation_id)
        )
        if len({item.operation_id for item in operations}) != len(operations):
            raise IngestionExecutionError("duplicate_execution_operation")
        if not {item.operation_id for item in operations}.issubset(
            set(approved) | set(rejected)
        ):
            raise IngestionExecutionError("bundle_contains_unplanned_operation")
        object.__setattr__(self, "database_operations", operations)
        assets: list[Mapping[str, Any]] = []
        for asset in self.approved_assets:
            if set(asset) != {
                "operation_id",
                "entity_digest",
                "actual_size",
                "detected_mime",
            }:
                raise IngestionExecutionError("invalid_bundle_asset")
            _identity(asset["operation_id"], "invalid_stage_operation_id")
            _sha(asset["entity_digest"], "invalid_entity_digest")
            if (
                isinstance(asset["actual_size"], bool)
                or not isinstance(asset["actual_size"], int)
                or asset["actual_size"] < 0
                or not isinstance(asset["detected_mime"], str)
                or _MIME_RE.fullmatch(asset["detected_mime"]) is None
            ):
                raise IngestionExecutionError("invalid_bundle_asset")
            if asset["operation_id"] not in approved:
                raise IngestionExecutionError("unapproved_bundle_asset")
            assets.append(MappingProxyType(dict(sorted(asset.items()))))
        if len({item["operation_id"] for item in assets}) != len(assets):
            raise IngestionExecutionError("duplicate_bundle_asset")
        object.__setattr__(
            self,
            "approved_assets",
            tuple(sorted(assets, key=lambda item: item["operation_id"])),
        )
        if not isinstance(
            self.verification_expectations, VerificationExpectation
        ):
            raise IngestionExecutionError("invalid_verification_expectation")
        decisions: list[Mapping[str, str]] = []
        for decision in self.conflict_decisions:
            if set(decision) != {
                "conflict_id",
                "selected_action",
                "evidence_digest",
            }:
                raise IngestionExecutionError("invalid_bundle_conflict_decision")
            _identity(decision["conflict_id"], "invalid_conflict_id")
            _token(decision["selected_action"], "invalid_selected_action")
            _sha(decision["evidence_digest"], "invalid_evidence_digest")
            decisions.append(MappingProxyType(dict(sorted(decision.items()))))
        object.__setattr__(
            self,
            "conflict_decisions",
            tuple(sorted(decisions, key=lambda item: item["conflict_id"])),
        )
        if self.execution_schema_version != EXECUTION_BUNDLE_SCHEMA_VERSION:
            raise IngestionExecutionError("execution_schema_version_mismatch")
        expected = "execution_" + normalized_json_digest(self.identity_payload())
        if self.execution_id != expected:
            raise IngestionExecutionError("execution_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "manifest_id": self.manifest_id,
            "plan_digest": self.plan_digest,
            "stage_plan_digest": self.stage_plan_digest,
            "approval_digest": self.approval_digest,
            "target_snapshot_fingerprint": self.target_snapshot_fingerprint,
            "approved_operations": list(self.approved_operations),
            "rejected_operations": list(self.rejected_operations),
            "database_operations": [
                item.to_dict() for item in self.database_operations
            ],
            "approved_assets": [
                dict(item) for item in self.approved_assets
            ],
            "verification_expectations": self.verification_expectations.to_dict(),
            "database_backup_fingerprint": self.database_backup_fingerprint,
            "execution_root_fingerprint": self.execution_root_fingerprint,
            "conflict_decisions": [
                dict(item) for item in self.conflict_decisions
            ],
            "execution_schema_version": self.execution_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"execution_id": self.execution_id, **self.identity_payload()}


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    check_results: Mapping[str, bool]
    reason_codes: tuple[str, ...]
    bundle: ExecutionBundle | None

    def __post_init__(self) -> None:
        checks = dict(sorted(self.check_results.items()))
        if any(
            _TOKEN_RE.fullmatch(key) is None or not isinstance(value, bool)
            for key, value in checks.items()
        ):
            raise IngestionExecutionError("invalid_preflight_result")
        reasons = tuple(sorted(set(
            sanitize_reason_code(item) for item in self.reason_codes
        )))
        object.__setattr__(self, "check_results", MappingProxyType(checks))
        object.__setattr__(self, "reason_codes", reasons)
        if self.passed != all(checks.values()):
            raise IngestionExecutionError("preflight_result_mismatch")
        if self.passed != (self.bundle is not None):
            raise IngestionExecutionError("preflight_bundle_mismatch")


_ACTION_OPERATION_COMPATIBILITY: Mapping[str, frozenset[str]] = {
    "insert": frozenset({
        "create_conversation",
        "create_message",
        "create_attachment_metadata",
        "register_asset_entity",
        "bind_source_record",
        "update_safe_metadata",
        "mark_metadata_only",
    }),
    "append": frozenset({
        "create_conversation",
        "create_message",
        "create_attachment_metadata",
        "bind_source_record",
        "update_safe_metadata",
        "mark_metadata_only",
    }),
    "no_change": frozenset({
        "skip_exact_duplicate",
        "verify_existing_record",
    }),
    "conflict": frozenset({
        "flag_possible_duplicate",
        "flag_conflict",
        "update_safe_metadata",
        "verify_existing_record",
    }),
    "tombstone_candidate": frozenset({"mark_deletion_candidate"}),
    "share_entity": frozenset({
        "register_asset_entity",
        "bind_source_record",
        "verify_existing_record",
    }),
    "metadata_only": frozenset({
        "update_safe_metadata",
        "mark_metadata_only",
    }),
    "reject": frozenset({
        "skip_exact_duplicate",
        "flag_conflict",
    }),
    "link_candidate": frozenset({
        "bind_source_record",
        "verify_existing_record",
    }),
}


def _validate_operation_graph(
    operations: Sequence[ExecutionOperation],
    *,
    stage_operations: Sequence[str],
    approved: set[str],
) -> tuple[ExecutionOperation, ...]:
    by_id = {item.operation_id: item for item in operations}
    if len(by_id) != len(operations):
        raise IngestionExecutionError("duplicate_execution_operation")
    known = set(by_id) | set(stage_operations)
    for operation in operations:
        dependencies = set(operation.dependency_operation_ids)
        if not dependencies.issubset(known):
            raise IngestionExecutionError("operation_dependency_missing")
        if operation.operation_id in approved and not dependencies.issubset(approved):
            raise IngestionExecutionError("approved_dependency_not_approved")
    visiting: set[str] = set()
    visited: set[str] = set(stage_operations)
    ordered: list[ExecutionOperation] = []

    def visit(operation_id: str) -> None:
        if operation_id in visited:
            return
        if operation_id in visiting:
            raise IngestionExecutionError("operation_dependency_cycle")
        operation = by_id[operation_id]
        visiting.add(operation_id)
        for dependency in operation.dependency_operation_ids:
            if dependency in by_id:
                visit(dependency)
        visiting.remove(operation_id)
        visited.add(operation_id)
        ordered.append(operation)

    for operation_id in sorted(by_id):
        visit(operation_id)
    return tuple(ordered)


def _expected_deltas(
    operations: Sequence[ExecutionOperation],
    approved: set[str],
) -> dict[str, int]:
    result = {key: 0 for key in COUNT_KEYS}
    operation_deltas = {
        "create_conversation": "conversations",
        "create_message": "messages",
        "create_attachment_metadata": "attachments",
        "register_asset_entity": "entities",
        "bind_source_record": "source_bindings",
    }
    for operation in operations:
        key = operation_deltas.get(operation.operation_type)
        if key is not None and operation.operation_id in approved:
            result[key] += 1
    return result


def _simulate_expected_logical_rows(
    *,
    before_snapshot: TestTargetSnapshot,
    operations: Sequence[ExecutionOperation],
    stage_operation_ids: Sequence[str],
    approved: set[str],
    conflict_decisions: Sequence[Mapping[str, str]],
) -> tuple[str, dict[str, set[str]]]:
    rows: dict[str, list[tuple[Any, ...]]] = {
        table: [tuple(row) for row in table_rows]
        for table, table_rows in before_snapshot.logical_rows.items()
    }
    additions: dict[str, set[str]] = {
        table: set()
        for table in before_snapshot.identity_sets
    }

    def metadata(payload: Mapping[str, Any]) -> str:
        return _payload_metadata(payload)

    def replace_metadata(
        table: str,
        target_id: str,
        metadata_json: str,
    ) -> None:
        updated = False
        for index, row in enumerate(rows[table]):
            if str(row[0]) == target_id:
                mutable = list(row)
                mutable[-1] = metadata_json
                rows[table][index] = tuple(mutable)
                updated = True
                break
        if not updated:
            raise IngestionExecutionError("existing_record_missing")

    ordered = _validate_operation_graph(
        operations,
        stage_operations=stage_operation_ids,
        approved=approved,
    )
    for operation in ordered:
        if operation.operation_id not in approved:
            continue
        payload = operation.payload
        operation_type = operation.operation_type
        if operation_type == "create_conversation":
            _ensure_payload_keys(
                payload,
                {"conversation_id", "source_identity"},
                {"safe_metadata"},
            )
            row = (
                _payload_text(payload, "conversation_id", identity=True),
                _payload_text(payload, "source_identity", identity=True),
                metadata(payload),
            )
            rows["conversations"].append(row)
            additions["conversations"].add(row[0])
        elif operation_type == "create_message":
            _ensure_payload_keys(
                payload,
                {
                    "message_id",
                    "conversation_id",
                    "source_identity",
                    "content_hash",
                },
                {"safe_metadata"},
            )
            row = (
                _payload_text(payload, "message_id", identity=True),
                _payload_text(payload, "conversation_id", identity=True),
                _payload_text(payload, "source_identity", identity=True),
                _payload_text(payload, "content_hash", sha=True),
                metadata(payload),
            )
            rows["messages"].append(row)
            additions["messages"].add(row[0])
        elif operation_type == "create_attachment_metadata":
            _ensure_payload_keys(
                payload,
                {
                    "attachment_id",
                    "message_id",
                    "source_identity",
                    "state",
                },
                {"entity_digest", "safe_metadata"},
            )
            entity_digest = payload.get("entity_digest")
            if entity_digest is not None:
                _sha(entity_digest, "operation_payload_invalid")
            row = (
                _payload_text(payload, "attachment_id", identity=True),
                _payload_text(payload, "message_id", identity=True),
                _payload_text(payload, "source_identity", identity=True),
                entity_digest,
                _payload_text(payload, "state", token=True),
                metadata(payload),
            )
            rows["attachments"].append(row)
            additions["attachments"].add(row[0])
        elif operation_type == "register_asset_entity":
            _ensure_payload_keys(
                payload,
                {"entity_digest", "size_bytes", "mime"},
            )
            size = payload.get("size_bytes")
            mime = payload.get("mime")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(mime, str)
                or _MIME_RE.fullmatch(mime) is None
            ):
                raise IngestionExecutionError("operation_payload_invalid")
            row = (
                _payload_text(payload, "entity_digest", sha=True),
                size,
                mime,
            )
            rows["asset_entities"].append(row)
            additions["asset_entities"].add(row[0])
        elif operation_type == "bind_source_record":
            _ensure_payload_keys(
                payload,
                {
                    "source_type",
                    "source_identity",
                    "record_kind",
                    "target_id",
                },
            )
            row = (
                _payload_text(payload, "source_type", token=True),
                _payload_text(payload, "source_identity", identity=True),
                _payload_text(payload, "record_kind", token=True),
                _payload_text(payload, "target_id", identity=True),
            )
            rows["source_bindings"].append(row)
            additions["source_bindings"].add(row[1])
        elif operation_type in {
            "update_safe_metadata",
            "mark_metadata_only",
        }:
            _ensure_payload_keys(
                payload,
                {"record_kind", "target_id"},
                {"safe_metadata"},
            )
            record_kind = _payload_text(
                payload,
                "record_kind",
                token=True,
            )
            table = {
                "conversation": "conversations",
                "message": "messages",
                "attachment": "attachments",
            }.get(record_kind)
            if table is None:
                raise IngestionExecutionError("operation_payload_invalid")
            replace_metadata(
                table,
                _payload_text(payload, "target_id", identity=True),
                metadata(payload),
            )
        elif operation_type == "verify_existing_record":
            _ensure_payload_keys(payload, {"record_kind", "target_id"})
        elif operation_type in AUDIT_ONLY_OPERATION_TYPES:
            _ensure_payload_keys(payload, set())
        elif operation_type in HISTORICAL_OPERATION_TYPES:
            # Historical v8302 rows are simulated and verified by the G
            # delegate.  They must never be projected into the v8301 test
            # execution tables here.
            pass
        if operation_type in DATABASE_OPERATION_TYPES:
            rows["execution_applied_operations"].append((
                operation.operation_id,
                operation.operation_type,
                operation.payload_digest,
            ))
    for decision in conflict_decisions:
        rows["execution_conflict_resolutions"].append((
            decision["conflict_id"],
            decision["selected_action"],
            decision["evidence_digest"],
        ))
    normalized_rows = {
        table: sorted(table_rows, key=lambda row: tuple(
            "" if value is None else str(value) for value in row
        ))
        for table, table_rows in rows.items()
    }
    return normalized_json_digest({
        "schema_version": TEST_EXECUTION_SCHEMA_VERSION,
        "rows": normalized_rows,
    }), additions


def _scope_digest(target: TestExecutionTarget, kind: str, value: str) -> str:
    if kind == "target":
        private_value = "|".join((
            str(target.database_path).casefold(),
            str(target.asset_root).casefold(),
        ))
    elif kind == "batch":
        private_value = value
    else:
        raise IngestionExecutionError("invalid_lease_scope")
    return normalized_json_digest({
        "lease_kind": kind,
        "private_scope": private_value,
        "schema_version": WRITER_LEASE_SCHEMA_VERSION,
    })


def _lease_paths(
    target: TestExecutionTarget,
    batch_id: str,
) -> tuple[Path, Path]:
    target_scope = _scope_digest(target, "target", "")
    batch_scope = _scope_digest(target, "batch", batch_id)
    return (
        _LEASE_ROOT / f"target-{target_scope}.lock",
        _LEASE_ROOT / f"batch-{batch_scope}.lock",
    )


def _recovery_marker(target: TestExecutionTarget, batch_id: str) -> Path:
    return target.root / (
        "recovery-required-"
        + normalized_json_digest({"batch_id": batch_id})
        + ".json"
    )


def _stage_sources_valid(
    stage_plan: AssetStagePlan,
    stage_result: AssetStageResult,
    approval: ApprovalArtifact,
    staged_entities: Sequence[StagedEntitySource],
) -> bool:
    if (
        stage_result.batch_id != stage_plan.batch_id
        or stage_result.stage_plan_digest != stage_plan.stage_plan_digest
        or stage_result.status not in {"staged", "staged_with_warnings"}
        or stage_result.entities_quarantined != 0
        or stage_result.asset_stage_fingerprint
        != stage_plan.asset_stage_fingerprint
        or stage_result.entities_staged + stage_result.entities_reused
        != len(stage_plan.operations)
        or (
            bool(stage_plan.operations)
            and stage_result.ledger_digest is None
        )
    ):
        return False
    planned = {item.operation_id: item for item in stage_plan.operations}
    sources = {item.operation_id: item for item in staged_entities}
    if len(sources) != len(staged_entities):
        return False
    required = set(planned).intersection(approval.approved_operation_ids)
    if set(sources) != required:
        return False
    for operation_id, source in sources.items():
        operation = planned[operation_id]
        if (
            source.entity_digest != operation.entity_digest
            or source.actual_size != operation.actual_size
            or source.detected_mime != operation.detected_mime
        ):
            return False
        try:
            validate_test_execution_root(source.staged_path.parent)
            digest, size = _hash_file(source.staged_path)
        except (OSError, IngestionExecutionError):
            return False
        if digest != source.entity_digest or size != source.actual_size:
            return False
    return True


def _build_execution_bundle(
    *,
    batch: ImportBatch,
    import_plan: ImportPlan,
    stage_plan: AssetStagePlan,
    approval: ApprovalArtifact,
    operations: Sequence[ExecutionOperation],
    before_snapshot: TestTargetSnapshot,
    target: TestExecutionTarget,
) -> ExecutionBundle:
    approved = set(approval.approved_operation_ids)
    decision_payloads = [
        {
            "conflict_id": item.conflict_id,
            "selected_action": item.selected_action,
            "evidence_digest": item.evidence_digest,
        }
        for item in approval.conflict_decisions
    ]
    expected_logical_digest, identity_additions = (
        _simulate_expected_logical_rows(
            before_snapshot=before_snapshot,
            operations=operations,
            stage_operation_ids=tuple(
                item.operation_id for item in stage_plan.operations
            ),
            approved=approved,
            conflict_decisions=decision_payloads,
        )
    )
    expectation = make_verification_expectation(
        expected_deltas=_expected_deltas(operations, approved),
        approved_operation_ids=approval.approved_operation_ids,
        rejected_operation_ids=approval.rejected_operation_ids,
        expected_conflict_resolutions=len(approval.conflict_decisions),
        before_snapshot=before_snapshot,
        expected_after_logical_digest=expected_logical_digest,
        approved_asset_digests=tuple(
            item.entity_digest
            for item in stage_plan.operations
            if item.operation_id in approved
        ),
        expected_identity_additions=identity_additions,
    )
    payload = {
        "batch_id": batch.batch_id,
        "manifest_id": import_plan.manifest_id,
        "plan_digest": import_plan_digest(import_plan),
        "stage_plan_digest": stage_plan.stage_plan_digest,
        "approval_digest": approval.approval_digest,
        "target_snapshot_fingerprint": before_snapshot.target_fingerprint,
        "approved_operations": list(approval.approved_operation_ids),
        "rejected_operations": list(approval.rejected_operation_ids),
        "database_operations": [
            item.to_dict()
            for item in sorted(operations, key=lambda item: item.operation_id)
        ],
        "approved_assets": [
            {
                "operation_id": item.operation_id,
                "entity_digest": item.entity_digest,
                "actual_size": item.actual_size,
                "detected_mime": item.detected_mime,
            }
            for item in stage_plan.operations
            if item.operation_id in approved
        ],
        "verification_expectations": expectation.to_dict(),
        "database_backup_fingerprint": normalized_json_digest({
            "target_fingerprint": before_snapshot.target_fingerprint,
            "database_content_sha256": (
                before_snapshot.database_content_sha256
            ),
        }),
        "execution_root_fingerprint": target.execution_root_fingerprint,
        "conflict_decisions": decision_payloads,
        "execution_schema_version": EXECUTION_BUNDLE_SCHEMA_VERSION,
    }
    digest = normalized_json_digest(payload)
    return ExecutionBundle(
        execution_id="execution_" + digest,
        database_operations=tuple(operations),
        approved_assets=tuple(payload["approved_assets"]),
        verification_expectations=expectation,
        conflict_decisions=tuple(payload["conflict_decisions"]),
        **{
            key: value
            for key, value in payload.items()
            if key not in {
                "database_operations",
                "approved_assets",
                "verification_expectations",
                "conflict_decisions",
            }
        },
    )


def preflight_execution(
    *,
    batch: ImportBatch,
    manifest: SourceManifest,
    import_plan: ImportPlan,
    stage_plan: AssetStagePlan,
    stage_result: AssetStageResult,
    approval: ApprovalArtifact,
    operations: Sequence[ExecutionOperation],
    target: TestExecutionTarget,
    before_snapshot: TestTargetSnapshot,
    staged_entities: Sequence[StagedEntitySource],
    now: str,
    minimum_free_bytes: int = 0,
) -> PreflightResult:
    """Run a fail-closed, non-writing preflight and create an immutable bundle."""
    checks: dict[str, bool] = {}
    reasons: set[str] = set()

    def record(name: str, reason: str, predicate: Callable[[], bool]) -> None:
        try:
            value = bool(predicate())
        except (
            IngestionExecutionError,
            IngestionApprovalError,
            OSError,
            sqlite3.Error,
        ):
            value = False
        checks[name] = value
        if not value:
            reasons.add(reason)

    operation_ids = {item.operation_id for item in operations}
    stage_operation_ids = {
        item.operation_id for item in stage_plan.operations
    }
    candidate_ids = operation_ids | stage_operation_ids
    approved = set(approval.approved_operation_ids)
    action_by_id: dict[str, PlanAction] = {
        item.action_id: item for item in import_plan.actions
    }
    target_lease, batch_lease = _lease_paths(target, batch.batch_id)
    planned_stage = {item.operation_id: item for item in stage_plan.operations}

    record(
        "batch_state_allows_commit",
        "batch_not_approved",
        lambda: (
            batch.current_state == "approved"
            and batch.recovery_status == "not_required"
        ),
    )
    record(
        "manifest_binding_valid",
        "manifest_binding_mismatch",
        lambda: (
            manifest.manifest_id
            == import_plan.manifest_id
            == stage_plan.manifest_id
            == batch.manifest_id
            and manifest.archive_sha256 == import_plan.archive_sha256
            and manifest.adapter_version == import_plan.adapter_version
        ),
    )
    record(
        "plan_binding_valid",
        "plan_binding_mismatch",
        lambda: (
            batch.batch_id == import_plan.batch_id == stage_plan.batch_id
            and batch.plan_digest in {
                import_plan_digest(import_plan),
                make_plan_digest(import_plan.identity_payload()),
            }
            and stage_plan.import_plan_id == import_plan.plan_id
            and stage_plan.import_plan_digest == import_plan_digest(import_plan)
        ),
    )
    record(
        "stage_result_complete",
        "asset_stage_result_incomplete",
        lambda: _stage_sources_valid(
            stage_plan,
            stage_result,
            approval,
            staged_entities,
        ),
    )
    record(
        "target_snapshot_stable",
        "target_snapshot_changed",
        lambda: (
            before_snapshot.schema_version == TEST_EXECUTION_SCHEMA_VERSION
            and before_snapshot.target_identity_digest
            == target.target_identity_digest
            and capture_test_target_snapshot(
                target.database_path,
                target.asset_root,
            ).target_fingerprint
            == before_snapshot.target_fingerprint
        ),
    )
    record(
        "approval_valid",
        "approval_invalid_or_expired",
        lambda: (
            validate_approval_artifact(
                approval,
                import_plan=import_plan,
                stage_plan=stage_plan,
                target_snapshot_fingerprint=before_snapshot.target_fingerprint,
                candidate_operation_ids=tuple(candidate_ids),
                now=now,
            )
            is None
            and batch.approval_digest == approval.approval_digest
        ),
    )
    record(
        "operation_plan_whitelist_valid",
        "operation_not_in_plan",
        lambda: (
            len(operation_ids) == len(operations)
            and all(
                item.plan_action_id in action_by_id
                and item.operation_type
                in _ACTION_OPERATION_COMPATIBILITY[
                    action_by_id[item.plan_action_id].action_type
                ]
                for item in operations
            )
        ),
    )
    record(
        "operation_payloads_valid",
        "operation_payload_invalid",
        lambda: bool(
            _simulate_expected_logical_rows(
                before_snapshot=before_snapshot,
                operations=operations,
                stage_operation_ids=tuple(stage_operation_ids),
                approved=approved,
                conflict_decisions=tuple(
                    {
                        "conflict_id": item.conflict_id,
                        "selected_action": item.selected_action,
                        "evidence_digest": item.evidence_digest,
                    }
                    for item in approval.conflict_decisions
                ),
            )[0]
        ),
    )
    record(
        "deletion_candidates_rejected",
        "deletion_candidate_must_be_rejected",
        lambda: all(
            item.operation_type != "mark_deletion_candidate"
            or item.operation_id not in approved
            for item in operations
        ),
    )
    record(
        "dependencies_closed_and_acyclic",
        "operation_dependency_invalid",
        lambda: bool(
            _validate_operation_graph(
                operations,
                stage_operations=tuple(stage_operation_ids),
                approved=approved,
            )
            or not operations
        )
        and all(
            set(item.dependency_operation_ids).issubset(approved)
            for item in stage_plan.operations
            if item.operation_id in approved
        ),
    )
    record(
        "approval_partition_exact",
        "approval_operation_set_stale",
        lambda: (
            approved.union(approval.rejected_operation_ids) == candidate_ids
            and not approved.intersection(approval.rejected_operation_ids)
        ),
    )
    record(
        "execution_root_safe",
        "execution_root_unsafe",
        lambda: (
            validate_test_execution_root(target.root) == target.root
            and target.database_path == target.root / "target.sqlite"
            and target.asset_root == target.root / "assets"
            and target.database_path != _PRODUCTION_DATABASE
        ),
    )
    required_bytes = (
        before_snapshot.database_size
        + sum(
            planned_stage[item].actual_size
            for item in approved.intersection(planned_stage)
        )
        + max(0, minimum_free_bytes)
    )
    record(
        "disk_space_sufficient",
        "insufficient_disk_space",
        lambda: shutil.disk_usage(target.root).free >= required_bytes,
    )
    record(
        "writer_lease_available",
        "writer_lease_conflict",
        lambda: not target_lease.exists() and not batch_lease.exists(),
    )
    record(
        "no_recovery_required",
        "recovery_required_exists",
        lambda: not _recovery_marker(target, batch.batch_id).exists(),
    )
    passed = all(checks.values())
    bundle = None
    if passed:
        bundle = _build_execution_bundle(
            batch=batch,
            import_plan=import_plan,
            stage_plan=stage_plan,
            approval=approval,
            operations=operations,
            before_snapshot=before_snapshot,
            target=target,
        )
    return PreflightResult(
        passed=passed,
        check_results=checks,
        reason_codes=tuple(reasons),
        bundle=bundle,
    )


@dataclass(frozen=True)
class WriterLeaseToken:
    batch_id: str
    target_scope_digest: str
    batch_scope_digest: str
    secret: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _identity(self.batch_id, "invalid_batch_id")
        _sha(self.target_scope_digest, "invalid_target_lease_scope")
        _sha(self.batch_scope_digest, "invalid_batch_lease_scope")
        if not isinstance(self.secret, str) or len(self.secret) < 32:
            raise IngestionExecutionError("invalid_lease_secret")


def _write_exclusive_json(path: Path, payload: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        data = canonical_json_bytes(payload) + b"\n"
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def acquire_writer_lease(
    target: TestExecutionTarget,
    batch_id: str,
) -> WriterLeaseToken:
    _identity(batch_id, "invalid_batch_id")
    _LEASE_ROOT.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(_LEASE_ROOT):
        raise IngestionExecutionError("lease_root_reparse_forbidden")
    target_path, batch_path = _lease_paths(target, batch_id)
    target_scope = target_path.stem.split("-", 1)[1]
    batch_scope = batch_path.stem.split("-", 1)[1]
    secret = secrets.token_hex(32)
    token_digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
    created: list[Path] = []
    try:
        for kind, path, scope in (
            ("target", target_path, target_scope),
            ("batch", batch_path, batch_scope),
        ):
            _write_exclusive_json(path, {
                "lease_schema_version": WRITER_LEASE_SCHEMA_VERSION,
                "lease_kind": kind,
                "scope_digest": scope,
                "batch_id": batch_id,
                "token_digest": token_digest,
                "acquired_at": utc_now(),
            })
            created.append(path)
    except FileExistsError:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise IngestionExecutionError("writer_lease_conflict") from None
    except OSError:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise IngestionExecutionError("writer_lease_acquire_failed") from None
    return WriterLeaseToken(
        batch_id=batch_id,
        target_scope_digest=target_scope,
        batch_scope_digest=batch_scope,
        secret=secret,
    )


def _validate_lease_token(
    target: TestExecutionTarget,
    lease_token: WriterLeaseToken,
) -> tuple[Path, Path]:
    target_path, batch_path = _lease_paths(target, lease_token.batch_id)
    if (
        target_path.stem.split("-", 1)[1]
        != lease_token.target_scope_digest
        or batch_path.stem.split("-", 1)[1]
        != lease_token.batch_scope_digest
    ):
        raise IngestionExecutionError("lease_scope_mismatch")
    expected_token = hashlib.sha256(
        lease_token.secret.encode("ascii")
    ).hexdigest()
    for path, kind in ((target_path, "target"), (batch_path, "batch")):
        if not path.is_file() or is_reparse_point(path):
            raise IngestionExecutionError("lease_missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise IngestionExecutionError("lease_corrupt") from None
        if (
            value.get("lease_schema_version") != WRITER_LEASE_SCHEMA_VERSION
            or value.get("lease_kind") != kind
            or value.get("batch_id") != lease_token.batch_id
            or value.get("token_digest") != expected_token
        ):
            raise IngestionExecutionError("lease_token_mismatch")
    return target_path, batch_path


def release_writer_lease(
    target: TestExecutionTarget,
    lease_token: WriterLeaseToken,
) -> None:
    paths = _validate_lease_token(target, lease_token)
    for path in reversed(paths):
        path.unlink()


_JOURNAL_DETAIL_KEYS = frozenset({
    "reason_code",
    "operation_id",
    "entity_digest",
    "created",
    "backup_digest",
    "verification_digest",
    "database_after_fingerprint",
    "asset_after_fingerprint",
    "owned_asset_count",
    "operation_count",
    "conflict_count",
    "status",
})


def _safe_journal_details(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value).issubset(
        _JOURNAL_DETAIL_KEYS
    ):
        raise IngestionExecutionError("journal_details_unsafe")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {
            "operation_id",
            "entity_digest",
            "backup_digest",
            "verification_digest",
            "database_after_fingerprint",
            "asset_after_fingerprint",
        }:
            if key in {"operation_id"}:
                _identity(item, "journal_details_unsafe")
            else:
                _sha(item, "journal_details_unsafe")
        elif key in {"reason_code", "status"}:
            item = sanitize_reason_code(item)
        elif key == "created":
            if not isinstance(item, bool):
                raise IngestionExecutionError("journal_details_unsafe")
        elif (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
        ):
            raise IngestionExecutionError("journal_details_unsafe")
        result[key] = item
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class ExecutionJournalEvent:
    execution_id: str
    sequence: int
    event_type: str
    occurred_at: str
    previous_digest: str
    details: Mapping[str, Any]
    event_digest: str
    journal_schema_version: str = EXECUTION_JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.execution_id, "invalid_execution_id")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 1
        ):
            raise IngestionExecutionError("invalid_journal_sequence")
        if self.event_type not in JOURNAL_EVENT_TYPES:
            raise IngestionExecutionError("invalid_journal_event")
        _timestamp(self.occurred_at, "invalid_journal_timestamp")
        _sha(self.previous_digest, "invalid_previous_digest")
        object.__setattr__(
            self,
            "details",
            MappingProxyType(_safe_journal_details(self.details)),
        )
        _sha(self.event_digest, "invalid_event_digest")
        if self.journal_schema_version != EXECUTION_JOURNAL_SCHEMA_VERSION:
            raise IngestionExecutionError("journal_schema_version_mismatch")
        if self.event_digest != normalized_json_digest(self.digest_payload()):
            raise IngestionExecutionError("journal_digest_mismatch")

    def digest_payload(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "previous_digest": self.previous_digest,
            "details": dict(self.details),
            "journal_schema_version": self.journal_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.digest_payload(), "event_digest": self.event_digest}


def read_execution_journal(
    journal_path: str | os.PathLike[str],
    *,
    execution_id: str | None = None,
) -> tuple[ExecutionJournalEvent, ...]:
    path = Path(journal_path)
    if not path.exists():
        return ()
    if not path.is_file() or is_reparse_point(path):
        raise IngestionExecutionError("execution_journal_unsafe")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise IngestionExecutionError("execution_journal_read_failed") from None
    events: list[ExecutionJournalEvent] = []
    previous = ZERO_DIGEST
    for sequence, line in enumerate(lines, 1):
        try:
            raw = json.loads(line)
            if set(raw) != {
                "execution_id",
                "sequence",
                "event_type",
                "occurred_at",
                "previous_digest",
                "details",
                "journal_schema_version",
                "event_digest",
            }:
                raise ValueError
            event = ExecutionJournalEvent(**raw)
        except (
            ValueError,
            TypeError,
            json.JSONDecodeError,
            IngestionExecutionError,
        ):
            raise IngestionExecutionError("execution_journal_corrupt") from None
        if (
            event.sequence != sequence
            or event.previous_digest != previous
            or (execution_id is not None and event.execution_id != execution_id)
        ):
            raise IngestionExecutionError(
                "execution_journal_chain_mismatch"
            )
        previous = event.event_digest
        events.append(event)
    return tuple(events)


def append_execution_journal_event(
    journal_path: str | os.PathLike[str],
    *,
    execution_id: str,
    event_type: str,
    details: Mapping[str, Any] | None = None,
    timestamp: str | None = None,
) -> ExecutionJournalEvent:
    path = Path(journal_path)
    if path.exists() and is_reparse_point(path):
        raise IngestionExecutionError("execution_journal_unsafe")
    events = read_execution_journal(path, execution_id=execution_id)
    occurred_at = timestamp or utc_now()
    payload = {
        "execution_id": execution_id,
        "sequence": len(events) + 1,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "previous_digest": (
            events[-1].event_digest if events else ZERO_DIGEST
        ),
        "details": _safe_journal_details(details or {}),
        "journal_schema_version": EXECUTION_JOURNAL_SCHEMA_VERSION,
    }
    event = ExecutionJournalEvent(
        event_digest=normalized_json_digest(payload),
        **payload,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "ab", closefd=False) as handle:
            handle.write(canonical_json_bytes(event.to_dict()) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return event


def _payload_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    identity: bool = False,
    token: bool = False,
    sha: bool = False,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise IngestionExecutionError("operation_payload_invalid")
    if identity:
        _identity(value, "operation_payload_invalid")
    if token:
        _token(value, "operation_payload_invalid")
    if sha:
        _sha(value, "operation_payload_invalid")
    return value


def _payload_metadata(payload: Mapping[str, Any]) -> str:
    value = payload.get("safe_metadata", {})
    if not isinstance(value, Mapping):
        raise IngestionExecutionError("operation_payload_invalid")
    data = canonical_json_bytes(_thaw_json(_freeze_json(value)))
    if len(data) > 1024 * 1024:
        raise IngestionExecutionError("safe_metadata_too_large")
    return data.decode("utf-8")


def _ensure_payload_keys(
    payload: Mapping[str, Any],
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    keys = set(payload)
    if not required_set.issubset(keys) or not keys.issubset(
        required_set | set(optional)
    ):
        raise IngestionExecutionError("operation_payload_invalid")


def _create_conversation(
    connection: sqlite3.Connection,
    operation: ExecutionOperation,
) -> None:
    payload = operation.payload
    _ensure_payload_keys(
        payload,
        {"conversation_id", "source_identity"},
        {"safe_metadata"},
    )
    connection.execute(
        """INSERT INTO conversations(
             conversation_id,source_identity,safe_metadata_json
           ) VALUES(?,?,?)""",
        (
            _payload_text(payload, "conversation_id", identity=True),
            _payload_text(payload, "source_identity", identity=True),
            _payload_metadata(payload),
        ),
    )


def _create_message(
    connection: sqlite3.Connection,
    operation: ExecutionOperation,
) -> None:
    payload = operation.payload
    _ensure_payload_keys(
        payload,
        {
            "message_id",
            "conversation_id",
            "source_identity",
            "content_hash",
        },
        {"safe_metadata"},
    )
    connection.execute(
        """INSERT INTO messages(
             message_id,conversation_id,source_identity,content_hash,
             safe_metadata_json
           ) VALUES(?,?,?,?,?)""",
        (
            _payload_text(payload, "message_id", identity=True),
            _payload_text(payload, "conversation_id", identity=True),
            _payload_text(payload, "source_identity", identity=True),
            _payload_text(payload, "content_hash", sha=True),
            _payload_metadata(payload),
        ),
    )


def _create_attachment_metadata(
    connection: sqlite3.Connection,
    operation: ExecutionOperation,
) -> None:
    payload = operation.payload
    _ensure_payload_keys(
        payload,
        {"attachment_id", "message_id", "source_identity", "state"},
        {"entity_digest", "safe_metadata"},
    )
    entity_digest = payload.get("entity_digest")
    if entity_digest is not None:
        _sha(entity_digest, "operation_payload_invalid")
    connection.execute(
        """INSERT INTO attachments(
             attachment_id,message_id,source_identity,entity_digest,state,
             safe_metadata_json
           ) VALUES(?,?,?,?,?,?)""",
        (
            _payload_text(payload, "attachment_id", identity=True),
            _payload_text(payload, "message_id", identity=True),
            _payload_text(payload, "source_identity", identity=True),
            entity_digest,
            _payload_text(payload, "state", token=True),
            _payload_metadata(payload),
        ),
    )


def _register_asset_entity(
    connection: sqlite3.Connection,
    operation: ExecutionOperation,
) -> None:
    payload = operation.payload
    _ensure_payload_keys(payload, {"entity_digest", "size_bytes", "mime"})
    size = payload.get("size_bytes")
    mime = payload.get("mime")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or not isinstance(mime, str)
        or _MIME_RE.fullmatch(mime) is None
    ):
        raise IngestionExecutionError("operation_payload_invalid")
    connection.execute(
        """INSERT INTO asset_entities(entity_digest,size_bytes,mime)
           VALUES(?,?,?)""",
        (_payload_text(payload, "entity_digest", sha=True), size, mime),
    )


def _bind_source_record(
    connection: sqlite3.Connection,
    operation: ExecutionOperation,
) -> None:
    payload = operation.payload
    _ensure_payload_keys(
        payload,
        {"source_type", "source_identity", "record_kind", "target_id"},
    )
    connection.execute(
        """INSERT INTO source_bindings(
             source_type,source_identity,record_kind,target_id
           ) VALUES(?,?,?,?)""",
        (
            _payload_text(payload, "source_type", token=True),
            _payload_text(payload, "source_identity", identity=True),
            _payload_text(payload, "record_kind", token=True),
            _payload_text(payload, "target_id", identity=True),
        ),
    )


_RECORD_TABLES: Mapping[str, tuple[str, str]] = {
    "conversation": ("conversations", "conversation_id"),
    "message": ("messages", "message_id"),
    "attachment": ("attachments", "attachment_id"),
}


def _update_safe_metadata(
    connection: sqlite3.Connection,
    operation: ExecutionOperation,
) -> None:
    payload = operation.payload
    _ensure_payload_keys(
        payload,
        {"record_kind", "target_id"},
        {"safe_metadata"},
    )
    record_kind = _payload_text(payload, "record_kind", token=True)
    target = _RECORD_TABLES.get(record_kind)
    if target is None:
        raise IngestionExecutionError("operation_payload_invalid")
    table, identity_column = target
    cursor = connection.execute(
        f'UPDATE "{table}" SET safe_metadata_json=? '
        f'WHERE "{identity_column}"=?',
        (
            _payload_metadata(payload),
            _payload_text(payload, "target_id", identity=True),
        ),
    )
    if cursor.rowcount != 1:
        raise IngestionExecutionError("existing_record_missing")


def _verify_existing_record(
    connection: sqlite3.Connection,
    operation: ExecutionOperation,
) -> None:
    payload = operation.payload
    _ensure_payload_keys(payload, {"record_kind", "target_id"})
    record_kind = _payload_text(payload, "record_kind", token=True)
    if record_kind == "asset":
        table, identity_column = ("asset_entities", "entity_digest")
        target_id = _payload_text(payload, "target_id", sha=True)
    else:
        target = _RECORD_TABLES.get(record_kind)
        if target is None:
            raise IngestionExecutionError("operation_payload_invalid")
        table, identity_column = target
        target_id = _payload_text(payload, "target_id", identity=True)
    row = connection.execute(
        f'SELECT 1 FROM "{table}" WHERE "{identity_column}"=?',
        (target_id,),
    ).fetchone()
    if row is None:
        raise IngestionExecutionError("existing_record_missing")


_OPERATION_HANDLERS: Mapping[
    str,
    Callable[[sqlite3.Connection, ExecutionOperation], None],
] = {
    "create_conversation": _create_conversation,
    "create_message": _create_message,
    "create_attachment_metadata": _create_attachment_metadata,
    "register_asset_entity": _register_asset_entity,
    "bind_source_record": _bind_source_record,
    "update_safe_metadata": _update_safe_metadata,
    "mark_metadata_only": _update_safe_metadata,
    "verify_existing_record": _verify_existing_record,
}


def _transaction_invariants(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise IngestionExecutionError("transaction_foreign_key_violation")
    orphan_messages = connection.execute(
        """SELECT COUNT(*) FROM messages m
           LEFT JOIN conversations c
             ON c.conversation_id=m.conversation_id
           WHERE c.conversation_id IS NULL"""
    ).fetchone()[0]
    orphan_attachments = connection.execute(
        """SELECT COUNT(*) FROM attachments a
           LEFT JOIN messages m ON m.message_id=a.message_id
           WHERE m.message_id IS NULL"""
    ).fetchone()[0]
    if orphan_messages or orphan_attachments:
        raise IngestionExecutionError("transaction_orphan_record")


def _detect_known_mime(prefix: bytes) -> str | None:
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio/wav"
    if prefix.startswith(b"ID3"):
        return "audio/mpeg"
    return None


def _promote_entity(
    *,
    target: TestExecutionTarget,
    execution_id: str,
    source: StagedEntitySource,
) -> bool:
    final_root = target.asset_root / "final"
    pending_root = target.asset_root / "pending" / execution_id
    for directory in (target.asset_root, final_root, pending_root):
        if directory.exists() and is_reparse_point(directory):
            raise IngestionExecutionError("asset_target_reparse_forbidden")
        directory.mkdir(parents=True, exist_ok=True)
    destination = final_root / ("sha256_" + source.entity_digest)
    if destination.exists():
        if not destination.is_file() or is_reparse_point(destination):
            raise IngestionExecutionError("asset_target_unsafe")
        digest, size = _hash_file(destination)
        if digest != source.entity_digest or size != source.actual_size:
            raise IngestionExecutionError("existing_asset_conflict")
        return False
    pending = pending_root / ("sha256_" + source.entity_digest + ".part")
    if pending.exists():
        raise IngestionExecutionError("asset_pending_collision")
    digest = hashlib.sha256()
    size = 0
    prefix = b""
    descriptor = os.open(
        pending,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            with source.staged_path.open("rb") as input_handle:
                while chunk := input_handle.read(STREAM_CHUNK_SIZE):
                    if len(prefix) < 16:
                        prefix += chunk[: 16 - len(prefix)]
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    detected = _detect_known_mime(prefix)
    if (
        digest.hexdigest() != source.entity_digest
        or size != source.actual_size
        or (detected is not None and detected != source.detected_mime)
    ):
        pending.unlink(missing_ok=True)
        raise IngestionExecutionError("asset_promotion_verification_failed")
    os.replace(pending, destination)
    final_digest, final_size = _hash_file(destination)
    if final_digest != source.entity_digest or final_size != source.actual_size:
        destination.unlink(missing_ok=True)
        raise IngestionExecutionError("asset_promotion_verification_failed")
    return True


def _remove_owned_assets(
    target: TestExecutionTarget,
    owned_digests: Iterable[str],
) -> tuple[str, ...]:
    removed: list[str] = []
    final_root = target.asset_root / "final"
    for digest in sorted(set(owned_digests)):
        _sha(digest, "invalid_owned_asset_digest")
        path = final_root / ("sha256_" + digest)
        if not path.exists():
            continue
        if not path.is_file() or is_reparse_point(path):
            raise IngestionExecutionError("owned_asset_unsafe")
        actual, _ = _hash_file(path)
        if actual != digest:
            raise IngestionExecutionError("owned_asset_changed")
        path.unlink()
        removed.append(digest)
    return tuple(removed)


def _create_database_backup(
    target: TestExecutionTarget,
    backup_path: Path,
    before_snapshot: TestTargetSnapshot,
) -> str:
    if backup_path.exists():
        raise IngestionExecutionError("database_backup_exists")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target.database_path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise IngestionExecutionError("database_sidecar_not_quiescent")
    source_digest, source_size = _hash_file(target.database_path)
    if (
        source_digest != before_snapshot.database_content_sha256
        or source_size != before_snapshot.database_size
    ):
        raise IngestionExecutionError("database_changed_before_backup")
    source_handle = target.database_path.open("rb")
    descriptor = os.open(
        backup_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with source_handle, os.fdopen(
            descriptor,
            "wb",
            closefd=False,
        ) as destination:
            while chunk := source_handle.read(STREAM_CHUNK_SIZE):
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except OSError:
        backup_path.unlink(missing_ok=True)
        raise IngestionExecutionError("database_backup_failed") from None
    finally:
        os.close(descriptor)
    if _hash_file(target.database_path) != (source_digest, source_size):
        backup_path.unlink(missing_ok=True)
        raise IngestionExecutionError("database_changed_during_backup")
    if _hash_file(backup_path) != (source_digest, source_size):
        backup_path.unlink(missing_ok=True)
        raise IngestionExecutionError("database_backup_verification_failed")
    backup_snapshot = capture_test_target_snapshot(
        backup_path,
        target.asset_root,
    )
    if (
        backup_snapshot.logical_digest != before_snapshot.logical_digest
        or backup_snapshot.schema_digest != before_snapshot.schema_digest
        or dict(backup_snapshot.counts) != dict(before_snapshot.counts)
        or backup_snapshot.target_identity_digest
        != before_snapshot.target_identity_digest
    ):
        raise IngestionExecutionError("database_backup_verification_failed")
    return _hash_file(backup_path)[0]


def _restore_database_backup(
    target: TestExecutionTarget,
    backup_path: Path,
) -> None:
    if not backup_path.is_file() or is_reparse_point(backup_path):
        raise IngestionExecutionError("database_backup_missing")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target.database_path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise IngestionExecutionError("database_sidecar_not_quiescent")
    pending = target.database_path.with_name("target.restore.pending")
    if pending.exists():
        raise IngestionExecutionError("database_restore_pending_exists")
    descriptor = os.open(
        pending,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with backup_path.open("rb") as source, os.fdopen(
            descriptor,
            "wb",
            closefd=False,
        ) as destination:
            while chunk := source.read(STREAM_CHUNK_SIZE):
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    except OSError:
        pending.unlink(missing_ok=True)
        raise IngestionExecutionError("database_restore_failed") from None
    finally:
        os.close(descriptor)
    if _hash_file(pending) != _hash_file(backup_path):
        pending.unlink(missing_ok=True)
        raise IngestionExecutionError("database_restore_verification_failed")
    os.replace(pending, target.database_path)


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    status: str
    reason_codes: tuple[str, ...]
    applied_operation_ids: tuple[str, ...]
    created_asset_digests: tuple[str, ...]
    reused_asset_digests: tuple[str, ...]
    database_before_fingerprint: str
    database_after_fingerprint: str | None
    verification: ExecutionVerificationResult | None
    journal_digest: str
    lease_token: WriterLeaseToken | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    result_schema_version: str = EXECUTION_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.execution_id, "invalid_execution_id")
        if self.status not in {
            "completed",
            "failed",
            "recovery_required",
            "rolled_back",
        }:
            raise IngestionExecutionError("invalid_execution_status")
        object.__setattr__(
            self,
            "reason_codes",
            tuple(sorted(set(
                sanitize_reason_code(item) for item in self.reason_codes
            ))),
        )
        applied = tuple(sorted(set(self.applied_operation_ids)))
        for item in applied:
            _identity(item, "invalid_operation_id")
        object.__setattr__(self, "applied_operation_ids", applied)
        for field_name in ("created_asset_digests", "reused_asset_digests"):
            values = tuple(sorted(set(getattr(self, field_name))))
            for item in values:
                _sha(item, "invalid_asset_digest")
            object.__setattr__(self, field_name, values)
        _sha(
            self.database_before_fingerprint,
            "invalid_database_before_fingerprint",
        )
        if self.database_after_fingerprint is not None:
            _sha(
                self.database_after_fingerprint,
                "invalid_database_after_fingerprint",
            )
        _sha(self.journal_digest, "invalid_journal_digest")
        if self.result_schema_version != EXECUTION_RESULT_SCHEMA_VERSION:
            raise IngestionExecutionError("result_schema_version_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "applied_operation_ids": list(self.applied_operation_ids),
            "created_asset_digests": list(self.created_asset_digests),
            "reused_asset_digests": list(self.reused_asset_digests),
            "database_before_fingerprint": self.database_before_fingerprint,
            "database_after_fingerprint": self.database_after_fingerprint,
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "journal_digest": self.journal_digest,
            "result_schema_version": self.result_schema_version,
        }


def _execution_paths(
    target: TestExecutionTarget,
    execution_id: str,
) -> tuple[Path, Path, Path]:
    root = target.root / "executions" / execution_id
    return root, root / "journal.jsonl", root / "database-before.sqlite"


def _write_recovery_marker(
    target: TestExecutionTarget,
    bundle: ExecutionBundle,
    reason_code: str,
) -> None:
    marker = _recovery_marker(target, bundle.batch_id)
    payload = {
        "execution_id": bundle.execution_id,
        "batch_id": bundle.batch_id,
        "reason_code": sanitize_reason_code(reason_code),
        "schema_version": RECOVERY_REPORT_SCHEMA_VERSION,
    }
    if marker.exists():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise IngestionExecutionError("recovery_marker_corrupt") from None
        if (
            existing.get("execution_id") != bundle.execution_id
            or existing.get("batch_id") != bundle.batch_id
            or existing.get("schema_version")
            != RECOVERY_REPORT_SCHEMA_VERSION
        ):
            raise IngestionExecutionError("recovery_marker_conflict")
        return
    _write_exclusive_json(marker, payload)


def _clear_recovery_marker(
    target: TestExecutionTarget,
    batch_id: str,
) -> None:
    marker = _recovery_marker(target, batch_id)
    if marker.exists():
        if not marker.is_file() or is_reparse_point(marker):
            raise IngestionExecutionError("recovery_marker_unsafe")
        marker.unlink()


def _last_journal_digest(journal_path: Path) -> str:
    events = read_execution_journal(journal_path)
    return events[-1].event_digest if events else ZERO_DIGEST


def _write_sanitized_report(
    report_path: Path,
    *,
    bundle: ExecutionBundle,
    result: ExecutionResult,
) -> None:
    verification = result.verification
    payload = {
        "report_schema_version": SANITIZED_REPORT_SCHEMA_VERSION,
        "execution_id": bundle.execution_id,
        "batch_id": bundle.batch_id,
        "status": result.status,
        "plan_digest": bundle.plan_digest,
        "stage_plan_digest": bundle.stage_plan_digest,
        "approval_digest": bundle.approval_digest,
        "operation_counts": {
            "approved": len(bundle.approved_operations),
            "rejected": len(bundle.rejected_operations),
            "applied": len(result.applied_operation_ids),
        },
        "conflict_decision_count": len(bundle.conflict_decisions),
        "expected_deltas": dict(
            bundle.verification_expectations.expected_deltas
        ),
        "actual_deltas": (
            dict(verification.actual_deltas) if verification else None
        ),
        "verification_passed": (
            verification.passed if verification else False
        ),
        "recovery_status": result.status,
        "database_before_fingerprint": (
            result.database_before_fingerprint
        ),
        "database_after_fingerprint": result.database_after_fingerprint,
        "asset_after_fingerprint": (
            verification.asset_after_fingerprint if verification else None
        ),
        "reason_codes": list(result.reason_codes),
    }
    data = canonical_json_bytes(payload) + b"\n"
    if report_path.exists():
        report_path.unlink()
    descriptor = os.open(
        report_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _result(
    *,
    bundle: ExecutionBundle,
    status: str,
    reasons: Sequence[str],
    applied: Iterable[str],
    created: Iterable[str],
    reused: Iterable[str],
    after_fingerprint: str | None,
    verification: ExecutionVerificationResult | None,
    journal_path: Path,
    lease_token: WriterLeaseToken | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        execution_id=bundle.execution_id,
        status=status,
        reason_codes=tuple(reasons),
        applied_operation_ids=tuple(applied),
        created_asset_digests=tuple(created),
        reused_asset_digests=tuple(reused),
        database_before_fingerprint=bundle.target_snapshot_fingerprint,
        database_after_fingerprint=after_fingerprint,
        verification=verification,
        journal_digest=_last_journal_digest(journal_path),
        lease_token=lease_token,
    )


def execute_test_bundle(
    *,
    bundle: ExecutionBundle,
    target: TestExecutionTarget,
    before_snapshot: TestTargetSnapshot,
    staged_entities: Sequence[StagedEntitySource],
    fault_injector: Callable[[str], bool | None] | None = None,
    execution_delegate: Callable[..., ExecutionResult] | None = None,
) -> ExecutionResult:
    """Execute one immutable bundle against its isolated test target."""
    if execution_delegate is not None:
        return execution_delegate(
            bundle=bundle,
            target=target,
            before_snapshot=before_snapshot,
            staged_entities=staged_entities,
            fault_injector=fault_injector,
        )
    if (
        bundle.target_snapshot_fingerprint
        != before_snapshot.target_fingerprint
        or bundle.execution_root_fingerprint
        != target.execution_root_fingerprint
        or before_snapshot.target_identity_digest
        != target.target_identity_digest
    ):
        raise IngestionExecutionError("execution_bundle_target_mismatch")
    current = capture_test_target_snapshot(
        target.database_path,
        target.asset_root,
    )
    if current.target_fingerprint != before_snapshot.target_fingerprint:
        raise IngestionExecutionError("target_snapshot_changed")
    source_by_id = {item.operation_id: item for item in staged_entities}
    expected_sources = {
        item["operation_id"]: item for item in bundle.approved_assets
    }
    approved = set(bundle.approved_operations)
    if set(source_by_id) != set(expected_sources):
        raise IngestionExecutionError("asset_source_set_mismatch")
    for operation_id, source in source_by_id.items():
        expected = expected_sources[operation_id]
        if (
            source.entity_digest != expected["entity_digest"]
            or source.actual_size != expected["actual_size"]
            or source.detected_mime != expected["detected_mime"]
        ):
            raise IngestionExecutionError("asset_source_binding_mismatch")
    execution_root, journal_path, backup_path = _execution_paths(
        target,
        bundle.execution_id,
    )
    if execution_root.exists():
        raise IngestionExecutionError("execution_already_exists")
    if _recovery_marker(target, bundle.batch_id).exists():
        raise IngestionExecutionError("recovery_required_exists")
    _reject_reparse_components(execution_root)
    lease_token = acquire_writer_lease(target, bundle.batch_id)
    try:
        execution_root.mkdir(parents=True)
        _reject_reparse_components(execution_root)
    except Exception:
        release_writer_lease(target, lease_token)
        raise
    created: list[str] = []
    reused: list[str] = []
    applied: list[str] = []
    verification: ExecutionVerificationResult | None = None
    database_committed = False
    transaction_started = False
    try:
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="preflight_started",
        )
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="preflight_passed",
        )
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="lease_acquired",
        )
        if capture_test_target_snapshot(
            target.database_path,
            target.asset_root,
        ).target_fingerprint != before_snapshot.target_fingerprint:
            raise IngestionExecutionError("target_changed_after_lease")
        backup_digest = _create_database_backup(
            target,
            backup_path,
            before_snapshot,
        )
        if bundle.database_backup_fingerprint != normalized_json_digest({
            "target_fingerprint": before_snapshot.target_fingerprint,
            "database_content_sha256": (
                before_snapshot.database_content_sha256
            ),
        }):
            raise IngestionExecutionError("database_backup_binding_mismatch")
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="backup_created",
            details={"backup_digest": backup_digest},
        )
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="asset_promotion_started",
        )
        for operation_id in sorted(source_by_id):
            source = source_by_id[operation_id]
            created_here = _promote_entity(
                target=target,
                execution_id=bundle.execution_id,
                source=source,
            )
            (created if created_here else reused).append(source.entity_digest)
            applied.append(operation_id)
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="asset_promoted",
                details={
                    "operation_id": operation_id,
                    "entity_digest": source.entity_digest,
                    "created": created_here,
                },
            )
        if fault_injector and fault_injector("after_asset_promoted"):
            raise ExecutionInterrupted(
                "after_asset_promoted",
                lease_token,
            )
        ordered = _validate_operation_graph(
            bundle.database_operations,
            stage_operations=tuple(source_by_id),
            approved=approved,
        )
        _validate_lease_token(target, lease_token)
        connection = sqlite3.connect(target.database_path)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="database_transaction_started",
            )
            for operation in ordered:
                if operation.operation_id not in approved:
                    continue
                if operation.operation_type in DATABASE_OPERATION_TYPES:
                    handler = _OPERATION_HANDLERS.get(operation.operation_type)
                    if handler is None:
                        raise IngestionExecutionError("unknown_operation_type")
                    handler(connection, operation)
                    connection.execute(
                        """INSERT INTO execution_applied_operations(
                             operation_id,operation_type,payload_digest
                           ) VALUES(?,?,?)""",
                        (
                            operation.operation_id,
                            operation.operation_type,
                            operation.payload_digest,
                        ),
                    )
                applied.append(operation.operation_id)
                append_execution_journal_event(
                    journal_path,
                    execution_id=bundle.execution_id,
                    event_type="operation_applied",
                    details={"operation_id": operation.operation_id},
                )
            for decision in bundle.conflict_decisions:
                connection.execute(
                    """INSERT INTO execution_conflict_resolutions(
                         conflict_id,selected_action,evidence_digest
                       ) VALUES(?,?,?)""",
                    (
                        decision["conflict_id"],
                        decision["selected_action"],
                        decision["evidence_digest"],
                    ),
                )
            _transaction_invariants(connection)
            _validate_lease_token(target, lease_token)
            connection.commit()
            database_committed = True
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="database_committed",
            details={
                "operation_count": len(applied),
                "conflict_count": len(bundle.conflict_decisions),
            },
        )
        _validate_lease_token(target, lease_token)
        if fault_injector and fault_injector("after_database_committed"):
            raise ExecutionInterrupted(
                "after_database_committed",
                lease_token,
            )
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="verification_started",
        )
        count_connection = sqlite3.connect(target.database_path)
        try:
            resolved_conflicts = count_connection.execute(
                "SELECT COUNT(*) FROM execution_conflict_resolutions"
            ).fetchone()[0]
        finally:
            count_connection.close()
        after, verification = verify_test_execution(
            execution_id=bundle.execution_id,
            database_path=target.database_path,
            asset_root=target.asset_root,
            before_snapshot=before_snapshot,
            expectation=bundle.verification_expectations,
            executed_operation_ids=applied,
            resolved_conflict_count=resolved_conflicts,
        )
        if not verification.passed:
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="recovery_required",
                details={"reason_code": "verification_failed"},
            )
            _write_recovery_marker(
                target,
                bundle,
                "verification_failed",
            )
            result = _result(
                bundle=bundle,
                status="recovery_required",
                reasons=("verification_failed",),
                applied=applied,
                created=created,
                reused=reused,
                after_fingerprint=after.target_fingerprint,
                verification=verification,
                journal_path=journal_path,
                lease_token=lease_token,
            )
            _write_sanitized_report(
                execution_root / "audit-report.json",
                bundle=bundle,
                result=result,
            )
            return result
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="verification_passed",
            details={
                "verification_digest": verification.verification_digest,
                "database_after_fingerprint": after.target_fingerprint,
                "asset_after_fingerprint": (
                    verification.asset_after_fingerprint
                ),
            },
        )
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="execution_completed",
            details={"status": "completed"},
        )
        result = _result(
            bundle=bundle,
            status="completed",
            reasons=(),
            applied=applied,
            created=created,
            reused=reused,
            after_fingerprint=after.target_fingerprint,
            verification=verification,
            journal_path=journal_path,
        )
        _write_sanitized_report(
            execution_root / "audit-report.json",
            bundle=bundle,
            result=result,
        )
        release_writer_lease(target, lease_token)
        return result
    except ExecutionInterrupted:
        raise
    except Exception as error:
        reason = (
            error.reason_code
            if isinstance(error, IngestionExecutionError)
            else "execution_runtime_failed"
        )
        if database_committed:
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="recovery_required",
                details={"reason_code": reason},
            )
            _write_recovery_marker(target, bundle, reason)
            after_fingerprint = capture_test_target_snapshot(
                target.database_path,
                target.asset_root,
            ).target_fingerprint
            result = _result(
                bundle=bundle,
                status="recovery_required",
                reasons=(reason,),
                applied=applied,
                created=created,
                reused=reused,
                after_fingerprint=after_fingerprint,
                verification=verification,
                journal_path=journal_path,
                lease_token=lease_token,
            )
        else:
            cleanup_reason = reason
            if (
                transaction_started
                and reason
                in {
                    "lease_missing",
                    "lease_corrupt",
                    "lease_token_mismatch",
                    "lease_scope_mismatch",
                }
            ):
                append_execution_journal_event(
                    journal_path,
                    execution_id=bundle.execution_id,
                    event_type="recovery_required",
                    details={"reason_code": "lease_lost_during_commit"},
                )
                _write_recovery_marker(
                    target,
                    bundle,
                    "lease_lost_during_commit",
                )
                return _result(
                    bundle=bundle,
                    status="recovery_required",
                    reasons=("lease_lost_during_commit",),
                    applied=applied,
                    created=created,
                    reused=reused,
                    after_fingerprint=capture_test_target_snapshot(
                        target.database_path,
                        target.asset_root,
                    ).target_fingerprint,
                    verification=None,
                    journal_path=journal_path,
                )
            try:
                _remove_owned_assets(target, created)
            except IngestionExecutionError:
                cleanup_reason = "asset_cleanup_failed"
                append_execution_journal_event(
                    journal_path,
                    execution_id=bundle.execution_id,
                    event_type="recovery_required",
                    details={"reason_code": cleanup_reason},
                )
                _write_recovery_marker(target, bundle, cleanup_reason)
                return _result(
                    bundle=bundle,
                    status="recovery_required",
                    reasons=(cleanup_reason,),
                    applied=applied,
                    created=created,
                    reused=reused,
                    after_fingerprint=capture_test_target_snapshot(
                        target.database_path,
                        target.asset_root,
                    ).target_fingerprint,
                    verification=None,
                    journal_path=journal_path,
                    lease_token=lease_token,
                )
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="execution_failed",
                details={"reason_code": cleanup_reason},
            )
            release_writer_lease(target, lease_token)
            result = _result(
                bundle=bundle,
                status="failed",
                reasons=(cleanup_reason,),
                applied=applied,
                created=(),
                reused=reused,
                after_fingerprint=capture_test_target_snapshot(
                    target.database_path,
                    target.asset_root,
                ).target_fingerprint,
                verification=None,
                journal_path=journal_path,
            )
        try:
            _write_sanitized_report(
                execution_root / "audit-report.json",
                bundle=bundle,
                result=result,
            )
        except OSError:
            pass
        return result


@dataclass(frozen=True)
class RecoveryReport:
    execution_id: str
    status: str
    reason_codes: tuple[str, ...]
    removed_asset_digests: tuple[str, ...]
    journal_digest: str
    verification: ExecutionVerificationResult | None
    lease_token: WriterLeaseToken | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    report_schema_version: str = RECOVERY_REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.execution_id, "invalid_execution_id")
        if self.status not in {
            "completed",
            "rolled_back",
            "recovery_required",
            "already_completed",
        }:
            raise IngestionExecutionError("invalid_recovery_status")
        object.__setattr__(
            self,
            "reason_codes",
            tuple(sorted(set(
                sanitize_reason_code(item) for item in self.reason_codes
            ))),
        )
        removed = tuple(sorted(set(self.removed_asset_digests)))
        for digest in removed:
            _sha(digest, "invalid_asset_digest")
        object.__setattr__(self, "removed_asset_digests", removed)
        _sha(self.journal_digest, "invalid_journal_digest")
        if self.report_schema_version != RECOVERY_REPORT_SCHEMA_VERSION:
            raise IngestionExecutionError("recovery_schema_version_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "removed_asset_digests": list(self.removed_asset_digests),
            "journal_digest": self.journal_digest,
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "report_schema_version": self.report_schema_version,
        }


def _journal_owned_assets(
    events: Sequence[ExecutionJournalEvent],
) -> tuple[str, ...]:
    return tuple(sorted({
        str(event.details["entity_digest"])
        for event in events
        if (
            event.event_type == "asset_promoted"
            and event.details.get("created") is True
        )
    }))


def _journal_applied_operations(
    events: Sequence[ExecutionJournalEvent],
) -> tuple[str, ...]:
    return tuple(sorted({
        str(event.details["operation_id"])
        for event in events
        if event.event_type in {"asset_promoted", "operation_applied"}
    }))


def recover_test_execution(
    *,
    bundle: ExecutionBundle,
    target: TestExecutionTarget,
    before_snapshot: TestTargetSnapshot,
    lease_token: WriterLeaseToken,
) -> RecoveryReport:
    """Recover a deliberately interrupted execution using exact lease authority."""
    _, journal_path, _ = _execution_paths(target, bundle.execution_id)
    events = read_execution_journal(
        journal_path,
        execution_id=bundle.execution_id,
    )
    if not events:
        raise IngestionExecutionError("execution_journal_missing")
    if any(item.event_type == "execution_completed" for item in events):
        return RecoveryReport(
            execution_id=bundle.execution_id,
            status="already_completed",
            reason_codes=(),
            removed_asset_digests=(),
            journal_digest=events[-1].event_digest,
            verification=None,
        )
    _validate_lease_token(target, lease_token)
    owned = _journal_owned_assets(events)
    applied = _journal_applied_operations(events)
    committed = any(
        item.event_type == "database_committed" for item in events
    )
    if committed:
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="verification_started",
        )
        connection = sqlite3.connect(target.database_path)
        try:
            conflict_count = connection.execute(
                "SELECT COUNT(*) FROM execution_conflict_resolutions"
            ).fetchone()[0]
        finally:
            connection.close()
        try:
            after, verification = verify_test_execution(
                execution_id=bundle.execution_id,
                database_path=target.database_path,
                asset_root=target.asset_root,
                before_snapshot=before_snapshot,
                expectation=bundle.verification_expectations,
                executed_operation_ids=applied,
                resolved_conflict_count=conflict_count,
            )
        except (
            IngestionVerificationError,
            OSError,
            sqlite3.Error,
        ):
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="recovery_required",
                details={"reason_code": "recovery_verification_unavailable"},
            )
            _write_recovery_marker(
                target,
                bundle,
                "recovery_verification_unavailable",
            )
            return RecoveryReport(
                execution_id=bundle.execution_id,
                status="recovery_required",
                reason_codes=("recovery_verification_unavailable",),
                removed_asset_digests=(),
                journal_digest=_last_journal_digest(journal_path),
                verification=None,
                lease_token=lease_token,
            )
        if verification.passed:
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="verification_passed",
                details={
                    "verification_digest": verification.verification_digest,
                    "database_after_fingerprint": after.target_fingerprint,
                    "asset_after_fingerprint": (
                        verification.asset_after_fingerprint
                    ),
                },
            )
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="execution_completed",
                details={"status": "completed"},
            )
            _clear_recovery_marker(target, bundle.batch_id)
            release_writer_lease(target, lease_token)
            current_events = read_execution_journal(
                journal_path,
                execution_id=bundle.execution_id,
            )
            return RecoveryReport(
                execution_id=bundle.execution_id,
                status="completed",
                reason_codes=(),
                removed_asset_digests=(),
                journal_digest=current_events[-1].event_digest,
                verification=verification,
            )
        if not any(
            item.event_type == "recovery_required" for item in events
        ):
            append_execution_journal_event(
                journal_path,
                execution_id=bundle.execution_id,
                event_type="recovery_required",
                details={"reason_code": "post_commit_verification_failed"},
            )
        _write_recovery_marker(
            target,
            bundle,
            "post_commit_verification_failed",
        )
        return RecoveryReport(
            execution_id=bundle.execution_id,
            status="recovery_required",
            reason_codes=("post_commit_verification_failed",),
            removed_asset_digests=(),
            journal_digest=_last_journal_digest(journal_path),
            verification=verification,
            lease_token=lease_token,
        )
    append_execution_journal_event(
        journal_path,
        execution_id=bundle.execution_id,
        event_type="recovery_required",
        details={"reason_code": "interrupted_before_database_commit"},
    )
    _write_recovery_marker(
        target,
        bundle,
        "interrupted_before_database_commit",
    )
    append_execution_journal_event(
        journal_path,
        execution_id=bundle.execution_id,
        event_type="rollback_started",
        details={"owned_asset_count": len(owned)},
    )
    try:
        removed = _remove_owned_assets(target, owned)
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="asset_rolled_back",
            details={"owned_asset_count": len(removed)},
        )
        current = capture_test_target_snapshot(
            target.database_path,
            target.asset_root,
        )
        if current.target_fingerprint != before_snapshot.target_fingerprint:
            raise IngestionExecutionError(
                "precommit_recovery_target_changed"
            )
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="rollback_completed",
            details={"status": "rolled_back"},
        )
        _clear_recovery_marker(target, bundle.batch_id)
        release_writer_lease(target, lease_token)
        return RecoveryReport(
            execution_id=bundle.execution_id,
            status="rolled_back",
            reason_codes=(),
            removed_asset_digests=removed,
            journal_digest=_last_journal_digest(journal_path),
            verification=None,
        )
    except IngestionExecutionError as error:
        _write_recovery_marker(target, bundle, error.reason_code)
        return RecoveryReport(
            execution_id=bundle.execution_id,
            status="recovery_required",
            reason_codes=(error.reason_code,),
            removed_asset_digests=(),
            journal_digest=_last_journal_digest(journal_path),
            verification=None,
            lease_token=lease_token,
        )


@dataclass(frozen=True)
class RollbackReport:
    execution_id: str
    status: str
    reason_codes: tuple[str, ...]
    database_restored: bool
    removed_asset_digests: tuple[str, ...]
    preserved_shared_asset_digests: tuple[str, ...]
    target_after_fingerprint: str | None
    journal_digest: str
    lease_token: WriterLeaseToken | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    report_schema_version: str = ROLLBACK_REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.execution_id, "invalid_execution_id")
        if self.status not in {"rolled_back", "recovery_required"}:
            raise IngestionExecutionError("invalid_rollback_status")
        object.__setattr__(
            self,
            "reason_codes",
            tuple(sorted(set(
                sanitize_reason_code(item) for item in self.reason_codes
            ))),
        )
        for field_name in (
            "removed_asset_digests",
            "preserved_shared_asset_digests",
        ):
            values = tuple(sorted(set(getattr(self, field_name))))
            for digest in values:
                _sha(digest, "invalid_asset_digest")
            object.__setattr__(self, field_name, values)
        if self.target_after_fingerprint is not None:
            _sha(
                self.target_after_fingerprint,
                "invalid_target_after_fingerprint",
            )
        _sha(self.journal_digest, "invalid_journal_digest")
        if self.report_schema_version != ROLLBACK_REPORT_SCHEMA_VERSION:
            raise IngestionExecutionError("rollback_schema_version_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "database_restored": self.database_restored,
            "removed_asset_digests": list(self.removed_asset_digests),
            "preserved_shared_asset_digests": list(
                self.preserved_shared_asset_digests
            ),
            "target_after_fingerprint": self.target_after_fingerprint,
            "journal_digest": self.journal_digest,
            "report_schema_version": self.report_schema_version,
        }


def rollback_test_execution(
    *,
    bundle: ExecutionBundle,
    target: TestExecutionTarget,
    before_snapshot: TestTargetSnapshot,
    import_plan: ImportPlan,
    stage_plan: AssetStagePlan,
    rollback_approval: ApprovalArtifact,
    now: str,
    lease_token: WriterLeaseToken | None = None,
) -> RollbackReport:
    """Restore the test DB backup and remove only journal-owned entities."""
    execution_root, journal_path, backup_path = _execution_paths(
        target,
        bundle.execution_id,
    )
    events = read_execution_journal(
        journal_path,
        execution_id=bundle.execution_id,
    )
    if not events:
        raise IngestionExecutionError("execution_journal_missing")
    current = capture_test_target_snapshot(
        target.database_path,
        target.asset_root,
    )
    candidate_ids = set(bundle.approved_operations) | set(
        bundle.rejected_operations
    )
    validate_approval_artifact(
        rollback_approval,
        import_plan=import_plan,
        stage_plan=stage_plan,
        target_snapshot_fingerprint=current.target_fingerprint,
        candidate_operation_ids=tuple(candidate_ids),
        now=now,
        required_scope="rollback_only",
    )
    if (
        set(rollback_approval.approved_operation_ids)
        != set(bundle.approved_operations)
        or set(rollback_approval.rejected_operation_ids)
        != set(bundle.rejected_operations)
    ):
        raise IngestionExecutionError("rollback_approval_operation_mismatch")
    authority = lease_token or acquire_writer_lease(target, bundle.batch_id)
    _validate_lease_token(target, authority)
    owned = _journal_owned_assets(events)
    promoted = {
        str(event.details["entity_digest"])
        for event in events
        if event.event_type == "asset_promoted"
    }
    shared = tuple(sorted(promoted - set(owned)))
    append_execution_journal_event(
        journal_path,
        execution_id=bundle.execution_id,
        event_type="rollback_started",
        details={"owned_asset_count": len(owned)},
    )
    removed: tuple[str, ...] = ()
    restored = False
    try:
        removed = _remove_owned_assets(target, owned)
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="asset_rolled_back",
            details={"owned_asset_count": len(removed)},
        )
        _restore_database_backup(target, backup_path)
        restored = True
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="database_restored",
        )
        after = capture_test_target_snapshot(
            target.database_path,
            target.asset_root,
        )
        if after.target_fingerprint != before_snapshot.target_fingerprint:
            raise IngestionExecutionError("rollback_verification_failed")
        append_execution_journal_event(
            journal_path,
            execution_id=bundle.execution_id,
            event_type="rollback_completed",
            details={"status": "rolled_back"},
        )
        _clear_recovery_marker(target, bundle.batch_id)
        release_writer_lease(target, authority)
        return RollbackReport(
            execution_id=bundle.execution_id,
            status="rolled_back",
            reason_codes=(),
            database_restored=True,
            removed_asset_digests=removed,
            preserved_shared_asset_digests=shared,
            target_after_fingerprint=after.target_fingerprint,
            journal_digest=_last_journal_digest(journal_path),
        )
    except (IngestionExecutionError, sqlite3.Error, OSError) as error:
        reason = (
            error.reason_code
            if isinstance(error, IngestionExecutionError)
            else "rollback_runtime_failed"
        )
        _write_recovery_marker(target, bundle, reason)
        try:
            after_fingerprint = capture_test_target_snapshot(
                target.database_path,
                target.asset_root,
            ).target_fingerprint
        except Exception:
            after_fingerprint = None
        return RollbackReport(
            execution_id=bundle.execution_id,
            status="recovery_required",
            reason_codes=(reason,),
            database_restored=restored,
            removed_asset_digests=removed,
            preserved_shared_asset_digests=shared,
            target_after_fingerprint=after_fingerprint,
            journal_digest=_last_journal_digest(journal_path),
            lease_token=authority,
        )
