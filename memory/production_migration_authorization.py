"""Immutable approval and human-authorization contracts for exact v8301.

An approval package describes a proposed operation and always keeps production
execution disabled.  A separate HumanAuthorizationRecord may later authorize
one exact execution.  This module validates those documents; it never creates
human authorization and never opens a writable database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from memory.ingestion_core import canonical_json_bytes, sanitize_reason_code
from memory.ingestion_migrations import (
    MIGRATION_DIGEST,
    MIGRATION_ID,
    MIGRATION_VERSION,
    PLAN_SCHEMA_VERSION,
    MigrationPlan,
    validate_migration_plan,
)


PRODUCTION_EXECUTION_PROTOCOL_VERSION = "v8.3-v8301-production-execution-1"
APPROVAL_PACKAGE_SCHEMA_VERSION = "v8.3-v8301-production-approval-package-2"
AUTHORIZATION_SCHEMA_VERSION = "v8.3-v8301-human-authorization-1"
AUTHORIZATION_SCOPE = "v8301_production_migration"
APPROVAL_STATUS = "awaiting_new_explicit_human_approval"
SUPERSEDED_CLASSIFICATION = "superseded_by_execution_protocol_change"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")

_AUTHORIZATION_FIELDS = frozenset({
    "authorization_id",
    "authorization_scope",
    "approval_package_digest",
    "migration_version",
    "migration_digest",
    "migration_plan_digest",
    "source_database_sha256",
    "source_schema_fingerprint",
    "target_schema_fingerprint",
    "code_commit",
    "user_authorization_text_digest",
    "authorized_at",
    "expires_at",
    "one_time_execution_id",
    "authorization_schema_version",
    "authorization_digest",
})

_APPROVAL_FIELDS = frozenset({
    "package_schema_version",
    "status",
    "production_execution_authorized",
    "production_execution_protocol_version",
    "created_at",
    "approval_expires_at",
    "code_commit",
    "migration",
    "database",
    "backup",
    "evidence",
    "supersedes",
    "prohibited_scope",
    "execution_entry",
    "package_digest",
})


class ProductionMigrationAuthorizationError(RuntimeError):
    """Fail-closed authorization error containing only a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        reason = sanitize_reason_code(reason_code)
        super().__init__(reason)
        self.reason_code = reason


def _sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProductionMigrationAuthorizationError(reason)
    return value


