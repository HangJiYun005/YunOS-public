from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from memory.asset_baseline import (
    ASSET_BASELINE_SCHEMA_VERSION,
    build_asset_baseline,
)
from memory.production_real_import_authorization import (
    APPROVAL_PACKAGE_SCHEMA_VERSION,
    APPROVAL_STATUS,
    AUTHORIZATION_SCHEMA_VERSION,
    AUTHORIZATION_SCOPE,
    EXECUTION_ENTRY,
    EXECUTION_STATE_LEDGER_RELATIVE_PATH,
    PRODUCTION_ASSET_ROOT_RELATIVE_PATH,
    PRODUCTION_DATABASE_RELATIVE_PATH,
    PRODUCTION_EXECUTION_PROTOCOL_VERSION,
    STANDING_AUTHORIZATION_TEXT_DIGEST,
    canonical_document_digest,
    execution_contract_digest,
)


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def execution_contract() -> dict[str, Any]:
    return {
        "execution_entry": EXECUTION_ENTRY,
        "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "authorization_scope": AUTHORIZATION_SCOPE,
        "asset_baseline_schema_version": ASSET_BASELINE_SCHEMA_VERSION,
        "production_database_relative_path": PRODUCTION_DATABASE_RELATIVE_PATH,
        "production_asset_root_relative_path": (
            PRODUCTION_ASSET_ROOT_RELATIVE_PATH
        ),
        "asset_storage_layout": "sha256/<first2>/<sha256>",
        "service_quiescence_required": True,
        "writer_lease_required": True,
        "persistent_writer_enablement": False,
        "unresolved_conflicts_deferred": True,
        "automatic_replay": False,
        "execution_state_ledger_relative_path": (
            EXECUTION_STATE_LEDGER_RELATIVE_PATH
        ),
    }


