from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any, Mapping

from memory.ingestion_core import canonical_json_bytes
from memory.ingestion_migrations import MigrationPlan
from memory.production_migration_authorization import (
    AUTHORIZATION_SCHEMA_VERSION,
    AUTHORIZATION_SCOPE,
    build_production_migration_approval_package_v2,
    canonical_document_digest,
    migration_plan_digest,
)


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_test_approval_package(
    *,
    plan: MigrationPlan,
    source_database_sha256: str,
    source_database_size: int,
    code_commit: str = "c" * 40,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    return build_production_migration_approval_package_v2(
        plan=plan,
        source_database_sha256=source_database_sha256,
        source_database_size=source_database_size,
        code_commit=code_commit,
        backup_directory="v8.3-pre-v8301-production-migration-test",
        backup_manifest_digest="b" * 64,
        backup_set_digest="d" * 64,
        evidence_digests={
            "authorization_protocol_summary": "1" * 64,
            "clone_execution_summary": "2" * 64,
            "code_review_summary": "3" * 64,
            "security_gate_summary": "4" * 64,
        },
        old_package_digest="a" * 64,
        created_at=utc_text(current - timedelta(minutes=5)),
        expires_at=utc_text(current + timedelta(hours=1)),
    )


def build_test_authorization_document(
    *,
    package: Mapping[str, Any],
    plan: MigrationPlan,
    source_database_sha256: str,
    code_commit: str | None = None,
    now: datetime | None = None,
    overrides: Mapping[str, Any] | None = None,
    recompute_digest: bool = True,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    document: dict[str, Any] = {
        "authorization_id": "authorization_v8301_test_001",
        "authorization_scope": AUTHORIZATION_SCOPE,
        "approval_package_digest": package["package_digest"],
        "migration_version": 8301,
        "migration_digest": plan.migration_digest,
        "migration_plan_digest": migration_plan_digest(plan),
        "source_database_sha256": source_database_sha256,
        "source_schema_fingerprint": plan.source_schema_fingerprint,
        "target_schema_fingerprint": plan.expected_target_schema_fingerprint,
        "code_commit": code_commit or package["code_commit"],
        "user_authorization_text_digest": hashlib.sha256(
            b"synthetic explicit v8301 authorization"
        ).hexdigest(),
        "authorized_at": utc_text(current - timedelta(minutes=1)),
        "expires_at": utc_text(current + timedelta(minutes=30)),
        "one_time_execution_id": "execution_v8301_test_001",
        "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
    }
    if overrides:
        document.update(overrides)
    if recompute_digest:
        document["authorization_digest"] = canonical_document_digest(
            document,
            digest_field="authorization_digest",
        )
    else:
        document.setdefault(
            "authorization_digest",
            hashlib.sha256(canonical_json_bytes(document)).hexdigest(),
        )
    return document
