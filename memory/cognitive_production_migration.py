"""One-shot, fixed-path production adoption for cognitive schema generation 10001.

The ordinary :func:`memory.cognitive_state_migration.apply_cognitive_state_migration`
remains clone-only.  This module is the deliberately separate production
boundary: its public execution and reconciliation entry points accept no
database path, SQL, force, downgrade, or rollback arguments.  They bind the
code-defined Operational State database to canonical authority and verified
backup records stored in one code-defined local control directory.

The migration preserves the v9 core marker/version 9001.  Generation 10001 is
an additive extension marker in ``cognitive_schema``; no v9 table is rebuilt,
renamed, deleted, or rewritten other than the required whole-schema digest in
``operational_schema``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import uuid
from contextlib import ExitStack, closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping

from memory.secure_control_files import (
    PinnedControlDirectory,
    PinnedControlFile,
    PinnedMutableControlFile,
    SecureControlFileError,
    _windows_final_handle_path,
    _windows_handle_info,
    pinned_control_directory,
)

from memory.cognitive_state_migration import (
    COGNITIVE_CONTRACT_VERSION,
    COGNITIVE_SCHEMA_GENERATION,
    COGNITIVE_SCHEMA_MARKER,
    COGNITIVE_SCHEMA_RESOURCE,
    _COGNITIVE_DDL,
    _COGNITIVE_OBJECTS,
    _COGNITIVE_OBJECT_NAMES,
    _all_named_schema_objects,
    _existing_cognitive_objects,
    _verify_cognitive_connection,
)
from memory.db_path_policy import (
    DEFAULT_OPERATIONAL_STATE_PATH,
    is_reparse_point,
)
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    canonical_json,
    normalize_timestamp,
    utc_now,
    validate_identity,
    validate_sha256,
)
from memory.operational_state_store import (
    PRODUCTION_WRITE_ENV,
    SCHEMA_MARKER,
    SCHEMA_VERSION,
    OperationalStateStore,
    is_production_operational_path,
    schema_fingerprint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_OPERATIONAL_DATABASE = (
    PROJECT_ROOT / "data" / "operational" / "ayunos_operational_state.db"
).absolute()
PRODUCTION_MIGRATION_CONTROL_ROOT = (
    PROJECT_ROOT / "output" / "v10-f-production-adoption"
).absolute()
PRODUCTION_AUTHORITY_RECORD = (
    PRODUCTION_MIGRATION_CONTROL_ROOT / "cognitive_migration_authority.json"
)
PRODUCTION_BACKUP_VERIFICATION_RECORD = (
    PRODUCTION_MIGRATION_CONTROL_ROOT
    / "cognitive_migration_backup_verification.json"
)
PRODUCTION_QUIESCENCE_RECORD = (
    PRODUCTION_MIGRATION_CONTROL_ROOT / "service_quiescence_evidence.json"
)
PRODUCTION_PLAN_RECORD = (
    PRODUCTION_MIGRATION_CONTROL_ROOT / "cognitive_migration_plan.json"
)
PRODUCTION_CONTROL_MANIFEST = (
    PRODUCTION_MIGRATION_CONTROL_ROOT / "control_manifest.json"
)
PRODUCTION_EXECUTION_LEDGER = (
    PRODUCTION_MIGRATION_CONTROL_ROOT / "cognitive_migration_execution.jsonl"
)
PRODUCTION_EXECUTION_LOCK = (
    PRODUCTION_MIGRATION_CONTROL_ROOT / "cognitive_migration_execution.lock"
)

COGNITIVE_PRODUCTION_MIGRATION_ID = (
    "yunos-v10-operational-cognitive-schema-10001"
)
COGNITIVE_PRODUCTION_CAPABILITY = (
    "migrate_operational_cognitive_schema_10001"
)
COGNITIVE_PRODUCTION_PLAN_VERSION = (
    "yunos-v10-f-cognitive-migration-plan-1"
)
COGNITIVE_PRODUCTION_AUTHORITY_VERSION = (
    "yunos-v10-f-cognitive-migration-authority-1"
)
COGNITIVE_PRODUCTION_BACKUP_VERSION = (
    "yunos-v10-f-cognitive-migration-backup-verification-1"
)
COGNITIVE_PRODUCTION_LEDGER_VERSION = (
    "yunos-v10-f-cognitive-migration-execution-ledger-1"
)
COGNITIVE_PRODUCTION_QUIESCENCE_VERSION = (
    "yunos-v10-f-service-quiescence-evidence-1"
)
COGNITIVE_PRODUCTION_LOCK_VERSION = (
    "yunos-v10-f-cognitive-migration-os-lock-1"
)
COGNITIVE_PRODUCTION_QUIESCENCE_TTL_SECONDS = 120
COGNITIVE_PRODUCTION_ROLLBACK_POLICY = "verified_backup_restore_only"
COGNITIVE_DDL_DIGEST = hashlib.sha256(
    COGNITIVE_SCHEMA_RESOURCE.read_bytes()
).hexdigest()

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{2,127}\Z")
_PRODUCTION_RELATIVE_PATH = (
    "data/operational/ayunos_operational_state.db"
)
COGNITIVE_PRODUCTION_AUTHORITY_ID = (
    "authority:yunos-v10-f-production-migration"
)
COGNITIVE_PRODUCTION_AUTHORITY_ISSUER = "user:yunos-owner"
COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256 = hashlib.sha256(
    "授权，继续".encode("utf-8")
).hexdigest()
COGNITIVE_PRODUCTION_AUTHORITY_TTL_SECONDS = 30 * 60
COGNITIVE_CAPTURE_ARTIFACT_VERSION = (
    "yunos-v10-f-production-quiescence-artifact-1"
)
COGNITIVE_CAPTURE_ISSUER_VERSION = (
    "yunos-v10-f-production-quiescence-issuer-1"
)
COGNITIVE_CAPTURE_ISSUER_ID = (
    "controller:yunos-v10-f-production-quiescence"
)
COGNITIVE_CAPTURE_ROOT_RELATIVE_PATH = "output/v10-f-quiescence-evidence"
COGNITIVE_CAPTURE_TAILSCALE_COMMANDS = (
    ("status", "--json"),
    ("serve", "status", "--json"),
    ("funnel", "status", "--json"),
    ("serve", "status", "--json"),
)
COGNITIVE_CAPTURE_CONTROLLER_DIGEST = canonical_digest(
    {
        "artifact_version": COGNITIVE_CAPTURE_ARTIFACT_VERSION,
        "issuer_id": COGNITIVE_CAPTURE_ISSUER_ID,
        "issuer_version": COGNITIVE_CAPTURE_ISSUER_VERSION,
        "operational_database": _PRODUCTION_RELATIVE_PATH,
        "tailscale_commands": [
            list(arguments)
            for arguments in COGNITIVE_CAPTURE_TAILSCALE_COMMANDS
        ],
        "user_authorization_text_digest": (
            COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256
        ),
    }
)
COGNITIVE_CONTROL_PREPARATION_VERSION = (
    "yunos-v10-f-production-control-preparation-1"
)
_CAPTURE_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
_CAPTURE_SOURCE_IDS = frozenset(
    {"capture_controller", "operational_database", "tailscale_executable"}
)
_CONTROL_DOCUMENT_FILENAMES = frozenset(
    {
        "cognitive_migration_plan.json",
        "cognitive_migration_backup_verification.json",
        "cognitive_migration_authority.json",
        "service_quiescence_evidence.json",
        "control_manifest.json",
    }
)
_LEDGER_STATES = frozenset(
    {"claimed", "completed", "rolled_back", "recovery_required", "restored"}
)
_AUTHORITY_PERMISSIONS = {
    "allowed_capabilities": [COGNITIVE_PRODUCTION_CAPABILITY],
    "database_write": "operational_cognitive_schema_10001_only",
    "commit": False,
    "merge": False,
    "tag": False,
    "push": False,
    "network": False,
    "next_version_creation": False,
    "persistent_background_agent": False,
    "force": False,
    "arbitrary_sql": False,
    "rollback_policy": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
}


class CognitiveProductionMigrationError(OperationalStateError):
    """Fail-closed production error with an explicit recovery decision."""

    def __init__(
        self,
        reason_code: str,
        *,
        recovery_required: bool = False,
    ) -> None:
        safe_reason = (
            reason_code
            if isinstance(reason_code, str)
            and _SAFE_REASON_RE.fullmatch(reason_code)
            else "cognitive_production_migration_failed"
        )
        self.recovery_required = bool(recovery_required)
        super().__init__(safe_reason)


@dataclass(frozen=True)
class _BoundProductionPaths:
    database: Path
    plan_record: Path
    authority_record: Path
    backup_record: Path
    quiescence_record: Path
    control_manifest: Path
    ledger: Path
    lock: Path
    production: bool


@dataclass(frozen=True)
class _PublicationPin:
    path: Path
    descriptor: int
    canonical_path: str
    device: int
    inode: int
    size: int
    sha256: str


_ACTIVE_CONTROL_DIRECTORY_PIN: ContextVar[
    PinnedControlDirectory | None
] = ContextVar("v10_f_active_control_directory_pin", default=None)


def _fixed_production_paths() -> _BoundProductionPaths:
    """Rebuild code-owned identities; do not trust mutable public constants."""

    root = Path(__file__).resolve().parents[1]
    control = root / "output" / "v10-f-production-adoption"
    return _BoundProductionPaths(
        database=(
            root / "data" / "operational" / "ayunos_operational_state.db"
        ).absolute(),
        plan_record=(control / "cognitive_migration_plan.json").absolute(),
        authority_record=(control / "cognitive_migration_authority.json").absolute(),
        backup_record=(
            control / "cognitive_migration_backup_verification.json"
        ).absolute(),
        quiescence_record=(control / "service_quiescence_evidence.json").absolute(),
        control_manifest=(control / "control_manifest.json").absolute(),
        ledger=(control / "cognitive_migration_execution.jsonl").absolute(),
        lock=(control / "cognitive_migration_execution.lock").absolute(),
        production=True,
    )


def _paths_for_clone_test(database: Path, control_root: Path) -> _BoundProductionPaths:
    """Private test seam. Public production APIs never call or expose this."""

    control = control_root.absolute()
    return _BoundProductionPaths(
        database=database.absolute(),
        plan_record=control / "cognitive_migration_plan.json",
        authority_record=control / "cognitive_migration_authority.json",
        backup_record=control / "cognitive_migration_backup_verification.json",
        quiescence_record=control / "service_quiescence_evidence.json",
        control_manifest=control / "control_manifest.json",
        ledger=control / "cognitive_migration_execution.jsonl",
        lock=control / "cognitive_migration_execution.lock",
        production=False,
    )


def _reason_from_exception(exc: BaseException) -> str:
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str) and _SAFE_REASON_RE.fullmatch(reason):
        return reason
    return "cognitive_production_migration_failed"


def _sha(value: Any, reason_code: str) -> str:
    try:
        return validate_sha256(value, reason_code)
    except OperationalStateError:
        raise CognitiveProductionMigrationError(reason_code) from None


def _identity(value: Any, reason_code: str) -> str:
    try:
        return validate_identity(value, reason_code)
    except OperationalStateError:
        raise CognitiveProductionMigrationError(reason_code) from None


def _commit(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise CognitiveProductionMigrationError(reason_code)
    return value


def _timestamp(value: Any, reason_code: str) -> datetime:
    try:
        normalized = normalize_timestamp(value)
    except OperationalStateError:
        raise CognitiveProductionMigrationError(reason_code) from None
    if normalized != value:
        raise CognitiveProductionMigrationError(reason_code)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def _utc_datetime(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise CognitiveProductionMigrationError("migration_clock_invalid")
    return current.astimezone(timezone.utc)


def _safe_package_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_path_invalid"
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ":" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_path_invalid"
        )
    return value


def validate_cognitive_production_quiescence_evidence(
    value: Mapping[str, Any],
    *,
    now: datetime | None = None,
    authorized_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate measured service quiescence and its short authority window."""

    if not isinstance(value, Mapping):
        raise CognitiveProductionMigrationError(
            "service_quiescence_evidence_invalid"
        )
    record = dict(value)
    expected = {
        "version",
        "captured_at",
        "local_service_port",
        "local_service_listener_count",
        "local_service_stopped",
        "writer_lease_count",
        "operational_wal_size",
        "production_write_env_enabled",
        "tailscale_backend",
        "tailscale_serve_config_digest",
        "funnel_enabled",
        "evidence_digest",
    }
    if set(record) != expected:
        raise CognitiveProductionMigrationError(
            "service_quiescence_evidence_invalid"
        )
    unsigned = {key: record[key] for key in record if key != "evidence_digest"}
    captured = _timestamp(
        record["captured_at"], "service_quiescence_evidence_invalid"
    )
    for field in ("tailscale_serve_config_digest", "evidence_digest"):
        _sha(record[field], "service_quiescence_evidence_invalid")
    if (
        record["version"] != COGNITIVE_PRODUCTION_QUIESCENCE_VERSION
        or record["local_service_port"] != 8765
        or type(record["local_service_listener_count"]) is not int
        or record["local_service_listener_count"] != 0
        or record["local_service_stopped"] is not True
        or type(record["writer_lease_count"]) is not int
        or record["writer_lease_count"] != 0
        or type(record["operational_wal_size"]) is not int
        or record["operational_wal_size"] != 0
        or record["production_write_env_enabled"] is not False
        or record["tailscale_backend"] != "Running"
        or record["funnel_enabled"] is not False
        or record["evidence_digest"] != canonical_digest(unsigned)
    ):
        raise CognitiveProductionMigrationError(
            "service_quiescence_evidence_invalid"
        )
    for instant, future_reason, stale_reason in (
        (
            authorized_at,
            "service_quiescence_authority_mismatch",
            "service_quiescence_authority_mismatch",
        ),
        (now, "service_quiescence_evidence_not_yet_valid", "service_quiescence_evidence_stale"),
    ):
        if instant is None:
            continue
        current = _utc_datetime(instant)
        if captured > current:
            raise CognitiveProductionMigrationError(future_reason)
        if (current - captured).total_seconds() > COGNITIVE_PRODUCTION_QUIESCENCE_TTL_SECONDS:
            raise CognitiveProductionMigrationError(stale_reason)
    return record


def _capture_relative_path(captured_at: str) -> str:
    stamp = captured_at.replace(":", "").replace(".", "").replace("-", "")
    if not stamp or any(character not in "0123456789TZ" for character in stamp):
        raise CognitiveProductionMigrationError(
            "capture_artifact_path_invalid"
        )
    return (
        COGNITIVE_CAPTURE_ROOT_RELATIVE_PATH
        + "/service_quiescence_evidence_"
        + stamp
        + ".json"
    )