def _identity(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise ProductionMigrationAuthorizationError(reason)
    return value


def _commit(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ProductionMigrationAuthorizationError(reason)
    return value


def _timestamp(value: Any, reason: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProductionMigrationAuthorizationError(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ProductionMigrationAuthorizationError(reason) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProductionMigrationAuthorizationError(reason)
    return parsed


def canonical_document_digest(
    document: Mapping[str, Any],
    *,
    digest_field: str,
) -> str:
    payload = dict(document)
    payload.pop(digest_field, None)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def migration_plan_digest(plan: MigrationPlan) -> str:
    validate_migration_plan(plan)
    return hashlib.sha256(canonical_json_bytes(plan.to_dict())).hexdigest()


def load_json_document(path: str | Path, *, reason_prefix: str) -> dict[str, Any]:
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ProductionMigrationAuthorizationError(f"{reason_prefix}_invalid")
        raw = target.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except ProductionMigrationAuthorizationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ProductionMigrationAuthorizationError(f"{reason_prefix}_invalid") from None
    if not isinstance(document, dict):
        raise ProductionMigrationAuthorizationError(f"{reason_prefix}_invalid")
    return document


def validate_approval_package(
    package: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(package, Mapping) or set(package) != _APPROVAL_FIELDS:
        raise ProductionMigrationAuthorizationError("approval_package_fields_mismatch")
    if package.get("package_schema_version") != APPROVAL_PACKAGE_SCHEMA_VERSION:
        raise ProductionMigrationAuthorizationError("approval_package_schema_mismatch")
    if package.get("status") != APPROVAL_STATUS:
        raise ProductionMigrationAuthorizationError("approval_package_status_mismatch")
    if package.get("production_execution_authorized") is not False:
        raise ProductionMigrationAuthorizationError("approval_package_authority_invalid")
    if (
        package.get("production_execution_protocol_version")
        != PRODUCTION_EXECUTION_PROTOCOL_VERSION
    ):
        raise ProductionMigrationAuthorizationError("approval_protocol_mismatch")
    expected_digest = canonical_document_digest(
        package,
        digest_field="package_digest",
    )
    if _sha(package.get("package_digest"), "approval_package_digest_invalid") != expected_digest:
        raise ProductionMigrationAuthorizationError("approval_package_digest_mismatch")

    created_at = _timestamp(package.get("created_at"), "approval_created_at_invalid")
    expires_at = _timestamp(
        package.get("approval_expires_at"),
        "approval_expiry_invalid",
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at <= created_at:
        raise ProductionMigrationAuthorizationError("approval_expiry_invalid")
    if expires_at <= current:
        raise ProductionMigrationAuthorizationError("approval_package_expired")
    _commit(package.get("code_commit"), "approval_code_commit_invalid")

    migration = package.get("migration")
    if not isinstance(migration, Mapping):
        raise ProductionMigrationAuthorizationError("approval_migration_invalid")
    expected_migration_fields = {
        "migration_id",
        "migration_version",
        "migration_digest",
        "migration_plan_digest",
        "plan_schema_version",
        "automatic_v8302_chaining",
    }
    if set(migration) != expected_migration_fields:
        raise ProductionMigrationAuthorizationError("approval_migration_fields_mismatch")
    if (
        migration.get("migration_id") != MIGRATION_ID
        or migration.get("migration_version") != MIGRATION_VERSION
        or migration.get("migration_digest") != MIGRATION_DIGEST
        or migration.get("plan_schema_version") != PLAN_SCHEMA_VERSION
        or migration.get("automatic_v8302_chaining") is not False
    ):
        raise ProductionMigrationAuthorizationError("approval_migration_mismatch")
    _sha(migration.get("migration_plan_digest"), "approval_plan_digest_invalid")

    database = package.get("database")
    if not isinstance(database, Mapping) or set(database) != {
        "fixed_identity",
        "relative_path",
        "source_database_sha256",
        "source_database_size",
        "source_schema_fingerprint",
        "target_schema_fingerprint",
        "wal_size",
    }:
        raise ProductionMigrationAuthorizationError("approval_database_fields_mismatch")
    if (
        database.get("fixed_identity") != "Main"
        or database.get("relative_path") != "data/ayunos_chatgpt.db"
        or not isinstance(database.get("source_database_size"), int)
        or database.get("source_database_size", -1) < 1
        or database.get("wal_size") != 0
    ):
        raise ProductionMigrationAuthorizationError("approval_database_mismatch")
    for field in (
        "source_database_sha256",
        "source_schema_fingerprint",
        "target_schema_fingerprint",
    ):
        _sha(database.get(field), f"approval_{field}_invalid")

    backup = package.get("backup")
    if not isinstance(backup, Mapping) or set(backup) != {
        "backup_directory",
        "backup_manifest_digest",
        "backup_set_digest",
        "source_database_sha256",
        "verified",
        "restore_rehearsal_passed",
    }:
        raise ProductionMigrationAuthorizationError("approval_backup_fields_mismatch")
    if (
        not isinstance(backup.get("backup_directory"), str)
        or not backup.get("backup_directory")
        or backup.get("verified") is not True
        or backup.get("restore_rehearsal_passed") is not True
        or backup.get("source_database_sha256")
        != database.get("source_database_sha256")
    ):
        raise ProductionMigrationAuthorizationError("approval_backup_invalid")
    _sha(backup.get("backup_manifest_digest"), "approval_backup_manifest_invalid")
    _sha(backup.get("backup_set_digest"), "approval_backup_set_invalid")

    supersedes = package.get("supersedes")
    if not isinstance(supersedes, Mapping) or set(supersedes) != {
        "package_digest",
        "classification",
    }:
        raise ProductionMigrationAuthorizationError("approval_supersedes_invalid")
    _sha(supersedes.get("package_digest"), "superseded_package_digest_invalid")
    if supersedes.get("classification") != SUPERSEDED_CLASSIFICATION:
        raise ProductionMigrationAuthorizationError("approval_supersedes_invalid")

    evidence = package.get("evidence")
    if not isinstance(evidence, Mapping) or not evidence:
        raise ProductionMigrationAuthorizationError("approval_evidence_invalid")
    for value in evidence.values():
        _sha(value, "approval_evidence_digest_invalid")

    if package.get("prohibited_scope") != [
        "v8302",
        "historical_backfill",
        "real_import",
        "ImportPlan",
        "writer",
        "asset_promotion",
    ]:
        raise ProductionMigrationAuthorizationError("approval_scope_invalid")
    if package.get("execution_entry") != "tools/run_authorized_v8301_migration.py":
        raise ProductionMigrationAuthorizationError("approval_execution_entry_invalid")
    return dict(package)


@dataclass(frozen=True)
class ProductionMigrationAuthorization:
    authorization_id: str
    authorization_scope: str
    approval_package_digest: str
    migration_version: int
    migration_digest: str
    migration_plan_digest: str
    source_database_sha256: str
    source_schema_fingerprint: str
    target_schema_fingerprint: str
    code_commit: str
    user_authorization_text_digest: str
    authorized_at: str
    expires_at: str
    one_time_execution_id: str
    authorization_schema_version: str
    authorization_digest: str

    @classmethod
    def from_mapping(
        cls,
        document: Mapping[str, Any],
    ) -> "ProductionMigrationAuthorization":
        if not isinstance(document, Mapping) or set(document) != _AUTHORIZATION_FIELDS:
            raise ProductionMigrationAuthorizationError(
                "authorization_fields_mismatch"
            )
        expected_digest = canonical_document_digest(
            document,
            digest_field="authorization_digest",
        )
        if (
            _sha(
                document.get("authorization_digest"),
                "authorization_digest_invalid",
            )
            != expected_digest
        ):
            raise ProductionMigrationAuthorizationError(
                "authorization_digest_mismatch"
            )
        if document.get("authorization_schema_version") != AUTHORIZATION_SCHEMA_VERSION:
            raise ProductionMigrationAuthorizationError(
                "authorization_schema_mismatch"
            )
        if document.get("authorization_scope") != AUTHORIZATION_SCOPE:
            raise ProductionMigrationAuthorizationError(
                "authorization_scope_mismatch"
            )
        if document.get("migration_version") != MIGRATION_VERSION:
            raise ProductionMigrationAuthorizationError(
                "authorization_target_version_mismatch"
            )
        if document.get("migration_digest") != MIGRATION_DIGEST:
            raise ProductionMigrationAuthorizationError(
                "authorization_migration_digest_mismatch"
            )
        _identity(document.get("authorization_id"), "authorization_id_invalid")
        _identity(
            document.get("one_time_execution_id"),
            "one_time_execution_id_invalid",
        )
        for field in (
            "approval_package_digest",
            "migration_plan_digest",
            "source_database_sha256",
            "source_schema_fingerprint",
            "target_schema_fingerprint",
            "user_authorization_text_digest",
        ):
            _sha(document.get(field), f"{field}_invalid")
        _commit(document.get("code_commit"), "code_commit_invalid")
        authorized_at = _timestamp(
            document.get("authorized_at"),
            "authorization_time_invalid",
        )
        expires_at = _timestamp(
            document.get("expires_at"),
            "authorization_expiry_invalid",
        )
        if expires_at <= authorized_at:
            raise ProductionMigrationAuthorizationError(
                "authorization_expiry_invalid"
            )
        return cls(**{field: document[field] for field in _AUTHORIZATION_FIELDS})

    def to_dict(self) -> dict[str, Any]:
        return {
            field: getattr(self, field)
            for field in sorted(_AUTHORIZATION_FIELDS)
        }

    def validate_time(self, *, now: datetime | None = None) -> None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        authorized_at = _timestamp(self.authorized_at, "authorization_time_invalid")
        expires_at = _timestamp(self.expires_at, "authorization_expiry_invalid")
        if authorized_at > current:
            raise ProductionMigrationAuthorizationError(
                "authorization_not_yet_valid"
            )
        if expires_at <= current:
            raise ProductionMigrationAuthorizationError("authorization_expired")


def validate_authorization_binding(
    authorization: ProductionMigrationAuthorization,
    package: Mapping[str, Any],
    *,
    expected_database_sha256: str,
    expected_current_schema: str,
    expected_target_schema: str,
    expected_migration_digest: str,
    expected_plan_digest: str,
    current_code_commit: str,
    now: datetime | None = None,
) -> None:
    validated_package = validate_approval_package(package, now=now)
    authorization.validate_time(now=now)
    migration = validated_package["migration"]
    database = validated_package["database"]
    bindings = (
        (
            authorization.approval_package_digest,
            validated_package["package_digest"],
            "authorization_package_digest_mismatch",
        ),
        (
            authorization.migration_digest,
            expected_migration_digest,
            "authorization_migration_digest_mismatch",
        ),
        (
            authorization.migration_plan_digest,
            expected_plan_digest,
            "authorization_plan_digest_mismatch",
        ),
        (
            authorization.source_database_sha256,
            expected_database_sha256,
            "authorization_database_digest_mismatch",
        ),
        (
            authorization.source_schema_fingerprint,
            expected_current_schema,
            "authorization_source_schema_mismatch",
        ),
        (
            authorization.target_schema_fingerprint,
            expected_target_schema,
            "authorization_target_schema_mismatch",
        ),
        (
            authorization.code_commit,
            current_code_commit,
            "authorization_code_commit_mismatch",
        ),
        (
            migration["migration_digest"],
            expected_migration_digest,
            "approval_migration_digest_mismatch",
        ),
        (
            migration["migration_plan_digest"],
            expected_plan_digest,
            "approval_plan_digest_mismatch",
        ),
        (
            database["source_database_sha256"],
            expected_database_sha256,
            "approval_database_digest_mismatch",
        ),
        (
            database["source_schema_fingerprint"],
            expected_current_schema,
            "approval_source_schema_mismatch",
        ),
        (
            database["target_schema_fingerprint"],
            expected_target_schema,
            "approval_target_schema_mismatch",
        ),
        (
            validated_package["code_commit"],
            current_code_commit,
            "approval_code_commit_mismatch",
        ),
    )
    for actual, expected, reason in bindings:
        if actual != expected:
            raise ProductionMigrationAuthorizationError(reason)
    if (
        authorization.migration_version != MIGRATION_VERSION
        or migration["migration_version"] != MIGRATION_VERSION
    ):
        raise ProductionMigrationAuthorizationError(
            "authorization_target_version_mismatch"
        )


def build_production_migration_approval_package_v2(
    *,
    plan: MigrationPlan,
    source_database_sha256: str,
    source_database_size: int,
    code_commit: str,
    backup_directory: str,
    backup_manifest_digest: str,
    backup_set_digest: str,
    evidence_digests: Mapping[str, str],
    old_package_digest: str,
    created_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """Build a sanitized v2 package; this never grants execution authority."""
    validate_migration_plan(plan)
    _sha(source_database_sha256, "source_database_digest_invalid")
    _commit(code_commit, "code_commit_invalid")
    _sha(backup_manifest_digest, "backup_manifest_digest_invalid")
    _sha(backup_set_digest, "backup_set_digest_invalid")
    _sha(old_package_digest, "old_package_digest_invalid")
    if not isinstance(source_database_size, int) or source_database_size < 1:
        raise ProductionMigrationAuthorizationError("source_database_size_invalid")
    if not isinstance(backup_directory, str) or not backup_directory:
        raise ProductionMigrationAuthorizationError("backup_directory_invalid")
    if not isinstance(evidence_digests, Mapping) or not evidence_digests:
        raise ProductionMigrationAuthorizationError("evidence_digests_invalid")
    sanitized_evidence = {
        str(name): _sha(value, "evidence_digest_invalid")
        for name, value in sorted(evidence_digests.items())
    }
    package = {
        "package_schema_version": APPROVAL_PACKAGE_SCHEMA_VERSION,
        "status": APPROVAL_STATUS,
        "production_execution_authorized": False,
        "production_execution_protocol_version": (
            PRODUCTION_EXECUTION_PROTOCOL_VERSION
        ),
        "created_at": created_at,
        "approval_expires_at": expires_at,
        "code_commit": code_commit,
        "migration": {
            "migration_id": plan.migration_id,
            "migration_version": MIGRATION_VERSION,
            "migration_digest": plan.migration_digest,
            "migration_plan_digest": migration_plan_digest(plan),
            "plan_schema_version": plan.plan_schema_version,
            "automatic_v8302_chaining": False,
        },
        "database": {
            "fixed_identity": "Main",
            "relative_path": "data/ayunos_chatgpt.db",
            "source_database_sha256": source_database_sha256,
            "source_database_size": source_database_size,
            "source_schema_fingerprint": plan.source_schema_fingerprint,
            "target_schema_fingerprint": (
                plan.expected_target_schema_fingerprint
            ),
            "wal_size": 0,
        },
        "backup": {
            "backup_directory": backup_directory,
            "backup_manifest_digest": backup_manifest_digest,
            "backup_set_digest": backup_set_digest,
            "source_database_sha256": source_database_sha256,
            "verified": True,
            "restore_rehearsal_passed": True,
        },
        "evidence": sanitized_evidence,
        "supersedes": {
            "package_digest": old_package_digest,
            "classification": SUPERSEDED_CLASSIFICATION,
        },
        "prohibited_scope": [
            "v8302",
            "historical_backfill",
            "real_import",
            "ImportPlan",
            "writer",
            "asset_promotion",
        ],
        "execution_entry": "tools/run_authorized_v8301_migration.py",
    }
    package["package_digest"] = canonical_document_digest(
        package,
        digest_field="package_digest",
    )
    return validate_approval_package(
        package,
        now=_timestamp(created_at, "approval_created_at_invalid"),
    )