def build_test_package(
    *,
    now: datetime | None = None,
    code_commit: str = "c" * 40,
    database_sha256: str = "a" * 64,
    database_schema: str = "b" * 64,
    database_size: int = 1,
    archive_sha256: str = "d" * 64,
    archive_size: int = 2,
    source_manifest_digest: str = "e" * 64,
    exact_plan_digest: str = "f" * 64,
    before_counts: Mapping[str, int] | None = None,
    after_counts: Mapping[str, int] | None = None,
    asset_count: int = 1,
    asset_bytes: int = 10,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    before = dict(before_counts or {
        "conversations": 1,
        "messages": 1,
        "assets": 0,
    })
    after = dict(after_counts or {
        "conversations": 1,
        "messages": 2,
        "assets": 0,
        "ingestion_asset_entities": asset_count,
    })
    package: dict[str, Any] = {
        "package_schema_version": APPROVAL_PACKAGE_SCHEMA_VERSION,
        "status": APPROVAL_STATUS,
        "production_execution_protocol_version": (
            PRODUCTION_EXECUTION_PROTOCOL_VERSION
        ),
        "created_at": utc_text(current - timedelta(minutes=5)),
        "approval_expires_at": utc_text(current + timedelta(hours=2)),
        "code_commit": code_commit,
        "production_main_baseline": {
            "size": database_size,
            "sha256": database_sha256,
            "schema_fingerprint": database_schema,
            "counts": before,
        },
        "production_asset_baseline": build_asset_baseline(
            root_state="absent",
            inventory=[],
        ),
        "imports_baseline": {
            "file_count": 1,
            "total_size": 3,
            "tree_sha256": "4" * 64,
        },
        "candidate_archive": {
            "candidate_id": "candidate_test_001",
            "location": "<registered_archive_root>/test.zip",
            "size": archive_size,
            "sha256": archive_sha256,
        },
        "source_manifest_digest": source_manifest_digest,
        "adapter_version": "test-adapter-1",
        "exact_import_plan_digest": exact_plan_digest,
        "one_time_plan_id": "realplan_" + exact_plan_digest[:48],
        "before_counts": before,
        "after_counts": after,
        "new_message_count": after["messages"] - before["messages"],
        "exact_duplicate_count": before["messages"],
        "conflict_counts": {},
        "old_time_new_id_count": 0,
        "asset_classification": "PARTIAL_ASSET_PRODUCTION_READY",
        "asset_counts": {
            "unique_mapped_logical_assets": asset_count,
            "unique_content_entities": asset_count,
            "ambiguous": 0,
            "missing": 0,
            "unsupported_or_external": 0,
        },
        "staged_byte_forecast": asset_bytes,
        "backup": {
            "backup_manifest_digest": "1" * 64,
            "backup_manifest_relative_path": (
                "backups/imports/test/backup_manifest.json"
            ),
            "backup_set_digest": "2" * 64,
        },
        "clone_execution": {"passed": True},
        "replay_result": "already_applied",
        "failure_and_recovery": {"passed": True},
        "rollback_procedure": "restore exact precommit copy",
        "maximum_expected_downtime": "bounded",
        "operator_checklist": ["validate", "execute", "verify"],
        "execution_contract": execution_contract(),
        "production_execution_authorized": False,
        "human_approval_required": True,
        "human_authorization_record_generated": False,
        "conflict_auto_decision": False,
        "historical_backfill": False,
        "v8302": False,
        "production_writer_persistent_enablement": False,
        "supersedes": [
            {
                "package_digest": (
                    "5912e6fe6fbcc8d7faa4e6c9c9f5e5644534da7e935fe971631c5a7b19d94279"
                ),
                "classification": "superseded",
            },
            {
                "package_digest": (
                    "21894e17597e840be59065feb7f0e2efb1563b336fc44d3c1e238e3f19e1224d"
                ),
                "classification": "superseded",
            },
            {
                "package_digest": (
                    "98ea836460c066850e0297453faa28eb94cd8d3359a24c01a384558030666e92"
                ),
                "classification": "superseded",
            },
        ],
        "final_conclusion": "READY_FOR_REAL_IMPORT_APPROVAL",
    }
    package["package_digest"] = canonical_document_digest(
        package,
        digest_field="package_digest",
    )
    return package


def build_test_authorization(
    package: Mapping[str, Any],
    *,
    now: datetime | None = None,
    overrides: Mapping[str, Any] | None = None,
    recompute_digest: bool = True,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    standing_digest = STANDING_AUTHORIZATION_TEXT_DIGEST
    document: dict[str, Any] = {
        "authorization_id": "authorization_real_import_test_001",
        "authorization_scope": AUTHORIZATION_SCOPE,
        "authorization_basis": "user_standing_delegation",
        "authorization_bound_by": "main_control_window",
        "standing_authorization_text_digest": standing_digest,
        "approval_package_digest": package["package_digest"],
        "bound_approval_package_digest": package["package_digest"],
        "exact_import_plan_digest": package["exact_import_plan_digest"],
        "bound_import_plan_digest": package["exact_import_plan_digest"],
        "one_time_plan_id": package["one_time_plan_id"],
        "source_database_sha256": package["production_main_baseline"]["sha256"],
        "source_schema_fingerprint": package[
            "production_main_baseline"
        ]["schema_fingerprint"],
        "source_archive_sha256": package["candidate_archive"]["sha256"],
        "source_manifest_digest": package["source_manifest_digest"],
        "bound_source_manifest_digest": package["source_manifest_digest"],
        "execution_contract_digest": execution_contract_digest(package),
        "code_commit": package["code_commit"],
        "bound_code_commit": package["code_commit"],
        "imports_tree_sha256": package["imports_baseline"]["tree_sha256"],
        "asset_baseline_digest": package[
            "production_asset_baseline"
        ]["baseline_digest"],
        "backup_manifest_digest": package["backup"]["backup_manifest_digest"],
        "backup_set_digest": package["backup"]["backup_set_digest"],
        "user_authorization_text_digest": standing_digest,
        "authorized_at": utc_text(current - timedelta(minutes=1)),
        "expires_at": utc_text(current + timedelta(minutes=30)),
        "one_time_execution_id": "execution_real_import_test_001",
        "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
    }
    if overrides:
        document.update(overrides)
    if recompute_digest:
        document["authorization_digest"] = canonical_document_digest(
            document,
            digest_field="authorization_digest",
        )
    return document