def _validate_capture_source_identities(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CAPTURE_SOURCE_IDS:
        raise CognitiveProductionMigrationError(
            "capture_artifact_source_identity_invalid"
        )
    fields = {
        "source_id",
        "path_scope",
        "path_reference",
        "device",
        "inode",
        "size",
        "mtime_ns",
        "sha256",
    }
    normalized: dict[str, Any] = {}
    for source_id in sorted(_CAPTURE_SOURCE_IDS):
        item_value = value.get(source_id)
        if not isinstance(item_value, Mapping) or set(item_value) != fields:
            raise CognitiveProductionMigrationError(
                "capture_artifact_source_identity_invalid"
            )
        item = dict(item_value)
        if (
            item.get("source_id") != source_id
            or item.get("path_scope")
            not in {"project", "external-path-digest"}
            or not isinstance(item.get("path_reference"), str)
            or not item["path_reference"]
            or any(
                type(item.get(field)) is not int or item[field] < 0
                for field in ("device", "inode", "size", "mtime_ns")
            )
        ):
            raise CognitiveProductionMigrationError(
                "capture_artifact_source_identity_invalid"
            )
        _sha(item.get("sha256"), "capture_artifact_source_identity_invalid")
        if source_id == "capture_controller":
            if (
                item["path_scope"] != "project"
                or item["path_reference"]
                != "tools/v10_f_capture_production_quiescence.py"
            ):
                raise CognitiveProductionMigrationError(
                    "capture_artifact_source_identity_invalid"
                )
        elif source_id == "operational_database":
            if (
                item["path_scope"] != "project"
                or item["path_reference"] != _PRODUCTION_RELATIVE_PATH
            ):
                raise CognitiveProductionMigrationError(
                    "capture_artifact_source_identity_invalid"
                )
        elif (
            item["path_scope"] != "external-path-digest"
            or not re.fullmatch(r"[0-9a-f]{64}", item["path_reference"])
        ):
            raise CognitiveProductionMigrationError(
                "capture_artifact_source_identity_invalid"
            )
        normalized[source_id] = item
    return normalized


def build_cognitive_quiescence_capture_artifact(
    *,
    evidence: Mapping[str, Any],
    source_identities: Mapping[str, Any],
    nonce: str,
) -> dict[str, Any]:
    """Build the closed capture artifact shared by controller and consumer."""

    normalized_evidence = validate_cognitive_production_quiescence_evidence(
        evidence
    )
    sources = _validate_capture_source_identities(source_identities)
    if not isinstance(nonce, str) or _CAPTURE_NONCE_RE.fullmatch(nonce) is None:
        raise CognitiveProductionMigrationError("capture_artifact_nonce_invalid")
    issuer_basis = {
        "version": COGNITIVE_CAPTURE_ISSUER_VERSION,
        "issuer_id": COGNITIVE_CAPTURE_ISSUER_ID,
        "nonce": nonce,
        "capture_digest": normalized_evidence["evidence_digest"],
        "capture_relative_path": _capture_relative_path(
            normalized_evidence["captured_at"]
        ),
        "source_identities": sources,
        "source_identities_digest": canonical_digest(sources),
        "controller_digest": COGNITIVE_CAPTURE_CONTROLLER_DIGEST,
        "user_authorization_text_digest": (
            COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256
        ),
        "captured_at": normalized_evidence["captured_at"],
    }
    artifact_digest = canonical_digest(
        {
            "version": COGNITIVE_CAPTURE_ARTIFACT_VERSION,
            "evidence": normalized_evidence,
            "issuer": issuer_basis,
        }
    )
    issuer = dict(issuer_basis)
    issuer["artifact_digest"] = artifact_digest
    issuer["issuer_digest"] = canonical_digest(issuer)
    artifact = {
        "version": COGNITIVE_CAPTURE_ARTIFACT_VERSION,
        "evidence": normalized_evidence,
        "issuer": issuer,
        "artifact_digest": artifact_digest,
    }
    artifact["record_digest"] = canonical_digest(artifact)
    return artifact


def validate_cognitive_quiescence_capture_artifact(
    value: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate the complete issuer/source/path binding, not raw evidence."""

    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "evidence",
        "issuer",
        "artifact_digest",
        "record_digest",
    }:
        raise CognitiveProductionMigrationError("capture_artifact_invalid")
    artifact = dict(value)
    if artifact.get("version") != COGNITIVE_CAPTURE_ARTIFACT_VERSION:
        raise CognitiveProductionMigrationError("capture_artifact_invalid")
    evidence_value = artifact.get("evidence")
    issuer_value = artifact.get("issuer")
    if not isinstance(evidence_value, Mapping) or not isinstance(
        issuer_value, Mapping
    ):
        raise CognitiveProductionMigrationError("capture_artifact_invalid")
    evidence = validate_cognitive_production_quiescence_evidence(
        evidence_value, now=now
    )
    issuer_fields = {
        "version",
        "issuer_id",
        "nonce",
        "capture_digest",
        "capture_relative_path",
        "source_identities",
        "source_identities_digest",
        "controller_digest",
        "user_authorization_text_digest",
        "captured_at",
        "artifact_digest",
        "issuer_digest",
    }
    if set(issuer_value) != issuer_fields:
        raise CognitiveProductionMigrationError("capture_artifact_issuer_invalid")
    issuer = dict(issuer_value)
    sources = _validate_capture_source_identities(issuer["source_identities"])
    issuer_basis = {
        key: issuer[key]
        for key in issuer_fields
        if key not in {"artifact_digest", "issuer_digest"}
    }
    issuer_basis["source_identities"] = sources
    expected_artifact_digest = canonical_digest(
        {
            "version": COGNITIVE_CAPTURE_ARTIFACT_VERSION,
            "evidence": evidence,
            "issuer": issuer_basis,
        }
    )
    if (
        issuer["version"] != COGNITIVE_CAPTURE_ISSUER_VERSION
        or issuer["issuer_id"] != COGNITIVE_CAPTURE_ISSUER_ID
        or not isinstance(issuer["nonce"], str)
        or _CAPTURE_NONCE_RE.fullmatch(issuer["nonce"]) is None
        or issuer["capture_digest"] != evidence["evidence_digest"]
        or issuer["capture_relative_path"]
        != _capture_relative_path(evidence["captured_at"])
        or issuer["source_identities_digest"] != canonical_digest(sources)
        or issuer["controller_digest"]
        != COGNITIVE_CAPTURE_CONTROLLER_DIGEST
        or issuer["user_authorization_text_digest"]
        != COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256
        or issuer["captured_at"] != evidence["captured_at"]
        or issuer["artifact_digest"] != expected_artifact_digest
        or artifact.get("artifact_digest") != expected_artifact_digest
        or issuer["issuer_digest"]
        != canonical_digest(
            {key: issuer[key] for key in issuer_fields if key != "issuer_digest"}
        )
        or artifact.get("record_digest")
        != canonical_digest(
            {key: artifact[key] for key in artifact if key != "record_digest"}
        )
    ):
        raise CognitiveProductionMigrationError(
            "capture_artifact_digest_mismatch"
        )
    artifact["evidence"] = evidence
    artifact["issuer"] = issuer
    return artifact


def cognitive_production_execution_id(
    *, plan_digest: str, backup_set_digest: str, code_commit: str
) -> str:
    return (
        "execution:yunos-v10-f-schema-10001-"
        + canonical_digest(
            {
                "plan_digest": _sha(plan_digest, "migration_plan_invalid"),
                "backup_set_digest": _sha(
                    backup_set_digest, "backup_verification_invalid"
                ),
                "code_commit": _commit(code_commit, "code_commit_invalid"),
            }
        )[:20]
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise CognitiveProductionMigrationError("database_hash_failed") from None
    return digest.hexdigest()


def _file_gate(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {"exists": False, "size": 0, "sha256": None}
        return {
            "exists": True,
            "size": path.stat().st_size,
            "sha256": _hash_file(path),
        }
    except OSError:
        raise CognitiveProductionMigrationError("database_gate_failed") from None


def _wal_gate(database: Path) -> dict[str, Any]:
    gate = _file_gate(Path(str(database) + "-wal"))
    # SQLite may remove an empty WAL when the last read-only connection
    # closes.  Absence and a zero-byte WAL carry the same quiescent content;
    # normalize them so this observational cleanup is not mistaken for source
    # database drift.  Every non-empty WAL remains a hard preflight failure.
    if not gate["exists"] or gate["size"] == 0:
        return {"exists": False, "size": 0, "sha256": None}
    return gate


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _same_path(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _reject_reparse_chain(path: Path) -> None:
    current = path.absolute()
    while True:
        try:
            if (current.exists() or current.is_symlink()) and is_reparse_point(
                current
            ):
                raise CognitiveProductionMigrationError(
                    "migration_path_reparse_forbidden"
                )
        except OSError:
            raise CognitiveProductionMigrationError(
                "migration_path_identity_unverifiable"
            ) from None
        if current.parent == current:
            break
        current = current.parent


def _reject_hardlink_alias(path: Path) -> None:
    if not path.is_file():
        return
    try:
        if path.stat().st_nlink != 1:
            raise CognitiveProductionMigrationError(
                "migration_path_hardlink_forbidden"
            )
    except OSError:
        raise CognitiveProductionMigrationError(
            "migration_path_identity_unverifiable"
        ) from None


def _validate_bound_database(paths: _BoundProductionPaths) -> None:
    _reject_reparse_chain(paths.database)
    if not paths.database.is_file():
        raise CognitiveProductionMigrationError(
            "production_operational_database_unavailable"
        )
    _reject_hardlink_alias(paths.database)
    if paths.production:
        expected = _fixed_production_paths()
        if not _same_path(paths.database, expected.database):
            raise CognitiveProductionMigrationError(
                "production_operational_database_mismatch"
            )
        try:
            if not is_production_operational_path(paths.database):
                raise CognitiveProductionMigrationError(
                    "production_operational_database_mismatch"
                )
            if paths.database.resolve(strict=True) != Path(
                DEFAULT_OPERATIONAL_STATE_PATH
            ).resolve(strict=True):
                raise CognitiveProductionMigrationError(
                    "production_operational_database_mismatch"
                )
        except OSError:
            raise CognitiveProductionMigrationError(
                "production_operational_database_unavailable"
            ) from None
    else:
        if is_production_operational_path(paths.database):
            raise CognitiveProductionMigrationError(
                "clone_test_path_resolves_to_production"
            )
        # The existing store enforces the system-temporary, alias-safe test root.
        OperationalStateStore(paths.database)


def _validate_bound_paths(paths: _BoundProductionPaths) -> None:
    _validate_bound_database(paths)
    if paths.production:
        expected = _fixed_production_paths()
        for actual, fixed in (
            (paths.plan_record, expected.plan_record),
            (paths.authority_record, expected.authority_record),
            (paths.backup_record, expected.backup_record),
            (paths.quiescence_record, expected.quiescence_record),
            (paths.control_manifest, expected.control_manifest),
            (paths.ledger, expected.ledger),
            (paths.lock, expected.lock),
        ):
            if not _same_path(actual, fixed):
                raise CognitiveProductionMigrationError(
                    "production_control_path_mismatch"
                )
    control = paths.authority_record.parent
    if not control.is_dir():
        raise CognitiveProductionMigrationError(
            "production_migration_control_root_unavailable"
        )
    _reject_reparse_chain(control)
    for path in (
        paths.plan_record,
        paths.authority_record,
        paths.backup_record,
        paths.quiescence_record,
        paths.control_manifest,
        paths.ledger,
        paths.lock,
    ):
        if path.parent != control:
            raise CognitiveProductionMigrationError(
                "production_control_path_mismatch"
            )
        if path.exists() or path.is_symlink():
            _reject_reparse_chain(path)
            _reject_hardlink_alias(path)


def _capture_bound_database_identity(database: Path) -> tuple[str, int, int]:
    """Bind one canonical database path to its current filesystem object."""

    try:
        _reject_reparse_chain(database)
        _reject_hardlink_alias(database)
        resolved = database.resolve(strict=True)
        details = resolved.stat()
    except CognitiveProductionMigrationError:
        raise
    except OSError:
        raise CognitiveProductionMigrationError(
            "production_database_identity_unverifiable",
            recovery_required=True,
        ) from None
    if not resolved.is_file() or not _same_path(resolved, database):
        raise CognitiveProductionMigrationError(
            "production_database_identity_changed",
            recovery_required=True,
        )
    return (_path_key(resolved), int(details.st_dev), int(details.st_ino))


def _assert_bound_database_identity(
    database: Path, expected: tuple[str, int, int]
) -> None:
    """Reject path/object replacement before publishing migration completion."""

    try:
        actual = _capture_bound_database_identity(database)
    except CognitiveProductionMigrationError:
        raise CognitiveProductionMigrationError(
            "production_database_identity_changed",
            recovery_required=True,
        ) from None
    if actual != expected:
        raise CognitiveProductionMigrationError(
            "production_database_identity_changed",
            recovery_required=True,
        )


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return size, digest.hexdigest()
    except OSError:
        raise CognitiveProductionMigrationError(
            "completion_publication_pin_read_failed",
            recovery_required=True,
        ) from None


def _open_windows_pinned_read(
    path: Path, *, allow_write_sharing: bool
) -> int:
    """Open a read pin while always denying delete/rename on Windows."""

    import ctypes
    from ctypes import wintypes
    import msvcrt

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | (0x00000002 if allow_write_sharing else 0),
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise CognitiveProductionMigrationError(
            "completion_publication_pin_unavailable",
            recovery_required=True,
        )
    try:
        handle_info = _windows_handle_info(int(handle))
        if (
            handle_info["attributes"] & 0x00000400
            or handle_info["links"] != 1
            or _path_key(_windows_final_handle_path(int(handle)))
            != _path_key(path.resolve(strict=True))
        ):
            close_handle(handle)
            raise CognitiveProductionMigrationError(
                "completion_publication_pin_path_changed",
                recovery_required=True,
            )
    except SecureControlFileError:
        close_handle(handle)
        raise CognitiveProductionMigrationError(
            "completion_publication_pin_unavailable",
            recovery_required=True,
        ) from None
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0),
        )
    except OSError:
        close_handle(handle)
        raise CognitiveProductionMigrationError(
            "completion_publication_pin_unavailable",
            recovery_required=True,
        ) from None


def _open_windows_share_read_only(path: Path) -> int:
    return _open_windows_pinned_read(path, allow_write_sharing=False)


def _open_windows_identity_pin(path: Path) -> int:
    # The live SQLite object must remain writable by the migration connection,
    # but delete/rename sharing stays denied for the complete transaction.
    return _open_windows_pinned_read(path, allow_write_sharing=True)


def _open_publication_pin(path: Path) -> _PublicationPin:
    try:
        _reject_reparse_chain(path)
        _reject_hardlink_alias(path)
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or not _same_path(resolved, path):
            raise CognitiveProductionMigrationError(
                "completion_publication_pin_path_changed",
                recovery_required=True,
            )
        if os.name == "nt":
            descriptor = _open_windows_share_read_only(resolved)
        else:
            descriptor = os.open(
                resolved,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
                raise CognitiveProductionMigrationError(
                    "completion_publication_pin_path_changed",
                    recovery_required=True,
                )
            try:
                # POSIX flock is advisory.  It is a best-effort compatibility
                # boundary; YunOS local production is Windows, where the
                # share-read-only handle above mandatorily denies mutation.
                import fcntl

                fcntl.flock(
                    descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except (ImportError, OSError):
                os.close(descriptor)
                raise CognitiveProductionMigrationError(
                    "completion_publication_pin_unavailable",
                    recovery_required=True,
                ) from None
        details = os.fstat(descriptor)
        path_details = resolved.stat()
        identity = (int(details.st_dev), int(details.st_ino))
        if (
            not stat.S_ISREG(details.st_mode)
            or int(details.st_nlink) != 1
            or identity
            != (int(path_details.st_dev), int(path_details.st_ino))
        ):
            raise CognitiveProductionMigrationError(
                "completion_publication_pin_path_changed",
                recovery_required=True,
            )
        size, digest = _hash_descriptor(descriptor)
        return _PublicationPin(
            path=resolved,
            descriptor=descriptor,
            canonical_path=_path_key(resolved),
            device=identity[0],
            inode=identity[1],
            size=size,
            sha256=digest,
        )
    except CognitiveProductionMigrationError:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except OSError:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise CognitiveProductionMigrationError(
            "completion_publication_pin_unavailable",
            recovery_required=True,
        ) from None


def _assert_publication_pin(pin: _PublicationPin) -> None:
    try:
        resolved = pin.path.resolve(strict=True)
        details = resolved.stat()
        descriptor_details = os.fstat(pin.descriptor)
    except OSError:
        raise CognitiveProductionMigrationError(
            "completion_publication_input_changed",
            recovery_required=True,
        ) from None
    if (
        _path_key(resolved) != pin.canonical_path
        or (int(details.st_dev), int(details.st_ino))
        != (pin.device, pin.inode)
        or (int(descriptor_details.st_dev), int(descriptor_details.st_ino))
        != (pin.device, pin.inode)
        or _hash_descriptor(pin.descriptor) != (pin.size, pin.sha256)
    ):
        raise CognitiveProductionMigrationError(
            "completion_publication_input_changed",
            recovery_required=True,
        )


def _open_identity_pin(path: Path) -> _PublicationPin:
    """Pin one regular-file identity while permitting expected byte changes."""

    try:
        _reject_reparse_chain(path)
        _reject_hardlink_alias(path)
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or not _same_path(resolved, path):
            raise CognitiveProductionMigrationError(
                "completion_publication_pin_path_changed",
                recovery_required=True,
            )
        descriptor = (
            _open_windows_identity_pin(resolved)
            if os.name == "nt"
            else os.open(
                resolved,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
        )
        details = os.fstat(descriptor)
        path_details = resolved.stat()
        identity = (int(details.st_dev), int(details.st_ino))
        if (
            not stat.S_ISREG(details.st_mode)
            or int(details.st_nlink) != 1
            or identity
            != (int(path_details.st_dev), int(path_details.st_ino))
        ):
            raise CognitiveProductionMigrationError(
                "completion_publication_pin_path_changed",
                recovery_required=True,
            )
        size, digest = _hash_descriptor(descriptor)
        return _PublicationPin(
            path=resolved,
            descriptor=descriptor,
            canonical_path=_path_key(resolved),
            device=identity[0],
            inode=identity[1],
            size=size,
            sha256=digest,
        )
    except CognitiveProductionMigrationError:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except OSError:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise CognitiveProductionMigrationError(
            "completion_publication_pin_unavailable",
            recovery_required=True,
        ) from None


def _assert_identity_pin(pin: _PublicationPin) -> None:
    try:
        resolved = pin.path.resolve(strict=True)
        details = resolved.stat()
        descriptor_details = os.fstat(pin.descriptor)
    except OSError:
        raise CognitiveProductionMigrationError(
            "completion_publication_input_changed",
            recovery_required=True,
        ) from None
    if (
        _path_key(resolved) != pin.canonical_path
        or (int(details.st_dev), int(details.st_ino))
        != (pin.device, pin.inode)
        or (int(descriptor_details.st_dev), int(descriptor_details.st_ino))
        != (pin.device, pin.inode)
        or not stat.S_ISREG(descriptor_details.st_mode)
        or int(descriptor_details.st_nlink) != 1
    ):
        raise CognitiveProductionMigrationError(
            "completion_publication_input_changed",
            recovery_required=True,
        )


@contextmanager
def _preclaim_recovery_pins(
    backup: Mapping[str, Any], database: Path
) -> Iterator[dict[str, _PublicationPin]]:
    """Hold recovery inputs and the live DB identity across the commit window."""

    package_root = Path(backup["preproduction_package_absolute_path"])
    selected = {
        "manifest": package_root / "release_manifest.json",
        "backup_database": package_root.joinpath(
            *PurePosixPath(backup["operational_backup_relative_path"]).parts
        ),
    }
    pins: dict[str, _PublicationPin] = {}
    try:
        for label, path in sorted(
            selected.items(), key=lambda item: _path_key(item[1])
        ):
            pins[label] = _open_publication_pin(path)
        pins["live_database"] = _open_identity_pin(database)
        yield pins
        for label, pin in pins.items():
            if label == "live_database":
                _assert_identity_pin(pin)
            else:
                _assert_publication_pin(pin)
    finally:
        for label, pin in reversed(tuple(pins.items())):
            try:
                if os.name != "nt" and label != "live_database":
                    try:
                        import fcntl

                        fcntl.flock(pin.descriptor, fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        pass
                os.close(pin.descriptor)
            except OSError:
                pass


@contextmanager
def _completion_publication_pins(
    backup: Mapping[str, Any], database: Path
) -> Iterator[dict[str, _PublicationPin]]:
    package_root = Path(backup["preproduction_package_absolute_path"])
    manifest = package_root / "release_manifest.json"
    backup_database = package_root.joinpath(
        *PurePosixPath(backup["operational_backup_relative_path"]).parts
    )
    selected = {
        "manifest": manifest,
        "backup_database": backup_database,
        "live_database": database,
    }
    pins: dict[str, _PublicationPin] = {}
    try:
        for label, path in sorted(
            selected.items(), key=lambda item: _path_key(item[1])
        ):
            pins[label] = _open_publication_pin(path)
        yield pins
        for pin in pins.values():
            _assert_publication_pin(pin)
    finally:
        for pin in reversed(tuple(pins.values())):
            try:
                if os.name != "nt":
                    try:
                        import fcntl

                        fcntl.flock(pin.descriptor, fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        pass
                os.close(pin.descriptor)
            except OSError:
                pass


def validate_fixed_production_operational_database() -> Path:
    """Read-only validation of the sole code-defined production target."""

    paths = _fixed_production_paths()
    _validate_bound_database(paths)
    return paths.database.resolve(strict=True)


def _read_canonical_document(path: Path, reason_code: str) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise CognitiveProductionMigrationError(reason_code)
        text = path.read_text(encoding="utf-8")
        value = json.loads(text)
    except CognitiveProductionMigrationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CognitiveProductionMigrationError(reason_code) from None
    if not isinstance(value, dict) or text.strip() != canonical_json(value):
        raise CognitiveProductionMigrationError(reason_code)
    return value


def _read_pinned_canonical_document(
    pinned: PinnedControlFile, reason_code: str
) -> dict[str, Any]:
    try:
        raw = pinned.read(1024 * 1024)
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(text)
    except (SecureControlFileError, UnicodeError, json.JSONDecodeError):
        raise CognitiveProductionMigrationError(reason_code) from None
    if not isinstance(value, dict) or text.strip() != canonical_json(value):
        raise CognitiveProductionMigrationError(reason_code)
    return value


@contextmanager
def _pinned_control_documents(
    paths: _BoundProductionPaths,
) -> Iterator[tuple[PinnedControlDirectory, dict[str, PinnedControlFile]]]:
    control = paths.authority_record.parent
    anchor = PROJECT_ROOT if paths.production else control.parent
    try:
        with pinned_control_directory(
            anchor=anchor,
            path=control,
            create_if_missing=False,
        ) as directory:
            expected = {
                "plan": paths.plan_record,
                "authority": paths.authority_record,
                "backup": paths.backup_record,
                "capture": paths.quiescence_record,
                "manifest": paths.control_manifest,
            }
            pins: dict[str, PinnedControlFile] = {}
            for label, path in expected.items():
                if path.parent != control:
                    raise CognitiveProductionMigrationError(
                        "production_control_path_mismatch"
                    )
                pins[label] = directory.open_file(path.name)
            if paths.lock.parent != control or paths.ledger.parent != control:
                raise CognitiveProductionMigrationError(
                    "production_control_path_mismatch"
                )
            # The fixed OS lock is acquired before any control bytes are read.
            # Its marker uses the migration identity; the validated authority
            # execution id is checked immediately after the pinned read.
            active_token = _ACTIVE_CONTROL_DIRECTORY_PIN.set(directory)
            try:
                with _execution_lock(
                    paths.lock,
                    COGNITIVE_PRODUCTION_MIGRATION_ID,
                    directory_pin=directory,
                ):
                    yield directory, pins
                    directory.verify()
            finally:
                _ACTIVE_CONTROL_DIRECTORY_PIN.reset(active_token)
    except SecureControlFileError:
        raise CognitiveProductionMigrationError(
            "production_control_input_identity_changed",
            recovery_required=True,
        ) from None


def canonical_control_document_bytes(document: Mapping[str, Any]) -> bytes:
    """Canonical bytes for operator-created fixed-path control documents."""

    return (canonical_json(dict(document)) + "\n").encode("utf-8")


def _typed_value(value: Any) -> bytes:
    if value is None:
        return b"n:"
    if isinstance(value, bytes):
        return b"b:" + hashlib.sha256(value).hexdigest().encode("ascii")
    if isinstance(value, str):
        return b"s:" + value.encode("utf-8")
    if isinstance(value, bool):
        return b"t:1" if value else b"t:0"
    if isinstance(value, int):
        return b"i:" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"f:" + value.hex().encode("ascii")
    raise CognitiveProductionMigrationError("unsupported_database_value")


def _core_schema_snapshot_digest(connection: sqlite3.Connection) -> str:
    rows = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": " ".join((row[3] or "").split()),
        }
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
        )
        if str(row[1]) not in _COGNITIVE_OBJECT_NAMES
    ]
    return canonical_digest(rows)


def _table_content_digest(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, Any]:
    cursor = connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"')
    column_names = [str(item[0]) for item in cursor.description or ()]
    row_digests: list[str] = []
    count = 0
    for row in cursor:
        digest = hashlib.sha256()
        for index, value in enumerate(row):
            if table == "operational_schema" and column_names[index] == "schema_digest":
                value = "<whole-schema-digest>"
            encoded = _typed_value(value)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        row_digests.append(digest.hexdigest())
        count += 1
    row_digests.sort()
    return {
        "rows": count,
        "digest": hashlib.sha256(
            "\n".join(row_digests).encode("ascii")
        ).hexdigest(),
    }


def _core_data_digest(connection: sqlite3.Connection) -> str:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        if str(row[0]) not in _COGNITIVE_OBJECT_NAMES
    ]
    return canonical_digest(
        {
            table: _table_content_digest(connection, table)
            for table in tables
        }
    )


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")


def _open_read_connection(path: Path, *, production: bool) -> sqlite3.Connection:
    if not production:
        # Preserve the existing clone-only path policy, then open read-only so
        # final verification can coexist with mandatory Windows publication
        # pins that deny every write-capable handle.
        OperationalStateStore(path)
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error:
        raise CognitiveProductionMigrationError(
            "production_operational_database_open_failed"
        ) from None
    _configure_connection(connection)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _open_write_connection(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=rw",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error:
        raise CognitiveProductionMigrationError(
            "production_operational_database_write_open_failed"
        ) from None
    _configure_connection(connection)
    return connection


def _assert_core_only(connection: sqlite3.Connection) -> None:
    OperationalStateStore._verify_schema(connection)
    existing = _existing_cognitive_objects(connection)
    if existing:
        if existing == _COGNITIVE_OBJECTS:
            raise CognitiveProductionMigrationError(
                "cognitive_production_migration_already_applied"
            )
        raise CognitiveProductionMigrationError(
            "cognitive_partial_schema_detected",
            recovery_required=True,
        )
    lease_count = connection.execute(
        "SELECT COUNT(*) FROM state_writer_leases"
    ).fetchone()[0]
    if lease_count != 0:
        raise CognitiveProductionMigrationError(
            "cognitive_migration_active_writer_lease"
        )


def _expected_target_schema_digest(
    source: sqlite3.Connection,
) -> str:
    memory = sqlite3.connect(":memory:")
    try:
        _configure_connection(memory)
        source.backup(memory)
        for statement in _COGNITIVE_DDL:
            memory.execute(statement)
        digest = schema_fingerprint(memory)
        memory.execute(
            "UPDATE operational_schema SET schema_digest=? WHERE singleton=1",
            (digest,),
        )
        memory.execute(
            "INSERT INTO cognitive_schema VALUES(?,?,?,?,?,?,?)",
            (
                1,
                COGNITIVE_SCHEMA_MARKER,
                COGNITIVE_SCHEMA_GENERATION,
                digest,
                COGNITIVE_CONTRACT_VERSION,
                "2000-01-01T00:00:00.000000Z",
                0,
            ),
        )
        _verify_cognitive_connection(memory)
        return digest
    finally:
        memory.close()


def _validate_ddl_boundary() -> None:
    if hashlib.sha256(COGNITIVE_SCHEMA_RESOURCE.read_bytes()).hexdigest() != COGNITIVE_DDL_DIGEST:
        raise CognitiveProductionMigrationError("cognitive_ddl_digest_drift")
    for statement in _COGNITIVE_DDL:
        without_comments = re.sub(
            r"\A(?:\s|--[^\r\n]*(?:\r?\n|$)|/\*.*?\*/)*",
            "",
            statement,
            flags=re.DOTALL,
        )
        if re.match(r"CREATE\s+(TABLE|INDEX|TRIGGER)\b", without_comments, re.I) is None:
            raise CognitiveProductionMigrationError(
                "cognitive_ddl_not_additive"
            )
        # UPDATE/DELETE tokens are expected inside append-only guard trigger
        # definitions.  The statement itself must still be a CREATE and the
        # exact resource/object allowlist is already digest-bound.
        if re.search(
            r"\b(DROP|ALTER|ATTACH|DETACH|VACUUM)\b",
            without_comments,
            re.I,
        ):
            raise CognitiveProductionMigrationError(
                "cognitive_ddl_not_additive"
            )


def _build_plan(
    database: Path,
    *,
    production: bool,
) -> dict[str, Any]:
    _validate_ddl_boundary()
    gate = _file_gate(database)
    wal = _wal_gate(database)
    if not gate["exists"] or gate["size"] < 1:
        raise CognitiveProductionMigrationError(
            "production_operational_database_unavailable"
        )
    if wal["exists"] and wal["size"] != 0:
        raise CognitiveProductionMigrationError(
            "production_operational_wal_not_quiescent"
        )
    with closing(
        _open_read_connection(database, production=production)
    ) as connection:
        _assert_core_only(connection)
        marker = connection.execute(
            "SELECT marker,schema_version,schema_digest,writer_default_enabled "
            "FROM operational_schema WHERE singleton=1"
        ).fetchone()
        source_schema = schema_fingerprint(connection)
        target_schema = _expected_target_schema_digest(connection)
        plan = {
            "plan_schema_version": COGNITIVE_PRODUCTION_PLAN_VERSION,
            "migration_id": COGNITIVE_PRODUCTION_MIGRATION_ID,
            "production_database_relative_path": _PRODUCTION_RELATIVE_PATH,
            "core_marker": SCHEMA_MARKER,
            "core_schema_version": SCHEMA_VERSION,
            "target_extension_marker": COGNITIVE_SCHEMA_MARKER,
            "target_schema_generation": COGNITIVE_SCHEMA_GENERATION,
            "ddl_digest": COGNITIVE_DDL_DIGEST,
            "source_database_sha256": gate["sha256"],
            "source_database_size": gate["size"],
            "source_schema_digest": source_schema,
            "target_schema_digest": target_schema,
            "core_schema_snapshot_digest": _core_schema_snapshot_digest(
                connection
            ),
            "core_data_digest": _core_data_digest(connection),
            "expected_cognitive_object_count": len(_COGNITIVE_OBJECTS),
            "writer_default_enabled": False,
            "active_writer_lease_count": 0,
            "additive_only": True,
            "destructive_ddl_allowed": False,
            "rollback_policy": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
        }
        if (
            marker is None
            or marker["marker"] != SCHEMA_MARKER
            or marker["schema_version"] != SCHEMA_VERSION
            or marker["schema_digest"] != source_schema
            or marker["writer_default_enabled"] != 0
        ):
            raise CognitiveProductionMigrationError(
                "operational_schema_marker_mismatch"
            )
    final_gate = _file_gate(database)
    final_wal = _wal_gate(database)
    if final_gate != gate or final_wal != wal:
        raise CognitiveProductionMigrationError(
            "production_database_changed_during_plan"
        )
    plan["plan_digest"] = canonical_digest(plan)
    return plan


def build_cognitive_migration_plan_for_clone(
    path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Build a read-only plan only for an alias-safe disposable clone."""

    target = Path(path).expanduser().absolute()
    if is_production_operational_path(target):
        raise CognitiveProductionMigrationError(
            "cognitive_clone_plan_production_forbidden"
        )
    # Construction validates that the target is in the system temp boundary.
    OperationalStateStore(target)
    return _build_plan(target, production=False)


def inspect_fixed_production_cognitive_migration_plan() -> dict[str, Any]:
    """Build the authority-bound plan from the sole production DB, read-only."""

    paths = _fixed_production_paths()
    _validate_bound_database(paths)
    return _build_plan(paths.database, production=True)


def _validate_recorded_package_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise CognitiveProductionMigrationError(
            "preproduction_package_path_invalid"
        )
    selected = Path(value)
    if not selected.is_absolute() or str(selected.absolute()) != value:
        raise CognitiveProductionMigrationError(
            "preproduction_package_path_invalid"
        )
    return value


def _validate_expected_file_gate(
    value: Any, reason_code: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CognitiveProductionMigrationError(reason_code)
    gate = dict(value)
    if (
        set(gate) != {"exists", "size", "sha256"}
        or gate["exists"] is not True
        or type(gate["size"]) is not int
        or gate["size"] < 1
    ):
        raise CognitiveProductionMigrationError(reason_code)
    _sha(gate["sha256"], reason_code)
    return gate


def build_cognitive_migration_backup_verification(
    *,
    plan: Mapping[str, Any],
    backup_manifest_digest: str,
    backup_set_digest: str,
    backup_database_sha256: str,
    backup_database_size: int,
    preproduction_package_absolute_path: str,
    operational_backup_relative_path: str,
    release_manifest_file_gate: Mapping[str, Any],
    service_quiescence_digest: str,
    service_quiescence_captured_at: str,
    capture_artifact_digest: str,
    capture_record_digest: str,
    capture_issuer_digest: str,
    capture_nonce: str,
    capture_source_identities_digest: str,
    capture_relative_path: str,
    verified_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """Build canonical verified-backup evidence; it grants no authority."""

    validated_plan = _validate_plan_document(plan)
    verified = _timestamp(verified_at, "backup_verified_at_invalid")
    expiry = _timestamp(expires_at, "backup_expiry_invalid")
    if (
        expiry <= verified
        or (expiry - verified).total_seconds()
        > COGNITIVE_PRODUCTION_AUTHORITY_TTL_SECONDS
    ):
        raise CognitiveProductionMigrationError("backup_expiry_invalid")
    if type(backup_database_size) is not int or backup_database_size < 1:
        raise CognitiveProductionMigrationError("backup_database_size_invalid")
    manifest_gate = _validate_expected_file_gate(
        release_manifest_file_gate, "release_manifest_file_gate_invalid"
    )
    captured = _timestamp(
        service_quiescence_captured_at,
        "service_quiescence_evidence_invalid",
    )
    if captured > verified:
        raise CognitiveProductionMigrationError(
            "service_quiescence_authority_mismatch"
        )
    if (
        not isinstance(capture_nonce, str)
        or _CAPTURE_NONCE_RE.fullmatch(capture_nonce) is None
    ):
        raise CognitiveProductionMigrationError("capture_artifact_nonce_invalid")
    if capture_relative_path != _capture_relative_path(
        service_quiescence_captured_at
    ):
        raise CognitiveProductionMigrationError("capture_artifact_path_invalid")
    document = {
        "backup_schema_version": COGNITIVE_PRODUCTION_BACKUP_VERSION,
        "migration_id": COGNITIVE_PRODUCTION_MIGRATION_ID,
        "production_database_relative_path": _PRODUCTION_RELATIVE_PATH,
        "source_database_sha256": validated_plan["source_database_sha256"],
        "source_database_size": validated_plan["source_database_size"],
        "source_schema_digest": validated_plan["source_schema_digest"],
        "core_schema_snapshot_digest": validated_plan[
            "core_schema_snapshot_digest"
        ],
        "core_data_digest": validated_plan["core_data_digest"],
        "backup_database_sha256": _sha(
            backup_database_sha256, "backup_database_sha256_invalid"
        ),
        "backup_database_size": backup_database_size,
        "backup_database_schema_digest": validated_plan[
            "source_schema_digest"
        ],
        "preproduction_package_absolute_path": _validate_recorded_package_path(
            preproduction_package_absolute_path
        ),
        "operational_backup_relative_path": _safe_package_relative_path(
            operational_backup_relative_path
        ),
        "release_manifest_file_gate": manifest_gate,
        "backup_integrity_check": "ok",
        "backup_foreign_key_violations": 0,
        "restore_rehearsal_passed": True,
        "services_quiesced": True,
        "active_writer_lease_count": 0,
        "backup_manifest_digest": _sha(
            backup_manifest_digest, "backup_manifest_digest_invalid"
        ),
        "backup_set_digest": _sha(
            backup_set_digest, "backup_set_digest_invalid"
        ),
        "service_quiescence_digest": _sha(
            service_quiescence_digest,
            "service_quiescence_digest_invalid",
        ),
        "service_quiescence_captured_at": service_quiescence_captured_at,
        "capture_artifact_digest": _sha(
            capture_artifact_digest, "capture_artifact_digest_invalid"
        ),
        "capture_record_digest": _sha(
            capture_record_digest, "capture_record_digest_invalid"
        ),
        "capture_issuer_digest": _sha(
            capture_issuer_digest, "capture_issuer_digest_invalid"
        ),
        "capture_nonce": capture_nonce,
        "capture_source_identities_digest": _sha(
            capture_source_identities_digest,
            "capture_source_identities_digest_invalid",
        ),
        "capture_relative_path": capture_relative_path,
        "verified_at": verified_at,
        "expires_at": expires_at,
        "rollback_policy": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
    }
    document["record_digest"] = canonical_digest(document)
    return document


def build_cognitive_production_migration_authority(
    *,
    plan: Mapping[str, Any],
    backup_record: Mapping[str, Any],
    authority_id: str,
    one_time_execution_id: str,
    issued_by: str,
    code_commit: str,
    user_authorization_text_digest: str,
    authorized_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """Build a strict one-shot record from explicit non-agent authorization."""

    validated_plan = _validate_plan_document(plan)
    validated_backup = _validate_backup_document(
        backup_record,
        plan=validated_plan,
        now=_timestamp(authorized_at, "authorization_time_invalid"),
        allow_future_verification=False,
    )
    authorized = _timestamp(authorized_at, "authorization_time_invalid")
    expiry = _timestamp(expires_at, "authorization_expiry_invalid")
    if (
        expiry <= authorized
        or (expiry - authorized).total_seconds()
        > COGNITIVE_PRODUCTION_AUTHORITY_TTL_SECONDS
    ):
        raise CognitiveProductionMigrationError(
            "authorization_expiry_invalid"
        )
    captured = _timestamp(
        validated_backup["service_quiescence_captured_at"],
        "service_quiescence_evidence_invalid",
    )
    if (
        captured > authorized
        or (authorized - captured).total_seconds()
        > COGNITIVE_PRODUCTION_QUIESCENCE_TTL_SECONDS
    ):
        raise CognitiveProductionMigrationError(
            "service_quiescence_authority_mismatch"
        )
    if authority_id != COGNITIVE_PRODUCTION_AUTHORITY_ID:
        raise CognitiveProductionMigrationError("authority_id_invalid")
    if issued_by != COGNITIVE_PRODUCTION_AUTHORITY_ISSUER:
        raise CognitiveProductionMigrationError("authority_issuer_invalid")
    if (
        user_authorization_text_digest
        != COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256
    ):
        raise CognitiveProductionMigrationError(
            "user_authorization_text_digest_invalid"
        )
    expected_execution_id = cognitive_production_execution_id(
        plan_digest=validated_plan["plan_digest"],
        backup_set_digest=validated_backup["backup_set_digest"],
        code_commit=code_commit,
    )
    if one_time_execution_id != expected_execution_id:
        raise CognitiveProductionMigrationError("one_time_execution_id_invalid")
    document = {
        "authority_schema_version": COGNITIVE_PRODUCTION_AUTHORITY_VERSION,
        "authority_id": authority_id,
        "one_time_execution_id": one_time_execution_id,
        "issued_by": issued_by,
        "migration_id": COGNITIVE_PRODUCTION_MIGRATION_ID,
        "production_database_relative_path": _PRODUCTION_RELATIVE_PATH,
        "core_schema_version": SCHEMA_VERSION,
        "target_schema_generation": COGNITIVE_SCHEMA_GENERATION,
        "ddl_digest": COGNITIVE_DDL_DIGEST,
        "source_database_sha256": validated_plan["source_database_sha256"],
        "source_database_size": validated_plan["source_database_size"],
        "source_schema_digest": validated_plan["source_schema_digest"],
        "target_schema_digest": validated_plan["target_schema_digest"],
        "core_schema_snapshot_digest": validated_plan[
            "core_schema_snapshot_digest"
        ],
        "core_data_digest": validated_plan["core_data_digest"],
        "plan_digest": validated_plan["plan_digest"],
        "backup_record_digest": validated_backup["record_digest"],
        "backup_manifest_digest": validated_backup[
            "backup_manifest_digest"
        ],
        "backup_set_digest": validated_backup["backup_set_digest"],
        "service_quiescence_digest": validated_backup[
            "service_quiescence_digest"
        ],
        "service_quiescence_captured_at": validated_backup[
            "service_quiescence_captured_at"
        ],
        "capture_artifact_digest": validated_backup[
            "capture_artifact_digest"
        ],
        "capture_record_digest": validated_backup["capture_record_digest"],
        "capture_issuer_digest": validated_backup["capture_issuer_digest"],
        "capture_nonce": validated_backup["capture_nonce"],
        "capture_source_identities_digest": validated_backup[
            "capture_source_identities_digest"
        ],
        "capture_relative_path": validated_backup["capture_relative_path"],
        "code_commit": _commit(code_commit, "code_commit_invalid"),
        "user_authorization_text_digest": _sha(
            user_authorization_text_digest,
            "user_authorization_text_digest_invalid",
        ),
        "authorized_at": authorized_at,
        "expires_at": expires_at,
        "permissions": dict(_AUTHORITY_PERMISSIONS),
    }
    document["authority_digest"] = canonical_digest(document)
    return document


def _validate_plan_document(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CognitiveProductionMigrationError("migration_plan_invalid")
    plan = dict(value)
    expected_fields = {
        "plan_schema_version",
        "migration_id",
        "production_database_relative_path",
        "core_marker",
        "core_schema_version",
        "target_extension_marker",
        "target_schema_generation",
        "ddl_digest",
        "source_database_sha256",
        "source_database_size",
        "source_schema_digest",
        "target_schema_digest",
        "core_schema_snapshot_digest",
        "core_data_digest",
        "expected_cognitive_object_count",
        "writer_default_enabled",
        "active_writer_lease_count",
        "additive_only",
        "destructive_ddl_allowed",
        "rollback_policy",
        "plan_digest",
    }
    if set(plan) != expected_fields:
        raise CognitiveProductionMigrationError("migration_plan_invalid")
    digest = plan.pop("plan_digest")
    if digest != canonical_digest(plan):
        raise CognitiveProductionMigrationError("migration_plan_digest_mismatch")
    plan["plan_digest"] = digest
    for field in (
        "ddl_digest",
        "source_database_sha256",
        "source_schema_digest",
        "target_schema_digest",
        "core_schema_snapshot_digest",
        "core_data_digest",
        "plan_digest",
    ):
        _sha(plan[field], "migration_plan_invalid")
    if (
        plan["plan_schema_version"] != COGNITIVE_PRODUCTION_PLAN_VERSION
        or plan["migration_id"] != COGNITIVE_PRODUCTION_MIGRATION_ID
        or plan["production_database_relative_path"] != _PRODUCTION_RELATIVE_PATH
        or plan["core_marker"] != SCHEMA_MARKER
        or plan["core_schema_version"] != SCHEMA_VERSION
        or plan["target_extension_marker"] != COGNITIVE_SCHEMA_MARKER
        or plan["target_schema_generation"] != COGNITIVE_SCHEMA_GENERATION
        or plan["ddl_digest"] != COGNITIVE_DDL_DIGEST
        or type(plan["source_database_size"]) is not int
        or plan["source_database_size"] < 1
        or plan["expected_cognitive_object_count"] != len(_COGNITIVE_OBJECTS)
        or plan["writer_default_enabled"] is not False
        or plan["active_writer_lease_count"] != 0
        or plan["additive_only"] is not True
        or plan["destructive_ddl_allowed"] is not False
        or plan["rollback_policy"] != COGNITIVE_PRODUCTION_ROLLBACK_POLICY
    ):
        raise CognitiveProductionMigrationError("migration_plan_invalid")
    return plan


def _validate_backup_document(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any] | None,
    now: datetime,
    allow_future_verification: bool = False,
    require_current: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CognitiveProductionMigrationError("backup_verification_invalid")
    record = dict(value)
    expected_fields = {
        "backup_schema_version",
        "migration_id",
        "production_database_relative_path",
        "source_database_sha256",
        "source_database_size",
        "source_schema_digest",
        "core_schema_snapshot_digest",
        "core_data_digest",
        "backup_database_sha256",
        "backup_database_size",
        "backup_database_schema_digest",
        "preproduction_package_absolute_path",
        "operational_backup_relative_path",
        "release_manifest_file_gate",
        "backup_integrity_check",
        "backup_foreign_key_violations",
        "restore_rehearsal_passed",
        "services_quiesced",
        "active_writer_lease_count",
        "backup_manifest_digest",
        "backup_set_digest",
        "service_quiescence_digest",
        "service_quiescence_captured_at",
        "capture_artifact_digest",
        "capture_record_digest",
        "capture_issuer_digest",
        "capture_nonce",
        "capture_source_identities_digest",
        "capture_relative_path",
        "verified_at",
        "expires_at",
        "rollback_policy",
        "record_digest",
    }
    if set(record) != expected_fields:
        raise CognitiveProductionMigrationError("backup_verification_invalid")
    digest = record.pop("record_digest")
    if digest != canonical_digest(record):
        raise CognitiveProductionMigrationError(
            "backup_verification_digest_mismatch"
        )
    record["record_digest"] = digest
    for field in (
        "source_database_sha256",
        "source_schema_digest",
        "core_schema_snapshot_digest",
        "core_data_digest",
        "backup_database_sha256",
        "backup_database_schema_digest",
        "backup_manifest_digest",
        "backup_set_digest",
        "service_quiescence_digest",
        "capture_artifact_digest",
        "capture_record_digest",
        "capture_issuer_digest",
        "capture_source_identities_digest",
        "record_digest",
    ):
        _sha(record[field], "backup_verification_invalid")
    verified = _timestamp(record["verified_at"], "backup_verified_at_invalid")
    captured = _timestamp(
        record["service_quiescence_captured_at"],
        "service_quiescence_evidence_invalid",
    )
    expiry = _timestamp(record["expires_at"], "backup_expiry_invalid")
    if (
        expiry <= verified
        or (expiry - verified).total_seconds()
        > COGNITIVE_PRODUCTION_AUTHORITY_TTL_SECONDS
        or (require_current and expiry <= now)
    ):
        raise CognitiveProductionMigrationError("backup_verification_expired")
    if not allow_future_verification and verified > now:
        raise CognitiveProductionMigrationError(
            "backup_verification_not_yet_valid"
        )
    _validate_recorded_package_path(
        record["preproduction_package_absolute_path"]
    )
    _safe_package_relative_path(record["operational_backup_relative_path"])
    _validate_expected_file_gate(
        record["release_manifest_file_gate"],
        "release_manifest_file_gate_invalid",
    )
    if captured > verified:
        raise CognitiveProductionMigrationError(
            "service_quiescence_authority_mismatch"
        )
    if (
        not isinstance(record["capture_nonce"], str)
        or _CAPTURE_NONCE_RE.fullmatch(record["capture_nonce"]) is None
        or record["capture_relative_path"]
        != _capture_relative_path(record["service_quiescence_captured_at"])
    ):
        raise CognitiveProductionMigrationError("backup_verification_invalid")
    if (
        record["backup_schema_version"] != COGNITIVE_PRODUCTION_BACKUP_VERSION
        or record["migration_id"] != COGNITIVE_PRODUCTION_MIGRATION_ID
        or record["production_database_relative_path"] != _PRODUCTION_RELATIVE_PATH
        or type(record["source_database_size"]) is not int
        or record["source_database_size"] < 1
        or type(record["backup_database_size"]) is not int
        or record["backup_database_size"] < 1
        or record["backup_integrity_check"] != "ok"
        or record["backup_foreign_key_violations"] != 0
        or record["restore_rehearsal_passed"] is not True
        or record["services_quiesced"] is not True
        or record["active_writer_lease_count"] != 0
        or record["rollback_policy"] != COGNITIVE_PRODUCTION_ROLLBACK_POLICY
        or record["backup_database_schema_digest"]
        != record["source_schema_digest"]
    ):
        raise CognitiveProductionMigrationError("backup_verification_invalid")
    if plan is not None:
        for field in (
            "source_database_sha256",
            "source_database_size",
            "source_schema_digest",
            "core_schema_snapshot_digest",
            "core_data_digest",
        ):
            if record[field] != plan[field]:
                raise CognitiveProductionMigrationError(
                    "backup_verification_plan_mismatch"
                )
    return record


def _validate_authority_document(
    value: Mapping[str, Any],
    *,
    now: datetime,
    require_current: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CognitiveProductionMigrationError("migration_authority_invalid")
    authority = dict(value)
    expected_fields = {
        "authority_schema_version",
        "authority_id",
        "one_time_execution_id",
        "issued_by",
        "migration_id",
        "production_database_relative_path",
        "core_schema_version",
        "target_schema_generation",
        "ddl_digest",
        "source_database_sha256",
        "source_database_size",
        "source_schema_digest",
        "target_schema_digest",
        "core_schema_snapshot_digest",
        "core_data_digest",
        "plan_digest",
        "backup_record_digest",
        "backup_manifest_digest",
        "backup_set_digest",
        "service_quiescence_digest",
        "service_quiescence_captured_at",
        "capture_artifact_digest",
        "capture_record_digest",
        "capture_issuer_digest",
        "capture_nonce",
        "capture_source_identities_digest",
        "capture_relative_path",
        "code_commit",
        "user_authorization_text_digest",
        "authorized_at",
        "expires_at",
        "permissions",
        "authority_digest",
    }
    if set(authority) != expected_fields:
        raise CognitiveProductionMigrationError("migration_authority_invalid")
    digest = authority.pop("authority_digest")
    if digest != canonical_digest(authority):
        raise CognitiveProductionMigrationError(
            "migration_authority_digest_mismatch"
        )
    authority["authority_digest"] = digest
    for field in (
        "ddl_digest",
        "source_database_sha256",
        "source_schema_digest",
        "target_schema_digest",
        "core_schema_snapshot_digest",
        "core_data_digest",
        "plan_digest",
        "backup_record_digest",
        "backup_manifest_digest",
        "backup_set_digest",
        "service_quiescence_digest",
        "capture_artifact_digest",
        "capture_record_digest",
        "capture_issuer_digest",
        "capture_source_identities_digest",
        "user_authorization_text_digest",
        "authority_digest",
    ):
        _sha(authority[field], "migration_authority_invalid")
    if authority["authority_id"] != COGNITIVE_PRODUCTION_AUTHORITY_ID:
        raise CognitiveProductionMigrationError("authority_id_invalid")
    if authority["issued_by"] != COGNITIVE_PRODUCTION_AUTHORITY_ISSUER:
        raise CognitiveProductionMigrationError("authority_issuer_invalid")
    if (
        authority["user_authorization_text_digest"]
        != COGNITIVE_PRODUCTION_USER_AUTHORIZATION_TEXT_SHA256
    ):
        raise CognitiveProductionMigrationError(
            "user_authorization_text_digest_invalid"
        )
    authorized = _timestamp(
        authority["authorized_at"], "authorization_time_invalid"
    )
    captured = _timestamp(
        authority["service_quiescence_captured_at"],
        "service_quiescence_evidence_invalid",
    )
    expiry = _timestamp(authority["expires_at"], "authorization_expiry_invalid")
    if authorized > now:
        raise CognitiveProductionMigrationError("authorization_not_yet_valid")
    if (
        expiry <= authorized
        or (expiry - authorized).total_seconds()
        > COGNITIVE_PRODUCTION_AUTHORITY_TTL_SECONDS
        or (require_current and expiry <= now)
    ):
        raise CognitiveProductionMigrationError("authorization_expired")
    if (
        captured > authorized
        or (authorized - captured).total_seconds()
        > COGNITIVE_PRODUCTION_QUIESCENCE_TTL_SECONDS
    ):
        raise CognitiveProductionMigrationError(
            "service_quiescence_authority_mismatch"
        )
    if (
        authority["authority_schema_version"]
        != COGNITIVE_PRODUCTION_AUTHORITY_VERSION
        or authority["migration_id"] != COGNITIVE_PRODUCTION_MIGRATION_ID
        or authority["production_database_relative_path"]
        != _PRODUCTION_RELATIVE_PATH
        or authority["core_schema_version"] != SCHEMA_VERSION
        or authority["target_schema_generation"]
        != COGNITIVE_SCHEMA_GENERATION
        or authority["ddl_digest"] != COGNITIVE_DDL_DIGEST
        or type(authority["source_database_size"]) is not int
        or authority["source_database_size"] < 1
        or authority["permissions"] != _AUTHORITY_PERMISSIONS
        or authority["one_time_execution_id"]
        != cognitive_production_execution_id(
            plan_digest=authority["plan_digest"],
            backup_set_digest=authority["backup_set_digest"],
            code_commit=authority["code_commit"],
        )
        or not isinstance(authority["capture_nonce"], str)
        or _CAPTURE_NONCE_RE.fullmatch(authority["capture_nonce"]) is None
        or authority["capture_relative_path"]
        != _capture_relative_path(authority["service_quiescence_captured_at"])
    ):
        raise CognitiveProductionMigrationError("migration_authority_invalid")
    _commit(authority["code_commit"], "code_commit_invalid")
    return authority


def _validate_bound_documents(
    *,
    plan: Mapping[str, Any],
    authority: Mapping[str, Any],
    backup: Mapping[str, Any],
    code_state: Mapping[str, Any] | None,
) -> None:
    bindings = (
        (authority["plan_digest"], plan["plan_digest"]),
        (authority["source_database_sha256"], plan["source_database_sha256"]),
        (authority["source_database_size"], plan["source_database_size"]),
        (authority["source_schema_digest"], plan["source_schema_digest"]),
        (authority["target_schema_digest"], plan["target_schema_digest"]),
        (
            authority["core_schema_snapshot_digest"],
            plan["core_schema_snapshot_digest"],
        ),
        (authority["core_data_digest"], plan["core_data_digest"]),
        (authority["backup_record_digest"], backup["record_digest"]),
        (
            authority["backup_manifest_digest"],
            backup["backup_manifest_digest"],
        ),
        (authority["backup_set_digest"], backup["backup_set_digest"]),
        (
            authority["service_quiescence_digest"],
            backup["service_quiescence_digest"],
        ),
        (
            authority["service_quiescence_captured_at"],
            backup["service_quiescence_captured_at"],
        ),
        (
            authority["capture_artifact_digest"],
            backup["capture_artifact_digest"],
        ),
        (
            authority["capture_record_digest"],
            backup["capture_record_digest"],
        ),
        (
            authority["capture_issuer_digest"],
            backup["capture_issuer_digest"],
        ),
        (authority["capture_nonce"], backup["capture_nonce"]),
        (
            authority["capture_source_identities_digest"],
            backup["capture_source_identities_digest"],
        ),
        (
            authority["capture_relative_path"],
            backup["capture_relative_path"],
        ),
    )
    if any(actual != expected for actual, expected in bindings):
        raise CognitiveProductionMigrationError(
            "production_migration_binding_mismatch"
        )
    if code_state is None:
        return
    if authority["code_commit"] != code_state.get("commit"):
        raise CognitiveProductionMigrationError(
            "production_migration_binding_mismatch"
        )
    if code_state.get("tracked_worktree_clean") is not True:
        raise CognitiveProductionMigrationError("tracked_worktree_dirty")
    if code_state.get("staging_empty") is not True:
        raise CognitiveProductionMigrationError("staging_not_empty")
    if code_state.get("untracked_empty") is not True:
        raise CognitiveProductionMigrationError("untracked_worktree_not_empty")
    if code_state.get("worktree_clean") is not True:
        raise CognitiveProductionMigrationError("worktree_not_clean")


def _validate_control_manifest_document(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    backup: Mapping[str, Any],
    authority: Mapping[str, Any],
    capture: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the atomic five-document preparation envelope."""

    if not isinstance(value, Mapping):
        raise CognitiveProductionMigrationError("control_manifest_invalid")
    manifest = dict(value)
    expected_fields = {
        "version",
        "prepared_at",
        "expires_at",
        "code_commit",
        "plan_digest",
        "backup_record_digest",
        "authority_digest",
        "service_quiescence_digest",
        "service_quiescence_captured_at",
        "capture_artifact_digest",
        "capture_record_digest",
        "capture_issuer_digest",
        "capture_nonce",
        "capture_source_identities_digest",
        "capture_relative_path",
        "preproduction_package_absolute_path",
        "operational_backup_relative_path",
        "release_manifest_file_gate",
        "preproduction_manifest_digest",
        "backup_set_digest",
        "source_provenance_digest",
        "source_provenance_verification_digest",
        "document_sha256",
        "files",
        "migration_executed",
        "manifest_digest",
    }
    if set(manifest) != expected_fields:
        raise CognitiveProductionMigrationError("control_manifest_invalid")
    digest = manifest.pop("manifest_digest")
    if digest != canonical_digest(manifest):
        raise CognitiveProductionMigrationError(
            "control_manifest_digest_mismatch"
        )
    manifest["manifest_digest"] = digest
    artifact = validate_cognitive_quiescence_capture_artifact(capture)
    evidence = artifact["evidence"]
    issuer = artifact["issuer"]
    documents = {
        "cognitive_migration_plan.json": plan,
        "cognitive_migration_backup_verification.json": backup,
        "cognitive_migration_authority.json": authority,
        "service_quiescence_evidence.json": artifact,
    }
    expected_document_sha = {
        name: hashlib.sha256(canonical_control_document_bytes(document)).hexdigest()
        for name, document in documents.items()
    }
    bindings = (
        (manifest["version"], COGNITIVE_CONTROL_PREPARATION_VERSION),
        (manifest["prepared_at"], authority["authorized_at"]),
        (manifest["expires_at"], authority["expires_at"]),
        (manifest["expires_at"], backup["expires_at"]),
        (manifest["code_commit"], authority["code_commit"]),
        (manifest["plan_digest"], plan["plan_digest"]),
        (manifest["backup_record_digest"], backup["record_digest"]),
        (manifest["authority_digest"], authority["authority_digest"]),
        (
            manifest["service_quiescence_digest"],
            evidence["evidence_digest"],
        ),
        (
            manifest["service_quiescence_captured_at"],
            evidence["captured_at"],
        ),
        (manifest["capture_artifact_digest"], artifact["artifact_digest"]),
        (manifest["capture_record_digest"], artifact["record_digest"]),
        (manifest["capture_issuer_digest"], issuer["issuer_digest"]),
        (manifest["capture_nonce"], issuer["nonce"]),
        (
            manifest["capture_source_identities_digest"],
            issuer["source_identities_digest"],
        ),
        (manifest["capture_relative_path"], issuer["capture_relative_path"]),
        (
            manifest["preproduction_package_absolute_path"],
            backup["preproduction_package_absolute_path"],
        ),
        (
            manifest["operational_backup_relative_path"],
            backup["operational_backup_relative_path"],
        ),
        (
            manifest["release_manifest_file_gate"],
            backup["release_manifest_file_gate"],
        ),
        (
            manifest["preproduction_manifest_digest"],
            backup["backup_manifest_digest"],
        ),
        (manifest["backup_set_digest"], backup["backup_set_digest"]),
        (manifest["document_sha256"], expected_document_sha),
        (manifest["files"], sorted(_CONTROL_DOCUMENT_FILENAMES)),
        (manifest["migration_executed"], False),
    )
    if any(actual != expected for actual, expected in bindings):
        raise CognitiveProductionMigrationError(
            "control_manifest_binding_mismatch"
        )
    for field in (
        "manifest_digest",
        "plan_digest",
        "backup_record_digest",
        "authority_digest",
        "service_quiescence_digest",
        "capture_artifact_digest",
        "capture_record_digest",
        "capture_issuer_digest",
        "capture_source_identities_digest",
        "preproduction_manifest_digest",
        "backup_set_digest",
    ):
        _sha(manifest[field], "control_manifest_invalid")
    for field in (
        "source_provenance_digest",
        "source_provenance_verification_digest",
    ):
        if manifest[field] is not None:
            _sha(manifest[field], "control_manifest_invalid")
    return manifest


def _validate_capture_bindings(
    *,
    capture: Mapping[str, Any],
    backup: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact = validate_cognitive_quiescence_capture_artifact(capture)
    evidence = artifact["evidence"]
    issuer = artifact["issuer"]
    expected = {
        "capture_artifact_digest": artifact["artifact_digest"],
        "capture_record_digest": artifact["record_digest"],
        "capture_issuer_digest": issuer["issuer_digest"],
        "capture_nonce": issuer["nonce"],
        "capture_source_identities_digest": issuer[
            "source_identities_digest"
        ],
        "capture_relative_path": issuer["capture_relative_path"],
        "service_quiescence_digest": evidence["evidence_digest"],
        "service_quiescence_captured_at": evidence["captured_at"],
    }
    if any(
        backup.get(field) != wanted or authority.get(field) != wanted
        for field, wanted in expected.items()
    ):
        raise CognitiveProductionMigrationError(
            "service_quiescence_binding_mismatch"
        )
    return artifact, evidence


def _load_release_manifest(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise CognitiveProductionMigrationError(
                "preproduction_release_manifest_invalid"
            )
        value = json.loads(path.read_text(encoding="utf-8"))
    except CognitiveProductionMigrationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CognitiveProductionMigrationError(
            "preproduction_release_manifest_invalid"
        ) from None
    if not isinstance(value, dict):
        raise CognitiveProductionMigrationError(
            "preproduction_release_manifest_invalid"
        )
    return value


def _manifest_schema_digest(connection: sqlite3.Connection) -> str:
    rows = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
        )
    ]
    return canonical_digest(rows)


def _default_package_verifier(package: Path) -> Mapping[str, Any]:
    try:
        from tools.v10_release_recovery import verify_package

        return verify_package(package, expected_phase="preproduction")
    except Exception:
        raise CognitiveProductionMigrationError(
            "preproduction_package_not_recoverable"
        ) from None


def _revalidate_preproduction_package(
    backup: Mapping[str, Any],
    *,
    package_verifier: Callable[[Path], Mapping[str, Any]],
    full_package_verification: bool = True,
) -> None:
    recorded_root = Path(backup["preproduction_package_absolute_path"])
    try:
        _reject_reparse_chain(recorded_root)
        root = recorded_root.resolve(strict=True)
    except OSError:
        raise CognitiveProductionMigrationError(
            "preproduction_package_unavailable"
        ) from None
    if not root.is_dir() or not _same_path(root, recorded_root):
        raise CognitiveProductionMigrationError(
            "preproduction_package_path_mismatch"
        )
    _reject_reparse_chain(root)

    manifest_path = root / "release_manifest.json"
    if not manifest_path.is_file():
        raise CognitiveProductionMigrationError(
            "preproduction_release_manifest_missing"
        )
    _reject_reparse_chain(manifest_path)
    _reject_hardlink_alias(manifest_path)
    if _file_gate(manifest_path) != backup["release_manifest_file_gate"]:
        raise CognitiveProductionMigrationError(
            "preproduction_release_manifest_changed"
        )
    manifest = _load_release_manifest(manifest_path)
    unsigned_manifest = {
        key: value for key, value in manifest.items() if key != "manifest_digest"
    }
    if (
        manifest.get("phase") != "preproduction"
        or manifest.get("manifest_digest") != canonical_digest(unsigned_manifest)
        or manifest.get("manifest_digest") != backup["backup_manifest_digest"]
        or manifest.get("backup_set_digest") != backup["backup_set_digest"]
    ):
        raise CognitiveProductionMigrationError(
            "preproduction_release_manifest_binding_mismatch"
        )
    database_results = manifest.get("database_results")
    operational = (
        database_results.get("operational")
        if isinstance(database_results, Mapping)
        else None
    )
    expected_audit = (
        operational.get("backup_audit")
        if isinstance(operational, Mapping)
        else None
    )
    relative = backup["operational_backup_relative_path"]
    if (
        not isinstance(operational, Mapping)
        or operational.get("backup_relative_path") != relative
        or not isinstance(expected_audit, Mapping)
    ):
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_binding_mismatch"
        )
    backup_path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        _reject_reparse_chain(backup_path)
        resolved_backup = backup_path.resolve(strict=True)
        resolved_backup.relative_to(root)
    except (OSError, ValueError):
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_missing"
        ) from None
    if not resolved_backup.is_file():
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_missing"
        )
    _reject_reparse_chain(resolved_backup)
    _reject_hardlink_alias(resolved_backup)
    actual_gate = _file_gate(resolved_backup)
    recorded_gate = {
        "exists": True,
        "size": backup["backup_database_size"],
        "sha256": backup["backup_database_sha256"],
    }
    if actual_gate != recorded_gate or expected_audit.get("file_gate") != actual_gate:
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_changed"
        )
    try:
        uri = resolved_backup.as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            connection.execute("PRAGMA query_only=ON")
            integrity = [
                str(row[0])
                for row in connection.execute("PRAGMA integrity_check")
            ]
            foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
            operational_schema = schema_fingerprint(connection)
            manifest_schema = _manifest_schema_digest(connection)
    except sqlite3.Error:
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_unreadable"
        ) from None
    if integrity != ["ok"]:
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_integrity_failed"
        )
    if foreign_keys:
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_foreign_key_failed"
        )
    if (
        operational_schema != backup["backup_database_schema_digest"]
        or expected_audit.get("schema_digest") != manifest_schema
        or expected_audit.get("integrity_check") != ["ok"]
        or expected_audit.get("foreign_key_violation_count") != 0
    ):
        raise CognitiveProductionMigrationError(
            "preproduction_operational_backup_schema_mismatch"
        )
    if full_package_verification:
        try:
            verified = dict(package_verifier(root))
        except CognitiveProductionMigrationError:
            raise
        except Exception:
            raise CognitiveProductionMigrationError(
                "preproduction_package_not_recoverable"
            ) from None
        if (
            verified.get("status") != "verified"
            or verified.get("phase") != "preproduction"
            or verified.get("manifest_digest")
            != backup["backup_manifest_digest"]
            or verified.get("backup_set_digest") != backup["backup_set_digest"]
            or verified.get("service_quiescence_digest")
            != backup["service_quiescence_digest"]
        ):
            raise CognitiveProductionMigrationError(
                "preproduction_package_not_recoverable"
            )
    if (
        _file_gate(manifest_path) != backup["release_manifest_file_gate"]
        or _file_gate(resolved_backup) != actual_gate
    ):
        raise CognitiveProductionMigrationError(
            "preproduction_package_changed_during_verification"
        )


def _listener_count(port: int) -> int:
    count = 0
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                if probe.connect_ex((host, port)) == 0:
                    count += 1
        except OSError:
            continue
    return count


def _capture_live_quiescence(database: Path) -> dict[str, Any]:
    try:
        uri = database.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            writer_lease_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM state_writer_leases"
                ).fetchone()[0]
            )
    except (sqlite3.Error, TypeError, ValueError):
        raise CognitiveProductionMigrationError(
            "live_writer_lease_gate_unavailable"
        ) from None
    return {
        "local_service_port": 8765,
        "local_service_listener_count": _listener_count(8765),
        "writer_lease_count": writer_lease_count,
        "operational_wal_size": _wal_gate(database)["size"],
        "production_write_env_enabled": (
            os.environ.get(PRODUCTION_WRITE_ENV, "").strip() == "1"
        ),
    }


def _assert_live_quiescence(value: Mapping[str, Any]) -> None:
    expected = {
        "local_service_port",
        "local_service_listener_count",
        "writer_lease_count",
        "operational_wal_size",
        "production_write_env_enabled",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CognitiveProductionMigrationError("live_quiescence_gate_invalid")
    if value["local_service_port"] != 8765:
        raise CognitiveProductionMigrationError("live_quiescence_gate_invalid")
    if value["local_service_listener_count"] != 0:
        raise CognitiveProductionMigrationError("live_service_listener_present")
    if value["writer_lease_count"] != 0:
        raise CognitiveProductionMigrationError("live_writer_lease_present")
    if value["operational_wal_size"] != 0:
        raise CognitiveProductionMigrationError("live_operational_wal_nonempty")
    if value["production_write_env_enabled"] is not False:
        raise CognitiveProductionMigrationError("live_production_write_env_enabled")


def _capture_live_claim_boundary_and_source_identities() -> dict[str, Any]:
    """Capture one sealed boundary/source snapshot under shared live pins."""

    try:
        from tools.v10_f_capture_production_quiescence import (
            capture_fixed_claim_boundary_and_source_identities,
        )

        value = capture_fixed_claim_boundary_and_source_identities()
    except Exception:
        raise CognitiveProductionMigrationError(
            "live_claim_boundary_source_snapshot_unavailable"
        ) from None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"boundary", "source_identities"}
        or not isinstance(value.get("boundary"), Mapping)
        or not isinstance(value.get("source_identities"), Mapping)
    ):
        raise CognitiveProductionMigrationError(
            "live_claim_boundary_source_snapshot_invalid"
        )
    return {
        "boundary": dict(value["boundary"]),
        "source_identities": dict(value["source_identities"]),
    }


def _assert_live_capture_source_identities(
    value: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    try:
        live = _validate_capture_source_identities(value)
        sealed = _validate_capture_source_identities(expected)
    except CognitiveProductionMigrationError:
        raise CognitiveProductionMigrationError(
            "live_capture_source_identity_invalid"
        ) from None
    if live != sealed:
        raise CognitiveProductionMigrationError(
            "capture_artifact_source_identity_changed_before_claim"
        )


def _assert_live_tailscale_boundary(
    value: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    expected = {
        "tailscale_backend",
        "tailscale_serve_config_digest",
        "funnel_enabled",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise CognitiveProductionMigrationError(
            "live_tailscale_boundary_invalid"
        )
    if (
        value["tailscale_backend"] != "Running"
        or value["funnel_enabled"] is not False
        or value["tailscale_serve_config_digest"]
        != evidence["tailscale_serve_config_digest"]
    ):
        raise CognitiveProductionMigrationError(
            "live_tailscale_boundary_changed"
        )


def _capture_code_state() -> dict[str, Any]:
    def run(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ("git", "-C", str(PROJECT_ROOT), *arguments),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=30,
            )
        except (OSError, UnicodeError, subprocess.SubprocessError):
            raise CognitiveProductionMigrationError(
                "migration_git_state_unavailable"
            ) from None
        return result.stdout.strip()

    tracked_status = run("status", "--porcelain", "--untracked-files=no")
    untracked_status = run("ls-files", "--others", "--exclude-standard")
    return {
        "commit": run("rev-parse", "HEAD"),
        "tracked_worktree_clean": not bool(tracked_status),
        "staging_empty": not bool(run("diff", "--cached", "--name-only")),
        "untracked_empty": not bool(untracked_status),
        "worktree_clean": not bool(tracked_status or untracked_status),
    }


def _open_control_relative_fd(
    path: Path,
    flags: int,
    *,
    directory_pin: PinnedControlDirectory | None,
    mode: int = 0o600,
) -> int:
    if directory_pin is not None:
        if path.parent != directory_pin.path:
            raise CognitiveProductionMigrationError(
                "production_control_path_mismatch"
            )
        directory_pin.verify()
    target: str | Path = path
    options: dict[str, Any] = {}
    if directory_pin is not None and os.name != "nt":
        target = path.name
        options["dir_fd"] = directory_pin.leaf_token
    try:
        descriptor = os.open(
            target,
            flags | getattr(os, "O_NOFOLLOW", 0),
            mode,
            **options,
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or int(details.st_nlink) != 1:
            raise OSError("unsafe control file identity")
        return descriptor
    except OSError:
        raise CognitiveProductionMigrationError(
            "migration_ledger_invalid",
            recovery_required=True,
        ) from None


def _read_ledger(
    path: Path,
    *,
    directory_pin: PinnedControlDirectory | None = None,
) -> list[dict[str, Any]]:
    if directory_pin is None:
        directory_pin = _ACTIVE_CONTROL_DIRECTORY_PIN.get()
    try:
        if directory_pin is not None:
            if path.parent != directory_pin.path:
                raise CognitiveProductionMigrationError(
                    "production_control_path_mismatch"
                )
            ledger_pin = directory_pin.open_mutable_file(
                path.name,
                create_if_missing=False,
                maximum_bytes=16 * 1024 * 1024,
                missing_ok=True,
            )
            if ledger_pin is None:
                return []
            raw = ledger_pin.read()
        else:
            if not path.exists():
                return []
            descriptor = _open_control_relative_fd(
                path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
                directory_pin=None,
            )
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                raw = handle.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise CognitiveProductionMigrationError("migration_ledger_invalid")
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except CognitiveProductionMigrationError:
        raise
    except (OSError, UnicodeError, SecureControlFileError):
        raise CognitiveProductionMigrationError("migration_ledger_invalid") from None
    if any(not line for line in lines):
        raise CognitiveProductionMigrationError("migration_ledger_invalid")
    records: list[dict[str, Any]] = []
    previous = _EMPTY_SHA256
    states: dict[str, list[str]] = {}
    bindings: dict[str, tuple[str, str, str]] = {}
    expected_fields = {
        "ledger_schema_version",
        "sequence",
        "one_time_execution_id",
        "authority_digest",
        "migration_id",
        "ddl_digest",
        "source_schema_digest",
        "target_schema_digest",
        "state",
        "reason_code",
        "recovery_required",
        "recorded_at",
        "previous_record_digest",
        "record_digest",
    }
    for index, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise CognitiveProductionMigrationError("migration_ledger_invalid") from None
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise CognitiveProductionMigrationError("migration_ledger_invalid")
        digest = value.pop("record_digest")
        if (
            value["sequence"] != index
            or value["ledger_schema_version"]
            != COGNITIVE_PRODUCTION_LEDGER_VERSION
            or value["migration_id"] != COGNITIVE_PRODUCTION_MIGRATION_ID
            or value["ddl_digest"] != COGNITIVE_DDL_DIGEST
            or value["state"] not in _LEDGER_STATES
            or value["previous_record_digest"] != previous
            or digest != canonical_digest(value)
            or type(value["recovery_required"]) is not bool
            or value["recovery_required"]
            is not (value["state"] == "recovery_required")
        ):
            raise CognitiveProductionMigrationError("migration_ledger_invalid")
        value["record_digest"] = digest
        _identity(value["one_time_execution_id"], "migration_ledger_invalid")
        for field in (
            "authority_digest",
            "ddl_digest",
            "source_schema_digest",
            "target_schema_digest",
            "previous_record_digest",
            "record_digest",
        ):
            _sha(value[field], "migration_ledger_invalid")
        _timestamp(value["recorded_at"], "migration_ledger_invalid")
        if not isinstance(value["reason_code"], str) or _SAFE_REASON_RE.fullmatch(
            value["reason_code"]
        ) is None:
            raise CognitiveProductionMigrationError("migration_ledger_invalid")
        history = states.setdefault(value["one_time_execution_id"], [])
        binding = (
            value["authority_digest"],
            value["source_schema_digest"],
            value["target_schema_digest"],
        )
        expected_binding = bindings.setdefault(
            value["one_time_execution_id"], binding
        )
        if binding != expected_binding:
            raise CognitiveProductionMigrationError("migration_ledger_invalid")
        if not history and value["state"] != "claimed":
            raise CognitiveProductionMigrationError("migration_ledger_invalid")
        if history:
            allowed = (
                {"completed", "rolled_back", "recovery_required"}
                if history[-1] == "claimed"
                else {"completed", "restored"}
                if history[-1] == "recovery_required"
                else {"recovery_required"}
                if history[-1] == "completed"
                else set()
            )
            if value["state"] not in allowed:
                raise CognitiveProductionMigrationError("migration_ledger_invalid")
        history.append(value["state"])
        previous = digest
        records.append(value)
    return records


def _append_ledger_state(
    path: Path,
    *,
    authority: Mapping[str, Any],
    state: str,
    reason_code: str,
    recovery_required: bool,
    recorded_at: str,
    directory_pin: PinnedControlDirectory | None = None,
) -> dict[str, Any]:
    if directory_pin is None:
        directory_pin = _ACTIVE_CONTROL_DIRECTORY_PIN.get()
    if state not in _LEDGER_STATES or _SAFE_REASON_RE.fullmatch(reason_code) is None:
        raise CognitiveProductionMigrationError("migration_ledger_state_invalid")
    records = _read_ledger(path, directory_pin=directory_pin)
    execution_history = [
        record["state"]
        for record in records
        if record["one_time_execution_id"]
        == authority["one_time_execution_id"]
    ]
    allowed = (
        {"claimed"}
        if not execution_history
        else {"completed", "rolled_back", "recovery_required"}
        if execution_history[-1] == "claimed"
        else {"completed", "restored"}
        if execution_history[-1] == "recovery_required"
        else {"recovery_required"}
        if execution_history[-1] == "completed"
        else set()
    )
    if state not in allowed:
        raise CognitiveProductionMigrationError(
            "migration_ledger_state_transition_invalid"
        )
    if recovery_required is not (state == "recovery_required"):
        raise CognitiveProductionMigrationError(
            "migration_ledger_state_invalid"
        )
    previous = records[-1]["record_digest"] if records else _EMPTY_SHA256
    record = {
        "ledger_schema_version": COGNITIVE_PRODUCTION_LEDGER_VERSION,
        "sequence": len(records) + 1,
        "one_time_execution_id": authority["one_time_execution_id"],
        "authority_digest": authority["authority_digest"],
        "migration_id": COGNITIVE_PRODUCTION_MIGRATION_ID,
        "ddl_digest": COGNITIVE_DDL_DIGEST,
        "source_schema_digest": authority["source_schema_digest"],
        "target_schema_digest": authority["target_schema_digest"],
        "state": state,
        "reason_code": reason_code,
        "recovery_required": recovery_required,
        "recorded_at": normalize_timestamp(recorded_at),
        "previous_record_digest": previous,
    }
    record["record_digest"] = canonical_digest(record)
    encoded = (canonical_json(record) + "\n").encode("utf-8")
    try:
        if directory_pin is not None:
            if path.parent != directory_pin.path:
                raise CognitiveProductionMigrationError(
                    "production_control_path_mismatch"
                )
            ledger_pin = directory_pin.open_mutable_file(
                path.name,
                create_if_missing=True,
                maximum_bytes=16 * 1024 * 1024,
            )
            if ledger_pin is None:  # create_if_missing makes this unreachable
                raise SecureControlFileError("secure_control_file_missing")
            ledger_pin.append(encoded)
        else:
            descriptor = _open_control_relative_fd(
                path,
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_BINARY", 0),
                directory_pin=None,
            )
            with os.fdopen(descriptor, "ab", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, SecureControlFileError):
        raise CognitiveProductionMigrationError(
            "migration_ledger_write_failed",
            recovery_required=state != "claimed",
        ) from None
    # A torn or concurrent append is a hard fail-closed condition.
    validated = _read_ledger(path, directory_pin=directory_pin)
    if validated[-1]["record_digest"] != record["record_digest"]:
        raise CognitiveProductionMigrationError(
            "migration_ledger_write_mismatch",
            recovery_required=True,
        )
    return record


def _assert_execution_not_replayed(
    records: list[dict[str, Any]], execution_id: str
) -> None:
    if any(record["one_time_execution_id"] == execution_id for record in records):
        raise CognitiveProductionMigrationError(
            "one_time_migration_authority_replayed"
        )


def _lock_descriptor(descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        raise CognitiveProductionMigrationError(
            "production_migration_lock_active",
            recovery_required=True,
        ) from None


def _unlock_descriptor(descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except (ImportError, OSError):
        # Closing the descriptor is the final kernel-level unlock.
        pass


@contextmanager
def _execution_lock(
    path: Path,
    execution_id: str,
    *,
    directory_pin: PinnedControlDirectory | None = None,
) -> Iterator[None]:
    """Hold a kernel lock whose ownership is released on hard process exit.

    The regular marker intentionally persists.  A subsequent process may only
    overwrite it after acquiring the same OS lock, so a dead owner is safely
    recoverable while a live owner remains fail-closed.
    """

    descriptor: int | None = None
    pinned_lock: PinnedMutableControlFile | None = None
    descriptor_owned_here = True
    locked = False
    try:
        if directory_pin is not None and path.parent != directory_pin.path:
            raise CognitiveProductionMigrationError(
                "production_control_path_mismatch"
            )
        if directory_pin is not None:
            pinned_lock = directory_pin.open_mutable_file(
                path.name,
                create_if_missing=True,
                maximum_bytes=64 * 1024,
                acquire_lock=False,
            )
            if pinned_lock is None:  # create_if_missing makes this unreachable
                raise SecureControlFileError("secure_control_file_missing")
            descriptor = pinned_lock.handle.fileno()
            descriptor_owned_here = False
        else:
            descriptor = os.open(
                path,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        details = os.fstat(descriptor)
        if details.st_size == 0:
            os.write(descriptor, b"\x00")
            os.fsync(descriptor)
        _reject_reparse_chain(path)
        _reject_hardlink_alias(path)
        path_details = path.stat()
        if (
            int(details.st_dev),
            int(details.st_ino),
        ) != (
            int(path_details.st_dev),
            int(path_details.st_ino),
        ):
            raise CognitiveProductionMigrationError(
                "migration_lock_identity_mismatch",
                recovery_required=True,
            )
        _lock_descriptor(descriptor)
        locked = True
        if directory_pin is not None:
            directory_pin.verify()
        # Recheck after acquisition to close the open/lock path substitution
        # window before publishing this process as the current owner.
        _reject_reparse_chain(path)
        _reject_hardlink_alias(path)
        path_details = path.stat()
        if (
            int(details.st_dev),
            int(details.st_ino),
        ) != (
            int(path_details.st_dev),
            int(path_details.st_ino),
        ):
            raise CognitiveProductionMigrationError(
                "migration_lock_identity_mismatch",
                recovery_required=True,
            )
        record = {
            "lock_schema_version": COGNITIVE_PRODUCTION_LOCK_VERSION,
            "migration_id": COGNITIVE_PRODUCTION_MIGRATION_ID,
            "one_time_execution_id": execution_id,
            "owner_pid": os.getpid(),
            "owner_host": socket.gethostname(),
            "owner_nonce": uuid.uuid4().hex,
            "acquired_at": utc_now(),
            "kernel_lock_protocol": True,
        }
        record["record_digest"] = canonical_digest(record)
        payload = canonical_control_document_bytes(record)
        if pinned_lock is not None:
            pinned_lock.rewrite(payload)
        else:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written < 1:
                    raise OSError("short lock write")
                view = view[written:]
            os.fsync(descriptor)
        yield
    except CognitiveProductionMigrationError:
        raise
    except SecureControlFileError as exc:
        reason = str(exc)
        if reason in {
            "secure_control_handle_open_failed",
            "secure_control_file_lock_unavailable",
        }:
            raise CognitiveProductionMigrationError(
                "production_migration_lock_active",
                recovery_required=True,
            ) from None
        raise CognitiveProductionMigrationError(
            "production_migration_lock_failed"
        ) from None
    except OSError:
        raise CognitiveProductionMigrationError(
            "production_migration_lock_failed"
        ) from None
    finally:
        if descriptor is not None:
            if locked:
                _unlock_descriptor(descriptor)
            if descriptor_owned_here:
                os.close(descriptor)


def _checkpoint(_name: str) -> None:
    """No-op production checkpoint; tests patch it for deterministic faults."""


def _verify_pretransaction_binding(
    connection: sqlite3.Connection,
    plan: Mapping[str, Any],
    database: Path,
) -> None:
    _assert_core_only(connection)
    gate = _file_gate(database)
    wal = _wal_gate(database)
    if (
        gate["sha256"] != plan["source_database_sha256"]
        or gate["size"] != plan["source_database_size"]
        or (wal["exists"] and wal["size"] != 0)
        or schema_fingerprint(connection) != plan["source_schema_digest"]
        or _core_schema_snapshot_digest(connection)
        != plan["core_schema_snapshot_digest"]
        or _core_data_digest(connection) != plan["core_data_digest"]
    ):
        raise CognitiveProductionMigrationError(
            "production_schema_changed_after_preflight"
        )


def _apply_fixed_cognitive_ddl(
    connection: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    objects_before = _all_named_schema_objects(connection)
    core_schema_before = _core_schema_snapshot_digest(connection)
    core_data_before = _core_data_digest(connection)
    for statement in _COGNITIVE_DDL:
        connection.execute(statement)
    _checkpoint("fixed_ddl_applied")
    objects_after = _all_named_schema_objects(connection)
    if objects_after - objects_before != _COGNITIVE_OBJECTS:
        raise CognitiveProductionMigrationError(
            "cognitive_schema_resource_effect_mismatch"
        )
    if _core_schema_snapshot_digest(connection) != core_schema_before:
        raise CognitiveProductionMigrationError("v9_core_schema_changed")
    digest = schema_fingerprint(connection)
    if digest != plan["target_schema_digest"]:
        raise CognitiveProductionMigrationError(
            "cognitive_target_schema_digest_mismatch"
        )
    connection.execute(
        "UPDATE operational_schema SET schema_digest=? WHERE singleton=1",
        (digest,),
    )
    connection.execute(
        "INSERT INTO cognitive_schema VALUES(?,?,?,?,?,?,?)",
        (
            1,
            COGNITIVE_SCHEMA_MARKER,
            COGNITIVE_SCHEMA_GENERATION,
            digest,
            COGNITIVE_CONTRACT_VERSION,
            normalize_timestamp(created_at),
            0,
        ),
    )
    _checkpoint("markers_written")
    verified = _verify_cognitive_connection(connection)
    if (
        _core_data_digest(connection) != core_data_before
        or core_data_before != plan["core_data_digest"]
    ):
        raise CognitiveProductionMigrationError("v9_core_data_changed")
    if connection.execute(
        "SELECT COUNT(*) FROM state_writer_leases"
    ).fetchone()[0] != 0:
        raise CognitiveProductionMigrationError(
            "cognitive_migration_active_writer_lease"
        )
    return verified


def _reconcile_database(
    database: Path,
    *,
    authority: Mapping[str, Any],
    production: bool,
    require_source_data_unchanged: bool = True,
) -> dict[str, Any]:
    try:
        with closing(
            _open_read_connection(database, production=production)
        ) as connection:
            existing = _existing_cognitive_objects(connection)
            lease_count = connection.execute(
                "SELECT COUNT(*) FROM state_writer_leases"
            ).fetchone()[0]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            )
            core_schema = _core_schema_snapshot_digest(connection)
            core_data = _core_data_digest(connection)
            if (
                existing == _COGNITIVE_OBJECTS
                and lease_count == 0
                and integrity == "ok"
                and foreign_keys == 0
            ):
                verified = _verify_cognitive_connection(connection)
                if (
                    verified["schema_digest"]
                    == authority["target_schema_digest"]
                    and core_schema
                    == authority["core_schema_snapshot_digest"]
                    and (
                        not require_source_data_unchanged
                        or core_data == authority["core_data_digest"]
                    )
                ):
                    return {
                        "state": "completed",
                        "reason_code": "postcommit_schema_reconciled",
                        "recovery_required": False,
                        "verification": verified,
                    }
            if not existing:
                OperationalStateStore._verify_schema(connection)
                if (
                    lease_count == 0
                    and integrity == "ok"
                    and foreign_keys == 0
                    and schema_fingerprint(connection)
                    == authority["source_schema_digest"]
                    and core_schema
                    == authority["core_schema_snapshot_digest"]
                    and core_data == authority["core_data_digest"]
                ):
                    return {
                        "state": "rolled_back",
                        "reason_code": "precommit_transaction_rolled_back",
                        "recovery_required": False,
                        "verification": None,
                    }
    except (OperationalStateError, sqlite3.Error):
        pass
    return {
        "state": "recovery_required",
        "reason_code": "migration_state_cannot_be_proven",
        "recovery_required": True,
        "verification": None,
        "recovery_action": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
    }


def _outcome(
    *,
    authority: Mapping[str, Any],
    status: str,
    reconciliation: bool,
    verification: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "migration_id": COGNITIVE_PRODUCTION_MIGRATION_ID,
        "core_schema_version": SCHEMA_VERSION,
        "schema_generation": COGNITIVE_SCHEMA_GENERATION,
        "ddl_digest": COGNITIVE_DDL_DIGEST,
        "source_schema_digest": authority["source_schema_digest"],
        "target_schema_digest": authority["target_schema_digest"],
        "authority_digest": authority["authority_digest"],
        "one_time_execution_id": authority["one_time_execution_id"],
        "system_verified": verification is not None,
        "verification": dict(verification) if verification is not None else None,
        "postcommit_reconciliation": reconciliation,
        "recovery_required": False,
        "acceptance_state": "waiting_acceptance",
        "rollback_policy": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
        "destructive_schema_rollback_allowed": False,
    }


def _publish_completed_under_pins(
    paths: _BoundProductionPaths,
    *,
    authority: Mapping[str, Any],
    backup: Mapping[str, Any],
    package_verifier: Callable[[Path], Mapping[str, Any]],
    recorded_at: str,
    reason_code: str,
    status: str,
    reconciliation: bool,
    expected_database_identity: tuple[str, int, int] | None,
    append_completed: bool = True,
    require_source_data_unchanged: bool = True,
) -> dict[str, Any]:
    """Publish completion while mandatory live-file pins remain held.

    Windows production uses share-read-only handles, so write/delete/rename is
    denied from the first final validation until the ledger append, read-back
    postconditions, outcome construction, and context return.  POSIX uses an
    advisory exclusive flock plus the same before/after identity and SHA-256
    postconditions; YunOS local production is Windows.
    """

    try:
        with _completion_publication_pins(backup, paths.database) as pins:
            live_pin = pins["live_database"]
            pinned_identity = (
                live_pin.canonical_path,
                live_pin.device,
                live_pin.inode,
            )
            if (
                expected_database_identity is not None
                and pinned_identity != expected_database_identity
            ):
                raise CognitiveProductionMigrationError(
                    "production_database_identity_changed",
                    recovery_required=True,
                )
            _revalidate_preproduction_package(
                backup,
                package_verifier=package_verifier,
                full_package_verification=False,
            )
            if expected_database_identity is not None:
                _assert_bound_database_identity(
                    paths.database, expected_database_identity
                )
            verification = _reconcile_database(
                paths.database,
                authority=authority,
                production=paths.production,
                require_source_data_unchanged=require_source_data_unchanged,
            )
            if verification["state"] != "completed":
                raise CognitiveProductionMigrationError(
                    "completion_publication_final_verification_failed",
                    recovery_required=True,
                )
            for pin in pins.values():
                _assert_publication_pin(pin)
            if append_completed:
                _append_ledger_state(
                    paths.ledger,
                    authority=authority,
                    state="completed",
                    reason_code=reason_code,
                    recovery_required=False,
                    recorded_at=recorded_at,
                )
            # These postconditions execute while the exact same handles remain
            # live.  They detect non-cooperating POSIX mutation and independently
            # prove that the object published in the ledger is still verified.
            _revalidate_preproduction_package(
                backup,
                package_verifier=package_verifier,
                full_package_verification=False,
            )
            if expected_database_identity is not None:
                _assert_bound_database_identity(
                    paths.database, expected_database_identity
                )
            post = _reconcile_database(
                paths.database,
                authority=authority,
                production=paths.production,
                require_source_data_unchanged=require_source_data_unchanged,
            )
            if post["state"] != "completed":
                raise CognitiveProductionMigrationError(
                    "completion_publication_postcondition_failed",
                    recovery_required=True,
                )
            for pin in pins.values():
                _assert_publication_pin(pin)
            return _outcome(
                authority=authority,
                status=status,
                reconciliation=reconciliation,
                verification=post["verification"],
            )
    except Exception as exc:
        reason = _reason_from_exception(exc)
        try:
            execution_records = [
                record
                for record in _read_ledger(paths.ledger)
                if record["one_time_execution_id"]
                == authority["one_time_execution_id"]
            ]
            if execution_records and execution_records[-1]["state"] in {
                "claimed",
                "completed",
            }:
                _append_ledger_state(
                    paths.ledger,
                    authority=authority,
                    state="recovery_required",
                    reason_code=reason,
                    recovery_required=True,
                    recorded_at=recorded_at,
                )
        except Exception as ledger_exc:
            raise CognitiveProductionMigrationError(
                "completion_publication_failure_ledger_write_failed",
                recovery_required=True,
            ) from ledger_exc
        raise CognitiveProductionMigrationError(
            "completion_publication_failed",
            recovery_required=True,
        ) from None


def _execute_bound_cognitive_migration(
    paths: _BoundProductionPaths,
    *,
    current_time: datetime | None = None,
    _test_package_verifier: Callable[[Path], Mapping[str, Any]] | None = None,
    _test_live_gate_probe: Callable[[Path], Mapping[str, Any]] | None = None,
    _test_tailscale_gate_probe: Callable[[], Mapping[str, Any]] | None = None,
    _test_source_identity_probe: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Private bound-target executor used by the fixed public entry and tests."""

    if paths.production and os.name != "nt":
        raise CognitiveProductionMigrationError(
            "production_platform_unsupported"
        )
    if paths.production and (
        current_time is not None
        or _test_package_verifier is not None
        or _test_live_gate_probe is not None
        or _test_tailscale_gate_probe is not None
        or _test_source_identity_probe is not None
    ):
        raise CognitiveProductionMigrationError("production_test_seam_forbidden")
    _validate_bound_paths(paths)
    now = _utc_datetime(current_time)
    now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    claimed = False
    with _pinned_control_documents(paths) as (
        control_directory,
        control_pins,
    ), ExitStack() as migration_lifetime:
        authority = _validate_authority_document(
            _read_pinned_canonical_document(
                control_pins["authority"],
                "migration_authority_record_invalid",
            ),
            now=now,
        )
        backup = _validate_backup_document(
            _read_pinned_canonical_document(
                control_pins["backup"],
                "backup_verification_record_invalid",
            ),
            plan=None,
            now=now,
        )
        capture = validate_cognitive_quiescence_capture_artifact(
            _read_pinned_canonical_document(
                control_pins["capture"],
                "service_quiescence_evidence_invalid",
            ),
            now=now,
        )
        evidence = validate_cognitive_production_quiescence_evidence(
            capture["evidence"],
            now=now,
            authorized_at=_timestamp(
                authority["authorized_at"], "authorization_time_invalid"
            ),
        )
        _validate_capture_bindings(
            capture=capture,
            backup=backup,
            authority=authority,
        )
        recorded_plan = _validate_plan_document(
            _read_pinned_canonical_document(
                control_pins["plan"], "migration_plan_invalid"
            )
        )
        manifest_document = _read_pinned_canonical_document(
            control_pins["manifest"], "control_manifest_invalid"
        )
        database_identity = _capture_bound_database_identity(paths.database)
        records = _read_ledger(paths.ledger)
        _assert_execution_not_replayed(
            records, authority["one_time_execution_id"]
        )
        plan = _build_plan(paths.database, production=paths.production)
        if dict(recorded_plan) != dict(plan):
            raise CognitiveProductionMigrationError(
                "recorded_migration_plan_mismatch"
            )
        backup = _validate_backup_document(backup, plan=plan, now=now)
        code_state = _capture_code_state()
        _validate_bound_documents(
            plan=plan,
            authority=authority,
            backup=backup,
            code_state=code_state,
        )
        _validate_control_manifest_document(
            manifest_document,
            plan=plan,
            backup=backup,
            authority=authority,
            capture=capture,
        )
        _revalidate_preproduction_package(
            backup,
            package_verifier=(
                _test_package_verifier or _default_package_verifier
            ),
        )
        _checkpoint("preflight_validated")
        claim_time = now if current_time is not None else _utc_datetime()
        authority = _validate_authority_document(authority, now=claim_time)
        backup = _validate_backup_document(
            backup, plan=plan, now=claim_time
        )
        claim_capture = validate_cognitive_quiescence_capture_artifact(
            _read_pinned_canonical_document(
                control_pins["capture"],
                "service_quiescence_evidence_invalid",
            ),
            now=claim_time,
        )
        claim_evidence = validate_cognitive_production_quiescence_evidence(
            claim_capture["evidence"],
            now=claim_time,
            authorized_at=_timestamp(
                authority["authorized_at"], "authorization_time_invalid"
            ),
        )
        if claim_capture != capture or claim_evidence != evidence:
            raise CognitiveProductionMigrationError(
                "service_quiescence_binding_mismatch"
            )
        now_text = claim_time.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        live_gate = (
            _test_live_gate_probe(paths.database)
            if _test_live_gate_probe is not None
            else _capture_live_quiescence(paths.database)
        )
        _assert_live_quiescence(live_gate)
        claim_code_state = _capture_code_state()
        _validate_bound_documents(
            plan=plan,
            authority=authority,
            backup=backup,
            code_state=claim_code_state,
        )
        if claim_code_state != code_state:
            raise CognitiveProductionMigrationError(
                "production_code_state_changed_before_claim"
            )
        preclaim_pins = migration_lifetime.enter_context(
            _preclaim_recovery_pins(backup, paths.database)
        )
        pinned_live_identity = preclaim_pins["live_database"]
        if (
            pinned_live_identity.canonical_path,
            pinned_live_identity.device,
            pinned_live_identity.inode,
        ) != database_identity:
            raise CognitiveProductionMigrationError(
                "production_database_identity_changed",
                recovery_required=True,
            )
        # This is deliberately the last exact source/package comparison
        # before the one-shot ledger claim.  The package already received a
        # full recovery verification above; here we re-read the bound
        # manifest, Operational DB copy, schema and integrity gates so claim
        # freshness does not depend on a second multi-gigabyte hash pass.
        _revalidate_preproduction_package(
            backup,
            package_verifier=(
                _test_package_verifier or _default_package_verifier
            ),
            full_package_verification=False,
        )
        if paths.production:
            live_claim_snapshot = (
                _capture_live_claim_boundary_and_source_identities()
            )
            tailscale_gate = live_claim_snapshot["boundary"]
            live_source_identities = live_claim_snapshot[
                "source_identities"
            ]
        else:
            if _test_tailscale_gate_probe is None:
                raise CognitiveProductionMigrationError(
                    "clone_claim_boundary_probe_required"
                )
            tailscale_gate = _test_tailscale_gate_probe()
            live_source_identities = (
                _test_source_identity_probe()
                if _test_source_identity_probe is not None
                else claim_capture["issuer"]["source_identities"]
            )
        _assert_live_tailscale_boundary(tailscale_gate, claim_evidence)
        _assert_live_capture_source_identities(
            live_source_identities,
            claim_capture["issuer"]["source_identities"],
        )
        _append_ledger_state(
            paths.ledger,
            authority=authority,
            state="claimed",
            reason_code="authority_claimed",
            recovery_required=False,
            recorded_at=now_text,
        )
        claimed = True
        committed = False
        try:
            _checkpoint("authority_claimed")
            with closing(_open_write_connection(paths.database)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                _checkpoint("transaction_started")
                _verify_pretransaction_binding(connection, plan, paths.database)
                verification = _apply_fixed_cognitive_ddl(
                    connection,
                    plan=plan,
                    created_at=now_text,
                )
                _checkpoint("before_commit")
                connection.commit()
                committed = True
                _checkpoint("after_commit")
            _assert_bound_database_identity(
                paths.database, database_identity
            )
            _revalidate_preproduction_package(
                backup,
                package_verifier=(
                    _test_package_verifier or _default_package_verifier
                ),
                full_package_verification=False,
            )
            reconciliation = _reconcile_database(
                paths.database,
                authority=authority,
                production=paths.production,
            )
            if reconciliation["state"] != "completed":
                raise CognitiveProductionMigrationError(
                    "postcommit_verification_failed",
                    recovery_required=True,
                )
            verification = reconciliation["verification"]
            _checkpoint("postcommit_verified")
            return _publish_completed_under_pins(
                paths,
                authority=authority,
                backup=backup,
                package_verifier=(
                    _test_package_verifier or _default_package_verifier
                ),
                recorded_at=now_text,
                reason_code="migration_completed",
                status="completed",
                reconciliation=False,
                expected_database_identity=database_identity,
            )
        except Exception as exc:
            reason = _reason_from_exception(exc)
            if not claimed:
                raise
            if reason == "completion_publication_failed":
                raise
            if reason in {
                "production_database_identity_changed",
                "production_database_identity_unverifiable",
                "postcommit_final_verification_failed",
            }:
                try:
                    _append_ledger_state(
                        paths.ledger,
                        authority=authority,
                        state="recovery_required",
                        reason_code=reason,
                        recovery_required=True,
                        recorded_at=now_text,
                    )
                except Exception as ledger_exc:
                    raise CognitiveProductionMigrationError(
                        "migration_failure_ledger_write_failed",
                        recovery_required=True,
                    ) from ledger_exc
                raise CognitiveProductionMigrationError(
                    reason,
                    recovery_required=True,
                ) from None
            reconciliation = _reconcile_database(
                paths.database,
                authority=authority,
                production=paths.production,
            )
            if reconciliation["state"] == "completed":
                return _publish_completed_under_pins(
                    paths,
                    authority=authority,
                    backup=backup,
                    package_verifier=(
                        _test_package_verifier or _default_package_verifier
                    ),
                    recorded_at=now_text,
                    reason_code="postcommit_schema_reconciled",
                    status=(
                        "completed_reconciled" if committed else "completed"
                    ),
                    reconciliation=True,
                    expected_database_identity=database_identity,
                )
            state = reconciliation["state"]
            recovery_required = state == "recovery_required"
            final_reason = (
                reconciliation["reason_code"]
                if recovery_required
                else reason
            )
            try:
                _append_ledger_state(
                    paths.ledger,
                    authority=authority,
                    state=state,
                    reason_code=final_reason,
                    recovery_required=recovery_required,
                    recorded_at=now_text,
                )
            except Exception as ledger_exc:
                raise CognitiveProductionMigrationError(
                    "migration_failure_ledger_write_failed",
                    recovery_required=True,
                ) from ledger_exc
            raise CognitiveProductionMigrationError(
                final_reason,
                recovery_required=recovery_required,
            ) from None


def execute_cognitive_production_migration() -> dict[str, Any]:
    """Execute generation 10001 on the one fixed production target.

    This signature intentionally has no path, SQL, force, replay, rollback, or
    policy arguments.  Control records and the execution ledger are fixed too.
    """

    if os.name != "nt":
        raise CognitiveProductionMigrationError(
            "production_platform_unsupported"
        )
    return _execute_bound_cognitive_migration(_fixed_production_paths())


def _reconcile_bound_cognitive_migration(
    paths: _BoundProductionPaths,
    *,
    current_time: datetime | None = None,
    _test_package_verifier: Callable[[Path], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if paths.production and os.name != "nt":
        raise CognitiveProductionMigrationError(
            "production_platform_unsupported"
        )
    if paths.production and (
        current_time is not None or _test_package_verifier is not None
    ):
        raise CognitiveProductionMigrationError("production_test_seam_forbidden")
    _validate_bound_paths(paths)
    now = _utc_datetime(current_time)
    now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with _pinned_control_documents(paths) as (
        _control_directory,
        control_pins,
    ):
        authority = _validate_authority_document(
            _read_pinned_canonical_document(
                control_pins["authority"],
                "migration_authority_record_invalid",
            ),
            now=now,
            require_current=False,
        )
        recorded_plan = _validate_plan_document(
            _read_pinned_canonical_document(
                control_pins["plan"], "migration_plan_invalid"
            )
        )
        backup = _validate_backup_document(
            _read_pinned_canonical_document(
                control_pins["backup"],
                "backup_verification_record_invalid",
            ),
            plan=recorded_plan,
            now=now,
            require_current=False,
        )
        capture = validate_cognitive_quiescence_capture_artifact(
            _read_pinned_canonical_document(
                control_pins["capture"],
                "service_quiescence_evidence_invalid",
            )
        )
        validate_cognitive_production_quiescence_evidence(
            capture["evidence"],
            authorized_at=_timestamp(
                authority["authorized_at"], "authorization_time_invalid"
            ),
        )
        _validate_capture_bindings(
            capture=capture,
            backup=backup,
            authority=authority,
        )
        manifest_document = _read_pinned_canonical_document(
            control_pins["manifest"], "control_manifest_invalid"
        )
        _validate_bound_documents(
            plan=recorded_plan,
            authority=authority,
            backup=backup,
            code_state=None,
        )
        _validate_control_manifest_document(
            manifest_document,
            plan=recorded_plan,
            backup=backup,
            authority=authority,
            capture=capture,
        )
        records = _read_ledger(paths.ledger)
        execution_records = [
            record
            for record in records
            if record["one_time_execution_id"]
            == authority["one_time_execution_id"]
        ]
        if not execution_records:
            raise CognitiveProductionMigrationError(
                "migration_reconciliation_without_claim"
            )
        last = execution_records[-1]
        if last["authority_digest"] != authority["authority_digest"]:
            raise CognitiveProductionMigrationError(
                "migration_reconciliation_authority_mismatch"
            )
        reconciliation = _reconcile_database(
            paths.database,
            authority=authority,
            production=paths.production,
            require_source_data_unchanged=last["state"] != "completed",
        )

        def backup_is_current_and_recoverable() -> bool:
            try:
                _revalidate_preproduction_package(
                    backup,
                    package_verifier=(
                        _test_package_verifier
                        or _default_package_verifier
                    ),
                )
                return True
            except Exception:
                return False

        def unresolved_backup_result() -> dict[str, Any]:
            return {
                "status": "recovery_required",
                "reason_code": "reconciliation_backup_revalidation_failed",
                "one_time_execution_id": authority[
                    "one_time_execution_id"
                ],
                "recovery_required": True,
                "recovery_action": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
                "destructive_schema_rollback_allowed": False,
            }
        if last["state"] == "completed":
            if reconciliation["state"] != "completed":
                return {
                    "status": "recovery_required",
                    "reason_code": "completed_migration_state_drift",
                    "one_time_execution_id": authority[
                        "one_time_execution_id"
                    ],
                    "recovery_required": True,
                    "recovery_action": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
                    "destructive_schema_rollback_allowed": False,
                }
            try:
                return _publish_completed_under_pins(
                    paths,
                    authority=authority,
                    backup=backup,
                    package_verifier=(
                        _test_package_verifier or _default_package_verifier
                    ),
                    recorded_at=now_text,
                    reason_code="already_completed_verified",
                    status="already_completed",
                    reconciliation=True,
                    expected_database_identity=None,
                    append_completed=False,
                    require_source_data_unchanged=False,
                )
            except CognitiveProductionMigrationError:
                return unresolved_backup_result()
        if last["state"] == "rolled_back":
            if reconciliation["state"] != "rolled_back":
                return {
                    "status": "recovery_required",
                    "reason_code": "rolled_back_migration_state_drift",
                    "one_time_execution_id": authority[
                        "one_time_execution_id"
                    ],
                    "recovery_required": True,
                    "recovery_action": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
                    "destructive_schema_rollback_allowed": False,
                }
            return {
                "status": "not_applied_new_authority_required",
                "one_time_execution_id": authority[
                    "one_time_execution_id"
                ],
                "recovery_required": False,
                "rollback_policy": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
            }
        if last["state"] == "restored":
            return {
                "status": (
                    "restored_verified"
                    if reconciliation["state"] == "rolled_back"
                    else "recovery_required"
                ),
                "reason_code": (
                    "verified_backup_restore_reconciled"
                    if reconciliation["state"] == "rolled_back"
                    else "restored_migration_state_drift"
                ),
                "one_time_execution_id": authority[
                    "one_time_execution_id"
                ],
                "recovery_required": reconciliation["state"] != "rolled_back",
                "recovery_action": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
                "destructive_schema_rollback_allowed": False,
            }
        if reconciliation["state"] == "completed":
            if not backup_is_current_and_recoverable():
                if last["state"] == "claimed":
                    _append_ledger_state(
                        paths.ledger,
                        authority=authority,
                        state="recovery_required",
                        reason_code=(
                            "reconciliation_backup_revalidation_failed"
                        ),
                        recovery_required=True,
                        recorded_at=now_text,
                    )
                return unresolved_backup_result()
            try:
                return _publish_completed_under_pins(
                    paths,
                    authority=authority,
                    backup=backup,
                    package_verifier=(
                        _test_package_verifier or _default_package_verifier
                    ),
                    recorded_at=now_text,
                    reason_code="postcommit_schema_reconciled",
                    status="completed_reconciled",
                    reconciliation=True,
                    expected_database_identity=None,
                )
            except CognitiveProductionMigrationError:
                return unresolved_backup_result()
        if (
            last["state"] == "recovery_required"
            and reconciliation["state"] == "rolled_back"
        ):
            if not backup_is_current_and_recoverable():
                return unresolved_backup_result()
            _append_ledger_state(
                paths.ledger,
                authority=authority,
                state="restored",
                reason_code="verified_backup_restore_reconciled",
                recovery_required=False,
                recorded_at=now_text,
            )
            return {
                "status": "restored_verified",
                "reason_code": "verified_backup_restore_reconciled",
                "one_time_execution_id": authority[
                    "one_time_execution_id"
                ],
                "recovery_required": False,
                "recovery_action": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
                "destructive_schema_rollback_allowed": False,
            }
        if last["state"] == "claimed":
            _append_ledger_state(
                paths.ledger,
                authority=authority,
                state=reconciliation["state"],
                reason_code=reconciliation["reason_code"],
                recovery_required=reconciliation["recovery_required"],
                recorded_at=now_text,
            )
        return {
            "status": reconciliation["state"],
            "reason_code": reconciliation["reason_code"],
            "one_time_execution_id": authority["one_time_execution_id"],
            "recovery_required": reconciliation["recovery_required"],
            "recovery_action": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
            "destructive_schema_rollback_allowed": False,
        }


def reconcile_cognitive_production_migration() -> dict[str, Any]:
    """Classify an uncertain fixed production execution without replaying it."""

    if os.name != "nt":
        raise CognitiveProductionMigrationError(
            "production_platform_unsupported"
        )
    return _reconcile_bound_cognitive_migration(_fixed_production_paths())


def cognitive_production_recovery_policy() -> dict[str, Any]:
    """Declare the only supported production recovery path."""

    return {
        "rollback_policy": COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
        "verified_backup_required": True,
        "restore_to_isolated_path_first": True,
        "destructive_schema_rollback_allowed": False,
        "drop_cognitive_schema_allowed": False,
        "automatic_replay_allowed": False,
    }


__all__ = [
    "COGNITIVE_DDL_DIGEST",
    "COGNITIVE_PRODUCTION_AUTHORITY_VERSION",
    "COGNITIVE_PRODUCTION_BACKUP_VERSION",
    "COGNITIVE_PRODUCTION_CAPABILITY",
    "COGNITIVE_PRODUCTION_LEDGER_VERSION",
    "COGNITIVE_PRODUCTION_MIGRATION_ID",
    "COGNITIVE_PRODUCTION_PLAN_VERSION",
    "COGNITIVE_PRODUCTION_ROLLBACK_POLICY",
    "CognitiveProductionMigrationError",
    "PRODUCTION_AUTHORITY_RECORD",
    "PRODUCTION_BACKUP_VERIFICATION_RECORD",
    "PRODUCTION_EXECUTION_LEDGER",
    "PRODUCTION_EXECUTION_LOCK",
    "PRODUCTION_MIGRATION_CONTROL_ROOT",
    "PRODUCTION_OPERATIONAL_DATABASE",
    "build_cognitive_migration_backup_verification",
    "build_cognitive_migration_plan_for_clone",
    "build_cognitive_production_migration_authority",
    "canonical_control_document_bytes",
    "cognitive_production_recovery_policy",
    "execute_cognitive_production_migration",
    "inspect_fixed_production_cognitive_migration_plan",
    "reconcile_cognitive_production_migration",
    "validate_fixed_production_operational_database",
]
